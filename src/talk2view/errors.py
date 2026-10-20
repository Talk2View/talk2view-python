"""Talk2View SDK error classes."""

from __future__ import annotations

import email.utils
import math
import time
from typing import Callable, Dict, FrozenSet, Optional

# Shown when the engine lost the chat session mid-turn: nothing is resent,
# because the tool handler has already run on this side.
SESSION_LOST_MESSAGE = (
    "The chat session ended before that step could be confirmed, so the assistant "
    "never got the result. It may already have been applied — check before sending "
    "your message again."
)

# Shown when the end-user denied the call, so nothing ran and there's nothing to check.
SESSION_LOST_AFTER_DENY_MESSAGE = (
    "The chat session ended before that step could be confirmed, so the assistant "
    "never got your answer. Send your message again."
)


class T2VError(Exception):
    """Base error class for Talk2View SDK errors.

    ``message`` is always the user-safe summary (the server's structured
    ``error.message`` plus the HTTP status). The server's raw ``detail`` (stack
    traces, DB errors, file paths, library versions) and any opaque non-JSON
    response body are kept off ``message`` so they cannot leak to end users. A
    *structured* detail (e.g. a FastAPI 422 validation list, JSON-serialized) is
    preserved on the separate ``detail`` attribute for debugging only. ``code``
    is the engine's optional machine-readable sub-code; it is never folded into
    ``message`` either.

    ``request_id`` is the engine's id for the failed request (also its
    ``X-Request-Id`` header): quote it when you contact support. ``retry_after``
    is the number of seconds to wait from a ``Retry-After`` header, sent on rate
    limits and some temporary outages. Both are ``None`` when the engine sent
    nothing, and on errors raised in the SDK itself.
    """

    request_id: Optional[str] = None
    retry_after: Optional[int] = None

    def __init__(
        self,
        message: str,
        type: Optional[str] = None,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.type = type if type is not None else "sdk_error"
        self.status_code = status_code
        self.code = code
        self.detail = detail


class AuthenticationError(T2VError):
    """Raised when authentication fails (invalid credentials, expired token)."""

    def __init__(
        self,
        message: str,
        type: Optional[str] = "authentication_error",
        status_code: Optional[int] = 401,
        code: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(message, type, status_code, code, detail)


class PartnerKeyError(T2VError):
    """Raised when the partner API key is invalid or missing."""

    def __init__(
        self,
        message: str,
        type: Optional[str] = "partner_key_error",
        status_code: Optional[int] = 401,
        code: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(message, type, status_code, code, detail)


class SessionError(T2VError):
    """Raised when session operations fail."""

    def __init__(
        self,
        message: str,
        type: Optional[str] = "session_error",
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(message, type, status_code, code, detail)


class NetworkError(T2VError):
    """Raised when a network request fails."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "network_error")


#: ``/v1/auth/anonymous`` refusals that only signing in (or a partner setting) can fix.
#: The same set as the TypeScript SDK's ``ANONYMOUS_UNAVAILABLE_TYPES``. Deliberately
#: NOT in it: ``anonymous_unavailable`` (503, a momentary backend blip) and
#: ``anonymous_error`` (400) — those are plain ``T2VError``s and a later call may succeed.
ANONYMOUS_UNAVAILABLE_TYPES: FrozenSet[str] = frozenset(
    {
        "anonymous_access_disabled",
        "anonymous_daily_cap_reached",
        "captcha_required",
        "captcha_invalid",
        "captcha_unavailable",
    }
)


class AnonymousUnavailableError(T2VError):
    """``start_anonymous()`` was refused for a reason a retry cannot fix: the partner
    does not offer anonymous access (``anonymous_access_disabled``), its daily cap is
    reached (``anonymous_daily_cap_reached``), or a captcha is required, was rejected, or
    cannot be verified (``captcha_required`` / ``captcha_invalid`` /
    ``captcha_unavailable``). ``type`` carries which. Raised by ``start_anonymous`` only,
    so ``error_for_type``'s map stays identical to the TypeScript SDK's."""

    def __init__(
        self,
        message: str,
        type: Optional[str] = "anonymous_access_disabled",
        status_code: Optional[int] = 403,
        code: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(message, type, status_code, code, detail)


#: ``ProviderSignInError.type`` values.
PROVIDER_SIGN_IN_ERROR_TYPES: FrozenSet[str] = frozenset(
    {
        "oauth_cancelled",  # the CancelToken was cancelled
        "oauth_timeout",  # the browser did not come back in time
        "oauth_denied",  # the end-user (or the provider) said no in the browser
        "oauth_failed",  # the engine could not finish it; trying again may work
        "oauth_browser_unavailable",  # open_browser raised or returned False
        "desktop_sign_in_disabled",  # the partner has not turned desktop sign-in on
        "oauth_expired",  # Continue came too late: the engine's start link had expired
        "rate_limited",  # too many sign-ins started from this network; wait a minute
        "engine_url_mismatch",  # the engine's start page is not on this SDK's base_url
    }
)


class ProviderSignInError(T2VError):
    """``sign_in_with_provider`` ended without a session. ``type`` says why (one of
    ``PROVIDER_SIGN_IN_ERROR_TYPES``) and the message is safe to show the end-user. Other
    failures (a bad partner key, the network) are raised as their own ``T2VError``."""

    def __init__(
        self,
        message: str,
        type: Optional[str] = "oauth_failed",
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(message, type, status_code, code, detail)


# The engine's error type for each class we publish. A failed request is raised
# as the matching subclass so ``except AuthenticationError`` works; everything
# else stays a plain T2VError.
_ERROR_CLASS_BY_TYPE: Dict[str, Callable[..., T2VError]] = {
    "authentication_error": AuthenticationError,
    "partner_key_error": PartnerKeyError,
    # The engine's dedicated SessionNotFoundError type. A dead chat session
    # currently 404s with the generic ``not_found``, which is deliberately NOT
    # mapped: the same type covers a missing skill, attachment or partner.
    # Recovery keys on the type (see ``is_session_gone``), not on this class.
    "session_not_found": SessionError,
}


def error_for_type(
    message: str,
    type: Optional[str],
    status_code: Optional[int] = None,
    code: Optional[str] = None,
    detail: Optional[str] = None,
    *,
    request_id: Optional[str] = None,
    retry_after: Optional[int] = None,
) -> T2VError:
    """Build the most specific error class for an engine error type."""
    cls = _ERROR_CLASS_BY_TYPE.get(type) if type else None
    err = (cls or T2VError)(message, type, status_code, code, detail)
    err.request_id = request_id
    err.retry_after = retry_after
    return err


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[int]:
    """Seconds from a ``Retry-After`` header: a number of seconds or an HTTP date.

    ``None`` when the header is absent or unreadable.
    """
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    current = time.time() if now is None else now
    return max(0, math.ceil(when.timestamp() - current))


def is_session_gone(err: object) -> bool:
    """True for the engine's "Session not found".

    Chat sessions live in the engine's memory, so a deploy, a restart or an
    eviction loses them. ``not_found`` is the type both ``/messages`` and
    ``/resume`` raise today; ``session_not_found`` is the engine's dedicated
    type, accepted too so a future rename does not silently stop recovery.
    """
    return (
        isinstance(err, T2VError)
        and err.status_code == 404
        and err.type in ("not_found", "session_not_found")
    )
