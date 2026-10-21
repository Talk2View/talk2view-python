"""The Agent Skills format (agentskills.io/specification) for skills the SDK writes.

A skill is a folder holding ``SKILL.md``: YAML frontmatter, then markdown. This module
reads and writes that file and checks it against the specification. It does no I/O.

The frontmatter reader is deliberately small: the SDK installs into host Pythons (3D
Slicer's) that have no YAML library, and the specification's fields are plain or quoted
scalars, ``|``/``>`` block scalars, and one string-to-string map (``metadata``). Anything
else is reported as a ``Problem`` on its field, never guessed at. Every frontmatter block
is kept as the raw text it was read from, so an edit through ``with_fields`` rewrites
only the fields it changes and leaves every other block byte-for-byte as it was read.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple, Union

from .skills import FRONTMATTER_RE, Skill

#: Agent Skills specification limits (agentskills.io/specification).
NAME_MAX = 64
DESCRIPTION_MAX = 1024
COMPATIBILITY_MAX = 500
#: A body past this many lines is legal but large enough to flag to the author.
LONG_BODY_LINES = 500
RESERVED_WORDS = ("anthropic", "claude")

_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_XML_TAG_RE = re.compile(r"<[^<>\n]+>")
#: The start of a frontmatter block: ``key:`` optionally followed by an inline value.
_KEY_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(?:[ \t]+(.*)|[ \t]*)$")
#: One ``key: value`` line inside an indented map (``metadata``).
_MAP_LINE_RE = re.compile(r"^[ \t]+([A-Za-z0-9_.-]+):(?:[ \t]+(.*)|[ \t]*)$")
#: A value safe to write unquoted: does not start with a YAML indicator character, and
#: (checked separately) contains neither ``": "`` nor ``" #"`` and does not end in
#: ``":"``/`` `` — each of those would change meaning, or be read back differently, if
#: left unquoted.
_PLAIN_SAFE_RE = re.compile(r"""^[^\s'"#&*!|>%@`{}\[\],?:-][^\n]*$""")
#: Every character ``str.splitlines()`` breaks a line at, other than ``"\n"``. The
#: frontmatter is read line by line with ``splitlines``, so one of these inside a name or
#: description would split it on the way back in: ``validate`` refuses them.
_ODD_LINE_BREAKS_RE = re.compile("[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
_ODD_LINE_BREAK_MESSAGE = (
    "contains a line-break character that isn't a normal newline "
    "(e.g. from a pasted document); remove it"
)

#: A frontmatter value as this reader understands it: text, or a flat string map.
_Value = Union[str, Dict[str, str]]


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a ``SkillDoc``, or one line of frontmatter this reader could
    not make sense of. ``field`` is one of "name", "description", "body",
    "compatibility", "metadata", "allowed-tools", or "frontmatter" for an unreadable
    line that belongs to no field."""

    field: str
    message: str


@dataclass(frozen=True)
class SkillDoc:
    """A SKILL.md, split into the fields the SDK cares about and the frontmatter blocks
    it does not otherwise touch."""

    name: str
    description: str
    body: str
    allowed_tools: Optional[List[str]] = None
    #: The frontmatter as ``(key, raw text)`` in file order, each raw text including its
    #: key line and any continuation lines, every line ending in ``"\\n"``. ``key`` is
    #: ``"#"`` for a comment or blank line kept verbatim between fields.
    blocks: Tuple[Tuple[str, str], ...] = ()


class _Unsupported(ValueError):
    """A frontmatter value used a YAML form this reader does not support."""


def _key_match(line: str) -> "re.Match[str]":
    match = _KEY_LINE_RE.match(line)
    if match is None:
        raise ValueError("not a 'key: value' line: {!r}".format(line))
    return match


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        out: List[str] = []
        i, body = 0, raw[1:-1]
        escapes = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body) and body[i + 1] in escapes:
                out.append(escapes[body[i + 1]])
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    if raw.startswith(("'", '"')):
        raise _Unsupported("an unterminated quoted value")
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    return raw


def _block_scalar(indicator: str, lines: List[str]) -> str:
    """The value of a ``|``/``>`` block scalar. Chomping (``-``/``+``) is read but this
    reader always writes (and only needs to round-trip) the default single trailing
    newline, or none for ``-`` (strip); ``+`` (keep) is read the same as the default."""
    if not lines:
        return ""
    indent = min(len(line) - len(line.lstrip(" ")) for line in lines if line.strip())
    stripped = [line[indent:] if line.strip() else "" for line in lines]
    if indicator.startswith("|"):
        text = "\n".join(stripped)
    else:
        text, para = "", []
        for line in stripped:
            if line:
                para.append(line)
            else:
                text += " ".join(para) + "\n"
                para = []
        text += " ".join(para)
    text = text.rstrip("\n")
    return text if indicator.endswith("-") else text + "\n"


def _read_value(key: str, first: str, rest: List[str]) -> _Value:
    """The parsed value of one block: text, or a flat string map for ``metadata``."""
    if first in ("|", "|-", "|+", ">", ">-", ">+"):
        return _block_scalar(first, rest)
    if first:
        if rest and any(line.strip() for line in rest):
            raise _Unsupported("a value continued on the next line; quote it or use | or >")
        return _unquote(first)
    if not any(line.strip() for line in rest):
        return ""
    mapping: Dict[str, str] = {}
    for line in rest:
        if not line.strip():
            continue
        match = _MAP_LINE_RE.match(line)
        if not match or match.group(2) in (None, "", "|", ">", "|-", ">-"):
            raise _Unsupported("only 'key: value' lines of text are allowed under {}".format(key))
        mapping[match.group(1)] = _unquote(match.group(2))
    return mapping


def parse(text: str, fallback_name: str = "") -> Tuple[SkillDoc, List[Problem]]:
    """Read a SKILL.md. Never raises: whatever cannot be read becomes a ``Problem`` on
    its field, and that field is left absent from the returned ``SkillDoc``."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return SkillDoc(name=fallback_name, description="", body=text), []
    raw_lines = match.group(1).splitlines(keepends=True)
    body = match.group(2)

    blocks: List[Tuple[str, str]] = []
    problems: List[Problem] = []
    current: Optional[int] = None
    for line in raw_lines:
        if not line.endswith("\n"):
            line = line + "\n"
        key_match = _KEY_LINE_RE.match(line.rstrip("\r\n"))
        if key_match:
            blocks.append((key_match.group(1), line))
            current = len(blocks) - 1
        elif current is not None and (line.startswith((" ", "\t")) or not line.strip()):
            key, raw = blocks[current]
            blocks[current] = (key, raw + line)
        elif line.lstrip().startswith("#") or not line.strip():
            blocks.append(("#", line))
            current = None
        else:
            problems.append(Problem("frontmatter", "could not read {!r}".format(line.strip()[:60])))

    values: Dict[str, _Value] = {}
    for key, raw in blocks:
        if key == "#":
            continue
        head, *rest = raw.splitlines()
        first = (_key_match(head).group(2) or "").strip()
        try:
            values[key] = _read_value(key, first, rest)
        except _Unsupported as exc:
            problems.append(Problem(key, str(exc)))

    for key in ("name", "description", "compatibility", "allowed-tools", "license"):
        if isinstance(values.get(key), dict):
            problems.append(Problem(key, "must be text, not a list of fields"))
            values.pop(key)
    if "metadata" in values and not isinstance(values["metadata"], dict):
        problems.append(Problem("metadata", "must be 'key: value' lines of text"))

    tools_raw = values.get("allowed-tools") or values.get("allowed_tools")
    tools = [t for t in re.split(r"[,\s]+", str(tools_raw).strip()) if t] if tools_raw else None
    doc = SkillDoc(
        name=str(values.get("name") or fallback_name),
        description=str(values.get("description") or ""),
        body=body,
        allowed_tools=tools or None,
        blocks=tuple(blocks),
    )
    return doc, problems


def _block_safe(value: str) -> bool:
    """Whether ``value`` (multi-line) reads back exactly from a ``|-`` block literal:
    the reader strips the block's common indentation, drops trailing newlines, and reads
    a whitespace-only line as empty -- so a value whose every line is indented, that ends
    in a newline, or that has a whitespace-only line, is quoted instead."""
    lines = value.split("\n")
    if value.endswith("\n") or any(line and not line.strip() for line in lines):
        return False
    return any(line.strip() and not line[0].isspace() for line in lines)


def _scalar(value: str) -> str:
    """``value`` rendered as a YAML scalar, quoted or block-literal only when needed to
    read back byte-for-byte (``parse`` gives back exactly ``value``; see
    tests/test_skill_format.py's round-trip test)."""
    if "\n" in value and _block_safe(value):
        return "|-\n" + "".join("  " + line + "\n" for line in value.split("\n"))
    if (
        value
        and _PLAIN_SAFE_RE.match(value)
        and value == value.strip()
        and ": " not in value
        and " #" not in value
        and "\t#" not in value
        and not value.endswith(":")
    ):
        return value + "\n"
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\n", "\\n")
    )
    return '"' + escaped + '"\n'


def _field_block(key: str, value: str) -> str:
    return key + ": " + _scalar(value)


def render(doc: SkillDoc) -> str:
    """The SKILL.md text for ``doc``. Round-trips byte-for-byte: for a ``doc`` straight
    from ``parse``, ``render(doc) == <the text parsed>``."""
    front = "".join(raw for _key, raw in doc.blocks)
    body = doc.body if doc.body.endswith("\n") else doc.body + "\n"
    return "---\n" + front + "---\n" + body


def with_fields(
    doc: SkillDoc,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    body: Optional[str] = None,
) -> SkillDoc:
    """``doc`` with these fields changed. Every other frontmatter block (license,
    metadata, compatibility, ...) is kept exactly as read; a changed field's block is
    rewritten and kept at its original position, or added (name, then description) when
    it was not already present."""
    blocks = list(doc.blocks)
    for key, value in (("name", name), ("description", description)):
        if value is None:
            continue
        new_block = (key, _field_block(key, value))
        for i, (existing_key, _raw) in enumerate(blocks):
            if existing_key == key:
                blocks[i] = new_block
                break
        else:
            at_start = key == "name"
            after_name = bool(blocks) and blocks[0][0] == "name"
            blocks.insert(0 if at_start else (1 if after_name else 0), new_block)
    return replace(
        doc,
        name=doc.name if name is None else name,
        description=doc.description if description is None else description,
        body=doc.body if body is None else body,
        blocks=tuple(blocks),
    )


def from_form(name: str, description: str, instructions: str) -> SkillDoc:
    """A fresh ``SkillDoc`` for a skill entered through a form: just ``name`` then
    ``description`` in the frontmatter, and ``instructions`` as the body."""
    return with_fields(
        SkillDoc(name="", description="", body=""),
        name=name,
        description=description,
        body=instructions,
    )


def _reread(raw_block: str) -> Optional[_Value]:
    """The value of one raw frontmatter block, re-read from its own text exactly as
    ``parse`` would. Used by ``validate`` for fields (``compatibility``, ``metadata``)
    that ``SkillDoc`` does not carry as a typed attribute, so validation also covers a
    doc built without going through ``parse``'s ``Problem`` list (e.g. one hand-built
    with ``with_fields``). ``None`` for a block this reader cannot read at all."""
    head, *rest = raw_block.splitlines()
    match = _KEY_LINE_RE.match(head)
    if not match:
        return None
    try:
        return _read_value(match.group(1), (match.group(2) or "").strip(), rest)
    except _Unsupported:
        return None


def validate(doc: SkillDoc, *, folder: Optional[str] = None) -> List[Problem]:
    """The specification's rules, checked against ``doc``. An empty list means valid.
    ``folder`` is the name of the directory ``SKILL.md`` would live in; the spec
    requires ``name`` to match it."""
    problems: List[Problem] = []
    name = doc.name
    if not name:
        problems.append(Problem("name", "a skill needs a name"))
    elif _ODD_LINE_BREAKS_RE.search(name):
        problems.append(Problem("name", _ODD_LINE_BREAK_MESSAGE))
    elif len(name) > NAME_MAX:
        problems.append(
            Problem("name", "at most {} characters; got {}".format(NAME_MAX, len(name)))
        )
    elif _XML_TAG_RE.search(name) or not _NAME_RE.match(name):
        problems.append(
            Problem(
                "name",
                "lowercase letters, numbers and single hyphens, not at the start or end; "
                "got {!r}".format(name),
            )
        )
    elif any(word in name for word in RESERVED_WORDS):
        problems.append(Problem("name", "must not contain 'anthropic' or 'claude'"))
    elif folder is not None and folder != name:
        problems.append(Problem("name", "must match its folder name {!r}".format(folder)))

    if not doc.description.strip():
        problems.append(Problem("description", "say what the skill does and when to use it"))
    elif _ODD_LINE_BREAKS_RE.search(doc.description):
        problems.append(Problem("description", _ODD_LINE_BREAK_MESSAGE))
    elif len(doc.description) > DESCRIPTION_MAX:
        problems.append(
            Problem(
                "description",
                "at most {} characters; got {}".format(DESCRIPTION_MAX, len(doc.description)),
            )
        )
    elif _XML_TAG_RE.search(doc.description):
        problems.append(Problem("description", "must not contain XML tags"))

    if not doc.body.strip():
        problems.append(Problem("body", "the instructions are empty"))

    raw_by_key = dict(doc.blocks)
    if "compatibility" in raw_by_key:
        compat = _reread(raw_by_key["compatibility"])
        if isinstance(compat, str) and len(compat) > COMPATIBILITY_MAX:
            problems.append(
                Problem("compatibility", "at most {} characters".format(COMPATIBILITY_MAX))
            )
    if "metadata" in raw_by_key and not isinstance(_reread(raw_by_key["metadata"]), dict):
        problems.append(Problem("metadata", "must be 'key: value' lines of text"))

    return problems


def suggest_name(text: str) -> str:
    """A valid ``name`` from free text: ``"Report volumes in mL"`` ->
    ``"report-volumes-in-ml"``. Never empty and never over ``NAME_MAX``."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[a-z0-9]+", ascii_text.lower())
    words = [w for w in words if w not in RESERVED_WORDS]
    name = "-".join(words)[:NAME_MAX].strip("-") or "skill"
    return re.sub(r"-{2,}", "-", name)


def body_is_long(doc: SkillDoc) -> bool:
    """True once ``doc.body`` is long enough (``LONG_BODY_LINES`` lines) that loading it
    whole is worth flagging to the author. Not a validation failure."""
    return doc.body.count("\n") + 1 > LONG_BODY_LINES


def to_skill(doc: SkillDoc) -> Skill:
    """``doc`` as the payload the engine expects (``talk2view.skills.Skill``)."""
    return Skill(
        name=doc.name,
        description=doc.description,
        content=doc.body.strip(),
        allowed_tools=doc.allowed_tools,
    )


__all__ = [
    "COMPATIBILITY_MAX",
    "DESCRIPTION_MAX",
    "LONG_BODY_LINES",
    "NAME_MAX",
    "RESERVED_WORDS",
    "Problem",
    "SkillDoc",
    "body_is_long",
    "from_form",
    "parse",
    "render",
    "suggest_name",
    "to_skill",
    "validate",
    "with_fields",
]
