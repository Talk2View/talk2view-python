"""Shared attachment constraints — the single source of truth for what the SDK uploads.

Mirrors the TypeScript SDK's ``src/constants.ts`` and the engine's own list of allowed
attachment types. The guard runs before any request so a wrong file type is
a clear local error instead of a bare server 415.
"""

from __future__ import annotations

from typing import FrozenSet

#: MIME allow-list (mirrors the server). Also usable as a file dialog filter.
ATTACHMENT_ACCEPT = "image/png,image/jpeg,image/webp,image/gif,application/pdf"

#: The same allow-list as a set, for membership checks.
ALLOWED_ATTACHMENT_MIME: FrozenSet[str] = frozenset(ATTACHMENT_ACCEPT.split(","))

#: Human-readable form of the allow-list, for hints and error messages.
ATTACHMENT_TYPES_LABEL = "PNG, JPEG, WebP, GIF, or PDF"


def is_allowed_attachment_type(mime_type: str) -> bool:
    """True when ``mime_type`` is one the SDK (and the engine) accepts."""
    return mime_type in ALLOWED_ATTACHMENT_MIME


__all__ = [
    "ALLOWED_ATTACHMENT_MIME",
    "ATTACHMENT_ACCEPT",
    "ATTACHMENT_TYPES_LABEL",
    "is_allowed_attachment_type",
]
