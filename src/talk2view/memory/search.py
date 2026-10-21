"""Memory search, the reference algorithm (spec §7.2): query terms, the FTS5 query, BM25 for
the scan backend, fusion, the pre-selection floor, excerpts and the ``search_memory`` result.

Pure functions: ``MemoryStore`` reads the rows and calls these, so every port can copy them
and the contract fixtures can pin them. Text never reaches a log here.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .models import RESULT_CHARS_MAX, SUMMARY_TITLE_MAX, SearchHit
from .text import (
    SEPARATOR,
    STOP_WORDS,
    fold_latin,
    fts_tokens,
    index_token_spans,
    is_segmented,
    is_token_char,
)

#: The most terms a query keeps: the first 12 distinct ones.
QUERY_TERMS_MAX = 12
#: Reciprocal rank fusion's k (spec §7.2 step 3).
RRF_K = 60
#: Pre-selection's floor (D18). A hit goes with a turn only when its fused score is at least
#: 1 / (60 + 4), rank 4 or better in its list, and it matches at least PRESELECT_MIN_TERMS
#: distinct terms, or one CJK or Thai phrase (see ``strong_match``). Module constants, so
#: the ablation can tune them.
PRESELECT_FLOOR = 1 / 64
PRESELECT_MIN_TERMS = 2
#: A round excerpt's width in code points, ellipses aside (D19).
EXCERPT_WIDTH = 600
ELLIPSIS = chr(0x2026)
#: The note a ``search_memory`` result carries when results were dropped to fit.
TRUNCATED = "truncated"

# FTS5's bm25() constants.
_K1 = 1.2
_B = 0.75

_SPACE = re.compile(r"\s")


# ── the query (D17) ──


def query_terms(text: str) -> List[str]:
    """The terms a search for ``text`` looks for: at most QUERY_TERMS_MAX (12), each once, in
    the order they first appear.

    The tokens are the index's own (``index_tokens``: NFKC, casefolded, no combining marks,
    no Latin diacritics). A run of CJK or Thai characters written together, with nothing
    but combining marks between them, is one term: a phrase of its characters between
    single spaces ("东京" gives "东 京"), matched whole. Any other token is a term matched as
    a prefix, unless it is one of STOP_WORDS or a single character, which would match
    nearly everything. A Latin word written against CJK text is a term of its own."""
    terms: List[str] = []
    for term in _terms(unicodedata.normalize("NFKC", text)):
        if term not in terms:
            terms.append(term)
            if len(terms) == QUERY_TERMS_MAX:
                break
    return terms


def _terms(text: str) -> Iterator[str]:
    """``text``'s terms in order, repeats included, lazily. ``text`` is in NFKC already, so
    ``index_token_spans`` gives ``index_tokens``' own tokens."""
    run: List[str] = []
    run_end = 0
    for token, start, end in index_token_spans(text):
        if len(token) == 1 and is_segmented(token):
            if run and not _parted(text[run_end:start]):
                run.append(token)
            else:
                if run:
                    yield " ".join(run)
                run = [token]
            run_end = end
            continue
        if run:
            yield " ".join(run)
            run = []
        if len(token) > 1 and token not in STOP_WORDS:
            yield token
    if run:
        yield " ".join(run)


def _parted(between: str) -> bool:
    """Whether the text ``between`` two tokens parts them: anything but combining marks,
    which normalisation removes."""
    return any(unicodedata.category(ch) != "Mn" for ch in between)


def is_phrase(term: str) -> bool:
    """Whether ``term`` (one of ``query_terms``') is matched whole, as a phrase: a run of
    CJK or Thai characters, one or more. Any other term is matched as a prefix."""
    return " " in term or (len(term) == 1 and is_segmented(term))


def build_fts_query(text: str) -> Optional[str]:
    """The FTS5 query a search for ``text`` runs, or None when ``text`` has no terms (stop
    words only, say): each of ``query_terms`` quoted, ``"tok"*`` for a prefix term and
    ``"东 京"`` for a phrase, joined with `` OR ``.

    Nothing in ``text`` can become FTS5 syntax. A term is made of token characters only, so
    quotes, parentheses, ``*``, ``^``, ``:`` and ``+`` never reach the query, and a word such
    as NEAR, AND or NOT stays a quoted string, which FTS5 reads as text. A ``"`` would be
    doubled, as FTS5 escapes it, though no term holds one."""
    terms = query_terms(text)
    return fts_query(terms) if terms else None


def fts_query(terms: Sequence[str]) -> str:
    """``query_terms``' terms as an FTS5 query: each one quoted, ``*`` after a prefix term,
    joined with `` OR ``."""
    return " OR ".join(fts_term(term) for term in terms)


def fts_term(term: str) -> str:
    """One term as FTS5 query syntax: a quoted string, followed by ``*`` for a prefix
    term."""
    quoted = '"' + term.replace('"', '""') + '"'
    return quoted if is_phrase(term) else quoted + "*"


# ── matching and BM25 (the scan backend's FTS5) ──


def term_frequency(tokens: Sequence[str], term: str) -> int:
    """How often ``term`` occurs in a text with the index tokens ``tokens``, as FTS5 counts
    a query phrase's instances: a prefix term once for each token it begins, a phrase once
    for each place its tokens stand in a row."""
    if not is_phrase(term):
        return sum(1 for token in tokens if token.startswith(term))
    parts = term.split(" ")
    size = len(parts)
    return sum(
        1
        for at in range(len(tokens) - size + 1)
        if tokens[at] == parts[0] and list(tokens[at : at + size]) == parts
    )


def matched_terms(tokens: Sequence[str], terms: Sequence[str]) -> List[str]:
    """The terms that occur in a text with the index tokens ``tokens``."""
    return [term for term in terms if term_frequency(tokens, term)]


def _counter(term: str) -> Callable[[str], int]:
    """What counts ``term``'s instances, as ``term_frequency`` counts them, in index text
    with its diacritics folded (``fold_latin`` of a ``seg_text``), without tokenizing it: a
    prefix term where it starts a token, a phrase where its first character is followed by
    the rest, each apart from the one before by separators only. In ``seg_text`` a CJK or
    Thai character is always a token of its own."""
    if not is_phrase(term):
        return lambda text: _token_starts(text, term)
    first, *rest = term.split(" ")
    following = "".join(SEPARATOR + "+" + re.escape(part) for part in rest)
    pattern = re.compile(re.escape(first) + ("(?=" + following + ")" if rest else ""))
    return lambda text: len(pattern.findall(text))


def _token_starts(text: str, term: str) -> int:
    """How many tokens of ``text`` start with ``term``, a run of token characters: no
    token can start inside one of its instances, so each search goes on after it."""
    count = 0
    at = text.find(term)
    while at >= 0:
        if at == 0 or not is_token_char(text[at - 1]):
            count += 1
        at = text.find(term, at + len(term))
    return count


def bm25_scan(
    docs: Sequence[Tuple[str, str]],
    terms: Sequence[str],
    *,
    cache: Optional[Dict[str, Tuple[int, str]]] = None,
) -> List[Tuple[str, float]]:
    """FTS5's ``bm25()`` without FTS5, for the scan backend: the ``(key, score)`` of each row
    of ``docs``, ``(key, seg_text)`` pairs, that matches any of ``terms``, best (lowest)
    score first, a tie in ``docs``' order. It is what ``SELECT key, bm25(t) FROM t WHERE t
    MATCH ?`` gives, with ``build_fts_query``'s query, on an FTS5 table holding ``docs``.

    So pass every row of the index, as FTS5 measures the whole table: the row count, the
    average row length in tokens and each term's count of rows. The constants are FTS5's
    (k1 = 1.2, b = 0.75; an IDF at or under 0 counts as 1e-6) and so is the order of the
    arithmetic, though FTS5 may round its last bit differently where its build fuses a
    multiply and an add. A substring test of each row's diacritic-folded text finds the
    rows that may match; ``cache`` keeps each row's token count and folded text, keyed by
    its text, from one call to the next, and is left holding this call's rows only."""
    if not terms:
        return []
    known = cache if cache is not None else {}
    wanted = [term.split(" ") for term in terms]
    rows: Dict[str, Tuple[int, str]] = {}
    total = 0
    candidates: List[Tuple[str, str, int]] = []
    for key, seg_text in docs:
        measured = known.get(seg_text)
        if measured is None:
            measured = (len(fts_tokens(seg_text)), fold_latin(seg_text))
        rows[seg_text] = measured
        size, folded = measured
        total += size
        if any(all(part in folded for part in parts) for parts in wanted):
            candidates.append((key, folded, size))
    if cache is not None:
        cache.clear()
        cache.update(rows)
    counters = [_counter(term) for term in terms]
    matches: List[Tuple[str, int, List[int]]] = []
    for key, folded, size in candidates:
        freqs = [count(folded) for count in counters]
        if any(freqs):
            matches.append((key, size, freqs))
    if not matches:
        return []
    rows_in_table = len(docs)
    average = total / rows_in_table
    idf: List[float] = []
    for position in range(len(terms)):
        hits = sum(1 for _, _, freqs in matches if freqs[position])
        weight = math.log((rows_in_table - hits + 0.5) / (hits + 0.5))
        idf.append(weight if weight > 0.0 else 1e-6)
    scored: List[Tuple[str, float]] = []
    for key, size, freqs in matches:
        score = 0.0
        for weight, freq in zip(idf, freqs):
            score += weight * ((freq * (_K1 + 1.0)) / (freq + _K1 * (1 - _B + _B * size / average)))
        scored.append((key, -1.0 * score))
    scored.sort(key=lambda item: item[1])
    return scored


# ── ranking, fusion and the floor (D18) ──


def round_ref(conversation_id: str, idx: int) -> str:
    """A round's id among search hits: ``r:<conversation_id>:<idx>``. A fact's is its own
    id, which never starts with ``r:``."""
    return "r:" + conversation_id + ":" + str(idx)


def best_first(rows: List[Tuple[float, str, int, str]], limit: int) -> List[Tuple[str, str]]:
    """The first ``limit`` of one list's matching rows, ``(score, time, rowid, id)``, as
    ``(id, time)``: by bm25 score (lower is better), then the newer (ISO 8601 times sort as
    text), then the later row, as the FTS5 backends' SQL orders them."""
    rows.sort(key=lambda row: row[2], reverse=True)
    rows.sort(key=lambda row: row[1], reverse=True)
    rows.sort(key=lambda row: row[0])
    return [(ref, time) for _, time, _, ref in rows[:limit]]


def rrf(lists: Sequence[Sequence[str]], *, k: int = RRF_K) -> Dict[str, float]:
    """Reciprocal rank fusion: each id's sum, over the lists it is in, of 1 / (k + its
    rank), ranks counted from 1."""
    scores: Dict[str, float] = {}
    for ranked in lists:
        for rank, ref in enumerate(ranked, start=1):
            scores[ref] = scores.get(ref, 0.0) + 1.0 / (k + rank)
    return scores


def fuse(
    lists: Sequence[Sequence[str]], recency: Mapping[str, str], *, k: int = RRF_K
) -> List[Tuple[str, float]]:
    """The ids of ``lists`` with their ``rrf`` scores, best first. A tie goes to the newer,
    by ``recency`` (each id's ISO 8601 time, which sorts as text), then to the id that
    comes first in ``lists`` read in order."""
    scores = rrf(lists, k=k)
    order = list(dict.fromkeys(ref for ranked in lists for ref in ranked))
    order.sort(key=lambda ref: recency.get(ref, ""), reverse=True)
    order.sort(key=lambda ref: scores[ref], reverse=True)
    return [(ref, scores[ref]) for ref in order]


def strong_match(tokens: Sequence[str], terms: Sequence[str]) -> bool:
    """Whether a text with the index tokens ``tokens`` matches enough of ``terms`` to be
    pre-selected: PRESELECT_MIN_TERMS (2) distinct terms, or one phrase of two or more CJK
    or Thai characters, specific enough on its own. A lone character is not."""
    matched = matched_terms(tokens, terms)
    return len(matched) >= PRESELECT_MIN_TERMS or any(" " in term for term in matched)


def preselected(score: float, tokens: Sequence[str], terms: Sequence[str]) -> bool:
    """Whether a hit clears pre-selection's floor: a fused ``score`` of at least
    PRESELECT_FLOOR and a ``strong_match``."""
    return score >= PRESELECT_FLOOR and strong_match(tokens, terms)


# ── hits and excerpts (D19) ──


def conversation_title(title: Optional[str], summary_title: Optional[str]) -> str:
    """What a conversation is called in what the engine is sent: its own title, else its
    summary's, cut to SUMMARY_TITLE_MAX (120) code points; "" when it has neither, or only
    blank ones, as when a forget has cleared them."""
    for name in (title, summary_title):
        if name and name.strip():
            return name[:SUMMARY_TITLE_MAX]
    return ""


def round_text(user_text: str, assistant_text: str) -> str:
    """A round as a search hit shows it, saying who said what."""
    parts = []
    if user_text:
        parts.append("User: " + user_text)
    if assistant_text:
        parts.append("Assistant: " + assistant_text)
    return "\n".join(parts)


def first_match(text: str, terms: Sequence[str]) -> Optional[Tuple[int, int]]:
    """The span of ``text``, in code points, where a term first occurs as the index would
    match it, or None. A phrase is found where its last token is."""
    wanted = [(term.split(" "), not is_phrase(term)) for term in terms]
    if not wanted:
        return None
    longest = max(len(parts) for parts, _ in wanted)
    recent: List[Tuple[str, int]] = []  # the last tokens, with where each starts
    for token, start, end in index_token_spans(text):
        recent = (recent + [(token, start)])[-longest:]
        for parts, prefix in wanted:
            if prefix:
                if token.startswith(parts[0]):
                    return start, end
            elif len(recent) >= len(parts) and [t for t, _ in recent[-len(parts) :]] == parts:
                return recent[-len(parts)][1], end
    return None


def excerpt(text: str, terms: Sequence[str], *, width: int = EXCERPT_WIDTH) -> str:
    """``text`` whole when it is at most ``width`` code points; else ``width`` of them
    around the first place a term occurs, half before it (the start of the text when none
    does). A cut that falls inside a word moves inward to the nearest whitespace between it
    and the match, when there is any, and each end that was cut gets an ellipsis (U+2026),
    so the excerpt is at most ``width`` + 2 code points."""
    if len(text) <= width:
        return text
    found = first_match(text, terms)
    begin, finish = found if found is not None else (0, 0)
    start = max(0, min(begin - width // 2, len(text) - width))
    end = start + width
    if start > 0 and not text[start - 1].isspace():
        space = _SPACE.search(text, start, max(begin, start))
        if space is not None:
            start = space.end()
    if end < len(text) and not text[end].isspace():
        for position in range(end - 1, max(finish, start) - 1, -1):
            if text[position].isspace():
                end = position
                break
    piece = text[start:end].strip()
    return (ELLIPSIS if start > 0 else "") + piece + (ELLIPSIS if end < len(text) else "")


def fact_hit(ref: str, score: float, text: str, recorded_at: str, title: str) -> SearchHit:
    """A fact as a search result: its text whole, dated the day it was recorded."""
    return SearchHit(
        kind="fact", date=recorded_at[:10], conversation=title, text=text, score=score, ref=ref
    )


def round_hit(
    ref: str,
    score: float,
    user_text: str,
    assistant_text: str,
    at: str,
    title: str,
    terms: Sequence[str],
) -> SearchHit:
    """A round as a search result: an ``excerpt`` of its ``round_text``, dated the day it
    was said."""
    return SearchHit(
        kind="excerpt",
        date=at[:10],
        conversation=title,
        text=excerpt(round_text(user_text, assistant_text), terms),
        score=score,
        ref=ref,
    )


# ── the search_memory result (spec §8.4) ──


def result_json(hits: Sequence[SearchHit]) -> str:
    """The ``search_memory`` tool's result: ``{"results": [{"date", "conversation", "kind",
    "text"}]}`` as compact JSON, at most RESULT_CHARS_MAX (8,000) code points. Results that
    would take it over are dropped from the end, and ``"note": "truncated"`` says so."""
    results = [
        {"date": hit.date, "conversation": hit.conversation, "kind": hit.kind, "text": hit.text}
        for hit in hits
    ]
    document = _dumps({"results": results})
    while len(document) > RESULT_CHARS_MAX and results:
        results.pop()
        document = _dumps({"results": results, "note": TRUNCATED})
    return document


def _dumps(value: Dict[str, Any]) -> str:
    # Characters as themselves, not \u escapes: the cap counts code points.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "ELLIPSIS",
    "EXCERPT_WIDTH",
    "PRESELECT_FLOOR",
    "PRESELECT_MIN_TERMS",
    "QUERY_TERMS_MAX",
    "RRF_K",
    "TRUNCATED",
    "best_first",
    "bm25_scan",
    "build_fts_query",
    "conversation_title",
    "excerpt",
    "fact_hit",
    "first_match",
    "fts_query",
    "fts_term",
    "fuse",
    "is_phrase",
    "matched_terms",
    "preselected",
    "query_terms",
    "result_json",
    "round_hit",
    "round_ref",
    "round_text",
    "rrf",
    "strong_match",
    "term_frequency",
]
