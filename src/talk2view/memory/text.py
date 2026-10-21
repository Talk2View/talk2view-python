"""Text rules for the memory store: index and query text, dedupe keys, code-point counts.

``normalise_for_index`` is the one function every port copies. The store indexes its output
with FTS5's ``unicode61 remove_diacritics 2`` tokenizer (or scans it, on the ``scan``
backend), and search queries go through the same function.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from typing import Dict, FrozenSet, List, Tuple


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


def _segmented(ch: str) -> bool:
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
        current = _segmented(ch)
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
# unicode61's token characters: letters and numbers (categories L* and N*, the str.isalnum
# characters, which [^\W_] matches) and private use (Co).
_TOKEN = re.compile(
    r"(?:[^\W_]|["
    + chr(0xE000)
    + "-"
    + chr(0xF8FF)
    + chr(0xF0000)
    + "-"
    + chr(0xFFFFD)
    + chr(0x100000)
    + "-"
    + chr(0x10FFFD)
    + "])+"
)


def index_tokens(text: str) -> List[str]:
    """The tokens the search index makes of ``text``: FTS5's ``unicode61 remove_diacritics
    2`` over ``normalise_for_index(text)``. A token is a run of letters, numbers and
    private-use characters, so punctuation and spaces part tokens ("Deep-dish" gives deep,
    dish), and a Latin letter loses its diacritics ("cà phê" gives ca, phe). A CJK or Thai
    character is a token of its own, as ``segment_cjk`` sets it apart."""
    return _TOKEN.findall(normalise_for_index(text).translate(_LATIN_FOLD))


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
