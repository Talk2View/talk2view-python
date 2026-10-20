"""The chat page's static files: where they live, how a URL maps onto them, their types.

The bundle is built by ``embed/`` into ``talk2view/ui/static/``. That
directory is git-ignored and carried into wheels and sdists by hatch ``artifacts``, so a
PyPI install has it and a git checkout does not until the page is built:
``StaticAssetsMissingError`` says how.
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Union
from urllib.parse import unquote, urlsplit

__all__ = [
    "BUILD_INSTRUCTIONS",
    "CONTENT_TYPES",
    "StaticAssetsMissingError",
    "content_type_for",
    "default_static_dir",
    "require_static_dir",
    "resolve_static",
]

#: Explicit map: on Windows ``mimetypes`` reads the registry and can answer ``text/plain``
#: for ``.js``, which makes Chromium refuse a module script (found on Slicer's Chromium 83).
CONTENT_TYPES: Dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}

BUILD_INSTRUCTIONS = (
    "Wheels from PyPI include the chat page. In a git checkout, build it with "
    "`cd embed && npm ci && npm run build`, which writes the bundle to "
    "src/talk2view/ui/static/, or pass UIServer(static_dir=...) "
    "pointing at a built bundle."
)

#: Windows reserved device names (case-insensitive, with or without an extension). Refused
#: as a path segment even off Windows: a bundle should never legitimately contain one, and
#: a wheel built here may still be served from a Windows host later. Includes the console
#: I/O device names (CONIN$, CONOUT$) and the superscript-digit spelling of COM/LPT 1-3
#: (U+00B9/U+00B2/U+00B3) that Windows also treats as reserved alongside the plain digits.
_SUPERSCRIPT_DIGITS = "¹²³"
_RESERVED_STEMS: FrozenSet[str] = frozenset(
    {"con", "nul", "prn", "aux", "conin$", "conout$"}
    | {"com%d" % digit for digit in range(1, 10)}
    | {"lpt%d" % digit for digit in range(1, 10)}
    | {"com%s" % digit for digit in _SUPERSCRIPT_DIGITS}
    | {"lpt%s" % digit for digit in _SUPERSCRIPT_DIGITS}
)


class StaticAssetsMissingError(RuntimeError):
    """The chat page is not built (``index.html`` is missing from the static directory)."""

    def __init__(self, static_dir: Path) -> None:
        self.static_dir = static_dir
        super().__init__(
            "The Talk2View chat page is not built: {} is missing. {}".format(
                static_dir / "index.html", BUILD_INSTRUCTIONS
            )
        )


def default_static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


def require_static_dir(static_dir: Optional[Union[str, "os.PathLike[str]"]] = None) -> Path:
    """The resolved static directory. Raises ``StaticAssetsMissingError`` without an index."""
    directory = Path(static_dir) if static_dir is not None else default_static_dir()
    if not (directory / "index.html").is_file():
        raise StaticAssetsMissingError(directory)
    return directory.resolve()


def _segment_is_safe(segment: str) -> bool:
    """A single path segment that is not a hidden file, a drive letter, or a reserved name."""
    if not segment or segment.startswith("."):
        return False
    if ":" in segment:
        return False
    # Windows strips trailing spaces and dots from a component before resolving it, so
    # "CON .txt" and "CON..." are the reserved name "CON" as far as the filesystem cares.
    stem = segment.split(".", 1)[0].rstrip(" .")
    return stem.lower() not in _RESERVED_STEMS


def resolve_static(static_dir: Union[str, "os.PathLike[str]"], raw_path: str) -> Optional[str]:
    """Map a request path onto a file inside ``static_dir``; ``None`` when there is none.

    ``/`` is ``index.html``. Traversal is refused whichever way it is spelled
    (``..``, percent-encoded, backslashes, NUL), and so is a symlink that resolves outside
    the directory, a dotfile (``/.env``), and a segment that is a Windows reserved device
    name or contains ``:``. A target ``urlsplit`` cannot parse maps to no file, not a raised
    exception.
    """
    try:
        path = unquote(urlsplit(raw_path).path)
    except ValueError:
        return None
    if "\\" in path or "\x00" in path:
        return None
    path = posixpath.normpath(path)
    if path in ("/", "."):
        path = "/index.html"
    rel = path.lstrip("/")
    if rel.startswith("..") or "/../" in "/" + rel + "/":
        return None
    segments = rel.split("/")
    if not all(_segment_is_safe(segment) for segment in segments):
        return None
    root = os.path.realpath(static_dir)
    full = os.path.realpath(os.path.join(root, *segments))
    if full != root and not full.startswith(root + os.sep):
        return None
    return full if os.path.isfile(full) else None


def content_type_for(path: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
