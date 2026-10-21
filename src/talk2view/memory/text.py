"""Text rules for the memory store: index and query text, dedupe keys, code-point counts.

``normalise_for_index`` is the one function every port copies. The store indexes its output
with FTS5's ``unicode61 remove_diacritics 2`` tokenizer (or scans it, on the ``scan``
backend), and search queries go through the same function.
"""

from __future__ import annotations

import functools
import itertools
import re
import unicodedata
from typing import Dict, FrozenSet, Iterator, List, Tuple


def code_points(text: str) -> int:
    """The length of ``text`` in Unicode code points, the unit of every memory cap: ``len``
    in Python. Never UTF-16 units (TypeScript's ``.length``), graphemes (Swift's
    ``String.count``) or bytes."""
    return len(text)


#: Inclusive code-point ranges of the scripts written without spaces between words: Thai,
#: kana and Han (with its extensions and compatibility ideographs). ``segment_cjk`` sets
#: their code points apart, so unicode61 makes each one a token.
_SEGMENTED: Tuple[Tuple[int, int], ...] = (
    # No Hangul: Korean spaces its words, and a prefix term matches a word with a particle.
    (0x0E00, 0x0E7F),  # Thai
    (0x3005, 0x3007),  # ideographic iteration mark, closing mark, number zero
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x31F0, 0x31FF),  # Katakana phonetic extensions
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFF66, 0xFF9F),  # Halfwidth Katakana
    (0x20000, 0x323AF),  # CJK Extensions B to I, Compatibility Ideographs Supplement
)


def is_segmented(ch: str) -> bool:
    """Whether ``segment_cjk`` sets the code point ``ch`` apart: a CJK or Thai one, which
    the index makes a token of its own."""
    point = ord(ch)
    return point >= 0x0E00 and any(low <= point <= high for low, high in _SEGMENTED)


def segment_cjk(text: str) -> str:
    """Set every CJK or Thai code point apart with a space on each side, against any
    neighbour that is not whitespace already, leaving all other text as it is. unicode61 then
    makes each such code point a token, and a Latin word written against them without
    spaces, as Chinese often writes one, stays a token of its own. Expects text without
    combining marks: ``normalise_for_index`` strips them first, so a Thai vowel sign is never
    parted from its consonant."""
    out: List[str] = []
    previous = False
    for ch in text:
        current = is_segmented(ch)
        if (current or previous) and out and not (ch.isspace() or out[-1].isspace()):
            out.append(" ")
        out.append(ch)
        previous = current
    return "".join(out)


def normalise_for_index(text: str) -> str:
    """The text the store indexes and builds queries from: NFKC, casefolded, combining marks
    (category ``Mn``) removed, then ``segment_cjk``.

    Removing the marks keeps a word one token. unicode61's token characters are ``L*``,
    ``N*`` and ``Co``, so an Arabic haraka or a decomposed Vietnamese mark left in place
    would split the word around it."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return segment_cjk("".join(ch for ch in folded if unicodedata.category(ch) != "Mn"))


def _latin_fold() -> Dict[int, int]:
    """unicode61's ``remove_diacritics 2`` as a ``str.translate`` table: each Latin letter
    whose canonical decomposition is an ASCII letter and combining marks, to that letter."""
    table: Dict[int, int] = {}
    for point in itertools.chain(range(0x00C0, 0x0250), range(0x1E00, 0x1F00)):
        decomposed = unicodedata.normalize("NFD", chr(point))
        if len(decomposed) > 1 and decomposed[0].isascii() and decomposed[0].isalpha():
            table[point] = ord(decomposed[0])
    return table


_LATIN_FOLD = _latin_fold()
_PRIVATE_USE = (
    chr(0xE000)
    + "-"
    + chr(0xF8FF)
    + chr(0xF0000)
    + "-"
    + chr(0xFFFFD)
    + chr(0x100000)
    + "-"
    + chr(0x10FFFD)
)
#: One of unicode61's token characters, as a regular expression: a letter or number
#: (categories L* and N*, the str.isalnum characters, which [^\W_] matches) or private use
#: (Co).
TOKEN_CHAR = r"(?:[^\W_]|[" + _PRIVATE_USE + "])"
#: One character that parts tokens: any other.
SEPARATOR = r"(?:(?![" + _PRIVATE_USE + r"])[\W_])"
_TOKEN = re.compile(TOKEN_CHAR + "+")


def is_token_char(ch: str) -> bool:
    """Whether the code point ``ch`` is one of unicode61's token characters (TOKEN_CHAR)."""
    return ch.isalnum() or unicodedata.category(ch) == "Co"


def index_tokens(text: str) -> List[str]:
    """The tokens the search index makes of ``text``: FTS5's ``unicode61 remove_diacritics
    2`` over ``normalise_for_index(text)``. A token is a run of letters, numbers and
    private-use characters, so punctuation and spaces part tokens ("Deep-dish" gives deep,
    dish), and a Latin letter loses its diacritics ("cà phê" gives ca, phe). A CJK or Thai
    character is a token of its own, as ``segment_cjk`` sets it apart."""
    return fts_tokens(normalise_for_index(text))


def fold_latin(text: str) -> str:
    """unicode61's ``remove_diacritics 2`` over ``text``: each Latin letter with diacritics
    to its base letter. ASCII text, which has none, comes back as it is."""
    return text if text.isascii() else text.translate(_LATIN_FOLD)


def fts_tokens(seg_text: str) -> List[str]:
    """``index_tokens`` of text that ``normalise_for_index`` has made already, such as the
    index's ``seg_text``, without normalising it again: just the tokenizer's own step."""
    return _TOKEN.findall(fold_latin(seg_text))


_ASCII_TOKEN = re.compile(r"[A-Za-z0-9]+")  # unicode61's token characters in ASCII


@functools.lru_cache(maxsize=4096)
def _folded(ch: str) -> str:
    """The code point ``ch`` as the index sees it on its own: ``normalise_for_index`` and
    the Latin fold, with a space for each separator and a space on each side of a CJK or
    Thai character, a token of its own. Nothing for a combining mark, which normalising
    removes without parting the token around it."""
    pieces: List[str] = []
    for folded in fold_latin(normalise_for_index(ch)):
        if is_segmented(folded):
            pieces.append(" " + folded + " ")
        else:
            pieces.append(folded if is_token_char(folded) else " ")
    return "".join(pieces)


def index_token_spans(text: str) -> Iterator[Tuple[str, int, int]]:
    """The tokens of ``text`` with the span each came from, ``(token, start, end)`` in code
    points of ``text`` as written, lazily, in order: for finding a term in the text itself.

    Each code point is normalised on its own, so the spans are exact. The tokens are
    ``index_tokens(text)`` whenever NFKC of the whole text is NFKC of each code point
    joined, as it is for text in NFKC already; where NFKC composes across code points
    (conjoining Hangul jamo, for one) they can differ."""
    if text.isascii():
        for match in _ASCII_TOKEN.finditer(text):
            yield match.group().lower(), match.start(), match.end()
        return
    token: List[str] = []
    start = end = 0
    for position, ch in enumerate(text):
        folded = _folded(ch)
        if not folded and token:
            end = position + 1  # a combining mark inside the token
        for piece in folded:
            if piece != " ":
                if not token:
                    start = position
                token.append(piece)
                end = position + 1
            elif token:
                yield "".join(token), start, end
                token = []
    if token:
        yield "".join(token), start, end


def normalise_dedupe(text: str) -> str:
    """The key by which two memory entries are the same: NFKC, casefolded, whitespace
    collapsed to single spaces, trailing ``.!?,;:`` removed."""
    folded = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return folded.rstrip(".!?,;: ")


#: English function words a search query leaves out. Chat pronouns are among them: nearly
#: every round holds "my" or "you", so as query terms they would match everything. Single
#: letters ("a", "i") are not listed: the query drops every 1-character Latin token already.
STOP_WORDS: FrozenSet[str] = frozenset(
    """
    about all an and are as at be been but by did do does for from had has have how if in
    is it me my of on or our that the their they this to was we were what when where which
    who will with you your
    """.split()
)
