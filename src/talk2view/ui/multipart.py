"""A strict ``multipart/form-data`` reader for ``POST /upload``: one part, and it is a file.

Standard library only (``cgi`` is gone in Python 3.13). Anything but the one shape the page
sends is refused with ``MultipartError`` (400): a ``multipart/form-data`` body with a
boundary, exactly one part, ``Content-Disposition: form-data; name="file"; filename=...``.
The file's bytes are returned exactly as sent.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from email.message import Message
from email.utils import collapse_rfc2231_value
from typing import Dict, FrozenSet, Optional

__all__ = ["MultipartError", "UploadedFile", "parse_upload", "safe_filename"]

#: A part's header block may not be larger than this.
MAX_PART_HEADER_BYTES = 8192
_MAX_FILENAME = 255
#: A giant "extension" is not one; a name this long is treated as having none.
_MAX_EXTENSION = 32

#: RFC 2231 ``filename*=charset''value``: only a charset ``email``'s own decoder cannot
#: misbehave on. ``idna``/``punycode`` can raise ``UnicodeError`` outside the ``LookupError``
#: it catches, and ``undefined`` always raises; excluding everything but these two avoids the
#: whole class rather than naming each bad codec.
_SAFE_FILENAME_CHARSETS: FrozenSet[str] = frozenset({"", "utf-8", "us-ascii"})

#: Unicode's Bidi_Control property in full: the Arabic letter mark (U+061C), the two
#: directional marks (U+200E, U+200F), the embedding/override controls (U+202A-U+202E), and
#: the isolate controls (U+2066-U+2069). Every codepoint is named as an escape rather than
#: embedded literally, so none of them can render as invisible (and indistinguishable from a
#: plain space) in a terminal, editor, or diff.
_BIDI_CONTROLS: FrozenSet[str] = frozenset(
    chr(c)
    for c in [0x061C, 0x200E, 0x200F] + list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A))
)
#: LINE SEPARATOR and PARAGRAPH SEPARATOR: not ``\n``/``\r``, but still break single-line
#: display; named as escapes for the same reason as ``_BIDI_CONTROLS`` above.
_LINE_BREAK_CONTROLS: FrozenSet[str] = frozenset({"\u2028", "\u2029"})
_RESERVED_NAMES: FrozenSet[str] = frozenset({"", ".", ".."})

#: A syntactically valid ``type/subtype`` token pair (RFC 7231 media-type grammar, minus
#: parameters); rejects a comma-joined list, a bare maintype, a control character or an
#: extra ``/``, rather than silently falling back to a default that isn't what was sent.
_MEDIA_TCHAR = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_MEDIA_TYPE_RE = re.compile(r"^%s/%s$" % (_MEDIA_TCHAR, _MEDIA_TCHAR))


class MultipartError(ValueError):
    """The upload body is not the one-file form the page sends."""


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    #: The part's own ``Content-Type``, lower-cased with parameters dropped; ``None`` if
    #: absent, empty, or not a syntactically valid ``type/subtype``.
    content_type: Optional[str]
    data: bytes


def _is_unsafe_filename_char(ch: str) -> bool:
    code = ord(ch)
    if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
        return True
    return ch in _BIDI_CONTROLS or ch in _LINE_BREAK_CONTROLS


def safe_filename(name: str) -> str:
    """The last path component: no control, C1, Bidi_Control or line-break characters,
    never bare ``.``/``..``, at most 255 characters with the extension preserved."""
    base = posixpath.basename(name.replace("\\", "/"))
    base = "".join(ch for ch in base if not _is_unsafe_filename_char(ch)).strip()
    if base in _RESERVED_NAMES:
        return "file"
    if len(base) > _MAX_FILENAME:
        stem, ext = posixpath.splitext(base)
        if not (0 < len(ext) <= _MAX_EXTENSION):
            stem, ext = base, ""
        base = stem[: max(_MAX_FILENAME - len(ext), 0)] + ext
    return base or "file"


def _header_params(name: str, value: str) -> Message:
    message = Message()
    message[name] = value
    return message


def _part_filename(disposition: Message) -> str:
    """The part's ``filename``/``filename*``, refusing an RFC 2231 charset that can make
    ``email``'s own decoder raise outside the ``LookupError`` it already catches."""
    raw = disposition.get_param("filename", None, "content-disposition")
    if raw is None:
        raise MultipartError("the part is not a file")
    if isinstance(raw, tuple):
        charset = (raw[0] or "").lower()
        if charset not in _SAFE_FILENAME_CHARSETS:
            raise MultipartError("unsupported filename charset {!r}".format(raw[0]))
    try:
        filename = collapse_rfc2231_value(raw).strip()
    except (LookupError, UnicodeError, ValueError) as exc:
        raise MultipartError("malformed filename") from exc
    return filename


def _part_content_type(raw: str) -> Optional[str]:
    """The part's declared media type, or ``None`` if absent, empty, or malformed — never a
    guessed default, so a caller falling back to the filename's extension can tell absence
    (or a control character, a comma-joined list, ...) from an actual declaration."""
    if any(_is_unsafe_filename_char(ch) for ch in raw):
        return None
    main = raw.split(";", 1)[0].strip()
    if not _MEDIA_TYPE_RE.match(main):
        return None
    return main.lower()


def parse_upload(body: bytes, content_type: str, *, field_name: str = "file") -> UploadedFile:
    envelope = _header_params("Content-Type", content_type)
    if envelope.get_content_type() != "multipart/form-data":
        raise MultipartError("expected multipart/form-data")
    boundary = envelope.get_param("boundary")
    if not isinstance(boundary, str) or not 1 <= len(boundary) <= 70:
        raise MultipartError("missing or invalid boundary")
    try:
        delimiter = b"--" + boundary.encode("ascii")
    except UnicodeEncodeError:
        raise MultipartError("missing or invalid boundary") from None

    # Indices into ``body`` throughout: no full-body copy is made until the final slice for
    # the returned payload, which is the one copy that cannot be avoided.
    part_start = len(delimiter) + 2
    if not body.startswith(delimiter + b"\r\n"):
        raise MultipartError("body does not start with the boundary")
    close = b"\r\n" + delimiter
    part_end = body.find(close, part_start)
    if part_end < 0:
        raise MultipartError("unterminated part")
    tail_start = part_end + len(close)
    if not body.startswith(b"--", tail_start):
        raise MultipartError("expected exactly one part")
    if body[tail_start + 2:] not in (b"", b"\r\n"):
        raise MultipartError("unexpected data after the closing boundary")

    header_limit = min(part_end, part_start + MAX_PART_HEADER_BYTES + 4)
    split = body.find(b"\r\n\r\n", part_start, header_limit)
    if split < 0:
        raise MultipartError("part headers missing or too large")
    try:
        header_text = body[part_start:split].decode("utf-8")
    except UnicodeDecodeError:
        raise MultipartError("part headers are not UTF-8") from None
    headers: Dict[str, str] = {}
    for line in header_text.split("\r\n"):
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip() or name.lower() in headers:
            raise MultipartError("malformed part header")
        headers[name.lower()] = value.strip()

    disposition = _header_params("Content-Disposition", headers.get("content-disposition", ""))
    if disposition.get_content_disposition() != "form-data":
        raise MultipartError("part is not form-data")
    if disposition.get_param("name", header="content-disposition") != field_name:
        raise MultipartError("expected the field {!r}".format(field_name))
    filename = _part_filename(disposition)
    part_type = _part_content_type(headers["content-type"]) if "content-type" in headers else None
    return UploadedFile(
        filename=safe_filename(filename), content_type=part_type, data=body[split + 4:part_end]
    )
