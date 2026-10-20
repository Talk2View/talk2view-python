"""Auth module — login, signup, anonymous, convert, refresh, logout, auth state."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import secrets
import threading
import time
from typing import Any, Callable, List, Optional, Tuple
from urllib.parse import SplitResult, urlsplit, urlunsplit

from ._loopback import LoopbackListener
from .cancel import CancelToken
from .client import T2VClient
from .errors import (
    ANONYMOUS_UNAVAILABLE_TYPES,
    AnonymousUnavailableError,
    AuthenticationError,
    ProviderSignInError,
    T2VError,
)
from .types import SignupOutcome, TokenResponse, User

logger = logging.getLogger("talk2view.auth")

AuthStateCallback = Callable[[Optional[User]], None]

#: Sent as ``client`` on ``/v1/auth/convert`` (the engine forwards it into the
#: confirmation redirect so the website can tailor its "open the app" step).
CONVERT_CLIENT = "python-sdk"

#: ``get_valid_access_token`` refreshes a token this many seconds before it expires, so a
#: request sent at the boundary does not race the expiry (TS ``TOKEN_EXPIRY_SKEW_SECONDS``).
TOKEN_EXPIRY_SKEW_SECONDS = 30

#: The providers ``sign_in_with_provider`` takes, in the order a sign-in form shows them
#: (Apple asks to be no less prominent than any other).
SIGN_IN_PROVIDERS: Tuple[str, ...] = ("apple", "google")
#: Where every desktop sign-in's start URL points, on the engine, and its handle.
_DESKTOP_START_PATH = "/v1/auth/oauth/desktop/go/"
_START_HANDLE = re.compile(r"[A-Za-z0-9_-]{43}")
#: The engine's static page the listener sends the browser to once it has the code.
_DESKTOP_DONE_PATH = "/v1/auth/oauth/desktop/done"
#: The longest ``timeout`` a caller may pass (the default is 300 s, as long as the engine
#: keeps a sign-in open; more only helps a slow provider screen, such as Apple's 2FA).
MAX_PROVIDER_SIGN_IN_TIMEOUT = 900.0

_PROVIDER_SIGN_IN_MESSAGES = {
    "oauth_cancelled": "Sign-in was cancelled.",
    "oauth_timeout": "Sign-in took too long. Try again.",
    "oauth_denied": "Sign-in was cancelled in the browser.",
    "oauth_failed": "Sign-in didn't finish. Try again.",
    "oauth_browser_unavailable": "Your web browser couldn't be opened.",
    "desktop_sign_in_disabled": "Sign-in with Apple or Google isn't turned on for this app.",
    "oauth_expired": "That sign-in link expired. Try again.",
    "rate_limited": (
        "Too many sign-in attempts from this network. Wait a minute and try again."
    ),
    "engine_url_mismatch": (
        "Sign-in with Apple or Google isn't set up correctly for this app. "
        "Sign in with email and password instead."
    ),
}
#: What the engine may send the loopback instead of a code, and what each means here.
_LOOPBACK_ERRORS = {
    "access_denied": "oauth_denied",
    "desktop_sign_in_disabled": "desktop_sign_in_disabled",
    "expired": "oauth_expired",
}


def _refused_for_too_many(err: T2VError) -> bool:
    """The engine's rate limit (its one 429 handler, type ``rate_limit_exceeded``). Its
    message is the limiter's own and unstable, so it is never shown."""
    return err.status_code == 429 or err.type == "rate_limit_exceeded"


def _origin(parts: SplitResult) -> str:
    """``scheme://host[:port]``: never a path (it holds the start handle) or userinfo."""
    host = parts.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    try:
        port = parts.port
    except ValueError:
        port = None
    return "{}://{}{}".format(parts.scheme, host, "" if port is None else ":{}".format(port))


def _provider_failure(kind: str, status_code: Optional[int] = None) -> ProviderSignInError:
    return ProviderSignInError(_PROVIDER_SIGN_IN_MESSAGES[kind], kind, status_code)


def _pkce_pair() -> Tuple[str, str]:
    """(verifier, S256 challenge): 43 characters each (RFC 7636)."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _jwt_exp(token: str) -> Optional[float]:
    """The ``exp`` claim of a JWT, or None when there is none to read: not a JWT, a payload
    that is not JSON, or an ``exp`` that is not a number. The signature is not checked:
    this only decides when to refresh (TS ``decodeJwtExp``). An integer ``exp`` comes back
    as it is, never converted: one past float range must not overflow."""
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, TypeError, RecursionError):  # binascii.Error is a ValueError
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return exp


def _expiring_soon(token: str) -> bool:
    """TS ``isExpiringSoon``: expired, or within the skew of expiring. An expiry that cannot
    be read proves nothing, so it is False and the reactive 401 path handles the token.

    Compared, not subtracted, so an integer ``exp`` past float range reads as TS reads it
    (``JSON.parse`` gives ±Infinity): one far in the future never expires and its token is
    returned as it is; one far in the past has expired."""
    exp = _jwt_exp(token)
    return exp is not None and exp <= time.time() + TOKEN_EXPIRY_SKEW_SECONDS


def _token(value: object) -> str:
    """A token field: anything but a string reads as absent (``""``)."""
    return value if isinstance(value, str) else ""


def _parse_token_response(data: dict) -> TokenResponse:  # type: ignore[type-arg]
    """Parse a TokenResponse body without assuming a session is present.

    A confirmation-pending signup returns the user with EMPTY (or absent)
    tokens: there is no session until the emailed link is clicked.
    """
    user = None
    raw_user = data.get("user")
    if isinstance(raw_user, dict) and raw_user.get("id"):
        user = User(
            id=raw_user["id"],
            email=raw_user.get("email") or "",
            user_metadata=raw_user.get("user_metadata"),
        )
    return TokenResponse(
        access_token=_token(data.get("access_token")),
        refresh_token=_token(data.get("refresh_token")),
        token_type=data.get("token_type", "bearer"),
        expires_in=data.get("expires_in", 3600),
        user=user,
        user_api_key=data.get("user_api_key"),
        is_anonymous=data.get("is_anonymous") is True,
    )


class T2VAuth:
    """Authentication — login, signup, anonymous demo sessions, logout, auth state."""

    def __init__(self, client: T2VClient) -> None:
        self._client = client
        # The SDK's own state holders (`_add_listener(early=True)`) run before any host
        # listener, which `on_auth_state_change` adds to `_listeners`.
        self._early_listeners: List[AuthStateCallback] = []
        self._listeners: List[AuthStateCallback] = []
        # Bumped, under `_session_lock`, whenever the end-user's choice of account changes
        # the stored session: a permanent session stored (login, signup, a provider
        # sign-in) or logout(). A provider sign-in that finishes after a bump is dropped.
        # Not bumped by a guest session starting (a provider sign-in replaces one) or a
        # rejected refresh clearing the session (nobody chose anything). Re-entrant: an
        # auth listener run from inside the provider sign-in's store may log in or out.
        self._session_lock = threading.RLock()
        self._generation = 0
        # A refresh token the engine rejects clears the session inside the client, on
        # whichever request met it; this makes that an auth-state change like logout().
        client.set_sign_out_hook(self._on_session_rejected)

    def signup(self, email: str, password: str) -> SignupOutcome:
        """Create a permanent account.

        From an anonymous session this CONVERTS it in place (same user id, so
        history and purchases stay): the engine emails a confirmation link and,
        until it is clicked, the session stays anonymous and the outcome has
        ``confirmation_required=True`` and no ``user``. A 409 means the email
        already has an account, so that account is signed into instead.

        Otherwise a plain signup. When the partner requires email confirmation
        the engine returns the user with no session: nothing is stored, no
        auth-state listener fires, and ``is_authenticated()`` stays False until
        the user confirms and logs in. An answer carrying only one of the two
        tokens stores nothing either and raises ``AuthenticationError``: the
        account exists, so the end-user signs in with it.
        """
        if self.is_anonymous():
            try:
                self._client.request(
                    "/v1/auth/convert",
                    method="POST",
                    body={"email": email, "password": password, "client": CONVERT_CLIENT},
                )
            except T2VError as err:
                if err.status_code != 409:
                    raise
                return SignupOutcome(user=self.login(email, password), confirmation_required=False)
            return SignupOutcome(user=None, confirmation_required=True)

        data = self._client.request(
            "/v1/auth/signup",
            method="POST",
            body={"email": email, "password": password},
            requires_auth=False,
        )
        response = _parse_token_response(data)

        if not response.user:
            raise AuthenticationError("Signup succeeded but no user returned")

        if not response.access_token and not response.refresh_token:
            # Email confirmation pending: no session until the link is clicked.
            return SignupOutcome(user=response.user, confirmation_required=True)
        # Checked before anything is stored, as login() does: half a pair is no session.
        if not response.access_token or not response.refresh_token:
            raise AuthenticationError(
                "Your account was created, but no session came back. Sign in to continue."
            )

        self._store_tokens(response)
        self._notify_listeners(response.user)
        return SignupOutcome(user=response.user, confirmation_required=False)

    def login(self, email: str, password: str) -> User:
        """Authenticate with email and password."""
        data = self._client.request(
            "/v1/auth/login",
            method="POST",
            body={"email": email, "password": password},
            requires_auth=False,
        )
        response = _parse_token_response(data if isinstance(data, dict) else {})
        # Checked before anything is stored, as for an anonymous start.
        if not response.user:
            raise AuthenticationError("Login succeeded but no user returned")
        if not response.access_token or not response.refresh_token:
            raise AuthenticationError("Login succeeded but no session returned")
        self._store_tokens(response)
        self._notify_listeners(response.user)
        return response.user

    def start_anonymous(self, captcha_token: Optional[str] = None) -> User:
        """Start an anonymous demo session (no account). Requires partner configuration:
        anonymous access must be enabled for the partner key, and unless the partner has
        turned the captcha requirement off, a Cloudflare Turnstile token must be passed —
        a native desktop host cannot obtain one by itself.

        Raises ``AnonymousUnavailableError`` (``type`` = ``captcha_required``,
        ``captcha_invalid``, ``captcha_unavailable``, ``anonymous_access_disabled`` or
        ``anonymous_daily_cap_reached``) when only signing in or a partner setting can
        help; any other refusal (``anonymous_unavailable`` 503, ``anonymous_error`` 400)
        is a plain ``T2VError`` and may succeed on a retry.
        """
        user = self._start_anonymous_session(captcha_token)
        self._notify_listeners(user)
        return user

    def desktop_sign_in_providers(self) -> List[str]:
        """The providers ``sign_in_with_provider`` can use for this partner, in the order
        a form shows them; empty when the partner has not turned desktop sign-in on. Asks
        the engine each time, with the partner key only (it never starts a guest session).
        Raises ``T2VError`` when the answer cannot be read."""
        data = self._client.request(
            "/v1/auth/oauth/desktop/providers", method="GET", requires_auth=False
        )
        offered = data.get("providers") if isinstance(data, dict) else None
        if not isinstance(offered, list):
            return []
        return [p for p in SIGN_IN_PROVIDERS if p in offered]

    def sign_in_with_provider(
        self,
        provider: str,
        *,
        open_browser: Callable[[str], Any],
        timeout: float = 300.0,
        cancel: Optional[CancelToken] = None,
    ) -> User:
        """Sign in with Apple or Google in the end-user's own web browser.

        Blocks until the browser comes back, ``timeout`` seconds pass, or ``cancel`` is
        cancelled, so call it off the UI thread. ``open_browser(url)`` must open ``url``
        in the system browser, never in an embedded web view (Google refuses those); a
        Qt host passes ``lambda url: host.post(lambda: host.open_external(url))``. It may
        return at once; only a ``False`` return (``webbrowser.open``'s "no browser") or an
        exception counts as failure.

        While it waits, a one-shot listener answers on ``127.0.0.1``; the engine sends the
        browser there with a one-time code, which is traded, with a PKCE verifier that
        never leaves this process, for the session. The browser shows a Talk2View screen
        asking the end-user to confirm first. Success stores the session and notifies the
        ``on_auth_state_change`` listeners, as ``login`` does (a guest session is
        replaced). Raises ``ProviderSignInError`` (``type`` in
        ``PROVIDER_SIGN_IN_ERROR_TYPES``), ``ValueError``/``TypeError`` for bad arguments,
        or the engine's own ``T2VError``. Nothing it handles is logged.

        Another sign-in or a sign-out wins. When ``login``, ``signup`` (with a session)
        or ``logout`` has changed the stored session since this call began, the provider
        session is dropped, even one the engine has already issued, and this raises
        ``oauth_cancelled``; one of them that comes while the provider session is being
        stored waits for that store and its listeners, then replaces it. A guest session
        started meanwhile does not count: this replaces it.

        ``cancel`` also ends the wait at once. A cancel that lands while the session is
        being stored waits for that store and its listeners to finish, so what the
        canceller does next comes after it: never cancel while holding a lock one of your
        ``on_auth_state_change`` listeners takes, and never cancel another sign-in's token
        from an auth listener (with two running, each would wait for the other's store).
        The listener is closed however the call ends.
        """
        if provider not in SIGN_IN_PROVIDERS:
            raise ValueError("provider must be one of {}".format(list(SIGN_IN_PROVIDERS)))
        if not callable(open_browser):
            raise TypeError("open_browser must be callable")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or math.isnan(timeout)
            or not 0 < timeout <= MAX_PROVIDER_SIGN_IN_TIMEOUT
        ):
            raise ValueError("timeout must be a number of seconds, above 0 and at most 900")
        if cancel is not None and cancel.cancelled:
            raise _provider_failure("oauth_cancelled")
        with self._session_lock:
            generation = self._generation
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(32)
        listener = LoopbackListener(state, self._client.base_url + _DESKTOP_DONE_PATH)
        try:
            unregister: Callable[[], None] = (
                cancel.on_cancel(listener.abort) if cancel is not None else (lambda: None)
            )
            try:
                start_url = self._begin_desktop_sign_in(
                    provider, challenge, listener.port, state
                )
                # Cancelled while /begin was in flight: never send the browser anywhere.
                if cancel is not None and cancel.cancelled:
                    raise _provider_failure("oauth_cancelled")
                listener.start()
                try:
                    opened = open_browser(start_url)
                except Exception as exc:
                    logger.warning("open_browser raised %s", type(exc).__name__)
                    raise _provider_failure("oauth_browser_unavailable") from None
                if opened is False:
                    raise _provider_failure("oauth_browser_unavailable")
                outcome = listener.wait(timeout)
            finally:
                unregister()
        finally:
            listener.close()
        if cancel is not None and cancel.cancelled:
            raise _provider_failure("oauth_cancelled")
        if outcome is None:
            raise _provider_failure("oauth_timeout")
        if outcome.code is None:
            raise _provider_failure(_LOOPBACK_ERRORS.get(outcome.error or "", "oauth_failed"))
        return self._redeem_desktop_code(outcome.code, verifier, cancel, generation)

    def _begin_desktop_sign_in(self, provider: str, challenge: str, port: int,
                               state: str) -> str:
        try:
            data = self._client.request(
                "/v1/auth/oauth/desktop/begin",
                method="POST",
                body={"provider": provider, "code_challenge": challenge,
                      "code_challenge_method": "S256", "port": port, "state": state},
                requires_auth=False,
            )
        except T2VError as err:
            if err.type == "desktop_sign_in_disabled":
                raise _provider_failure("desktop_sign_in_disabled", err.status_code) from None
            if _refused_for_too_many(err):
                raise _provider_failure("rate_limited", err.status_code) from None
            raise
        start_url = data.get("start_url") if isinstance(data, dict) else None
        checked = self._engine_start_url(start_url) if isinstance(start_url, str) else None
        if checked is not None:
            return checked
        if isinstance(start_url, str) and self._on_another_engine_url(start_url):
            logger.warning(
                "desktop sign-in: the engine's start page is on %s, not on %s; the engine's "
                "oauth_login_redirect_base must be this SDK's base_url",
                _origin(urlsplit(start_url)), _origin(urlsplit(self._client.base_url)),
            )
            raise _provider_failure("engine_url_mismatch")
        raise _provider_failure("oauth_failed")

    def _on_another_engine_url(self, url: str) -> bool:
        """A well-formed start URL, only on another scheme, host or port than this SDK's
        ``base_url``: the engine's ``oauth_login_redirect_base`` is set to another address
        (a staging or local engine left on the default). Still never opened."""
        parts = urlsplit(url)
        engine = urlsplit(self._client.base_url)
        if urlunsplit(parts) != url or parts.scheme not in ("http", "https") or not parts.netloc:
            return False
        if (parts.scheme, parts.netloc) == (engine.scheme, engine.netloc):
            return False
        moved = urlunsplit((engine.scheme, engine.netloc, parts.path, parts.query,
                            parts.fragment))
        return self._engine_start_url(moved) is not None

    def _engine_start_url(self, url: str) -> Optional[str]:
        """The URL to open, rebuilt from the parts that were checked, or None. Only ever
        the engine's own start page: same scheme, host and port as the engine this client
        talks to, the start path and a well-formed handle, no query or fragment, and https
        unless the engine itself is on this computer (tests, a local engine). A string
        ``urlsplit`` would have to clean up (a leading space or control character, a tab
        or newline anywhere, an empty ``?`` or ``#``) is refused, not cleaned: the opener
        may not clean it the same way.

        The path must start with the start path itself, so an SDK whose ``base_url``
        carries a path prefix (``https://host/api``) is refused (``oauth_failed``): it
        fails closed. Today's engine URL has no path."""
        engine = urlsplit(self._client.base_url)
        parts = urlsplit(url)
        local = engine.hostname in ("127.0.0.1", "localhost")
        valid = (
            urlunsplit(parts) == url
            and parts.scheme == engine.scheme
            and parts.netloc == engine.netloc
            and (parts.scheme == "https" or (parts.scheme == "http" and local))
            and parts.path.startswith(_DESKTOP_START_PATH)
            and _START_HANDLE.fullmatch(parts.path[len(_DESKTOP_START_PATH):]) is not None
            and not parts.query
            and not parts.fragment
        )
        return urlunsplit(parts) if valid else None

    def _redeem_desktop_code(self, code: str, verifier: str, cancel: Optional[CancelToken],
                             generation: int) -> User:
        try:
            data = self._client.request(
                "/v1/auth/oauth/desktop/token",
                method="POST",
                body={"code": code, "code_verifier": verifier},
                requires_auth=False,
            )
        except T2VError as err:
            if err.type == "desktop_sign_in_disabled":
                raise _provider_failure("desktop_sign_in_disabled", err.status_code) from None
            if err.type == "invalid_grant":
                raise _provider_failure("oauth_failed", err.status_code) from None
            if _refused_for_too_many(err):
                raise _provider_failure("rate_limited", err.status_code) from None
            raise
        response = _parse_token_response(data if isinstance(data, dict) else {})
        # Checked before anything is stored, as login() does: half a session is none.
        if not response.user or not response.access_token or not response.refresh_token:
            raise _provider_failure("oauth_failed")
        response.is_anonymous = False
        self._store_unless_superseded(response, cancel, generation)
        return response.user

    def _store_unless_superseded(self, response: TokenResponse,
                                 cancel: Optional[CancelToken], generation: int) -> None:
        """Store and announce ``response``, unless ``cancel`` is cancelled or the session
        changed hands (``_generation`` moved on from ``generation``) first.

        While the browser was out or /token was in flight the end-user may have chosen
        another account (the UI server cancels before it signs in with a password or signs
        out; another host may not). The session is then dropped, never stored over that
        choice. It is not revoked: the client sends no bearer but the stored one
        (``request`` refuses a caller's Authorization), and the dropped session is the
        same person's.

        The check, the store and the notify are one step, under ``_session_lock``: a
        ``login``/``logout`` that follows waits for it rather than landing between the
        check and the store and then being overwritten. A ``cancel()`` from another
        thread runs a closer that waits for the step too. Both locks are re-entrant, so a
        listener that cancels the same token, or logs in or out, from this thread does not
        deadlock."""
        if cancel is None:
            with self._session_lock:
                if self._generation != generation:
                    raise _provider_failure("oauth_cancelled")
                self._store_tokens(response)
                self._notify_listeners(response.user)
            return
        step = threading.RLock()

        def wait_for_the_step() -> None:
            with step:
                pass

        unregister = cancel.on_cancel(wait_for_the_step)
        try:
            with step, self._session_lock:
                if cancel.cancelled or self._generation != generation:
                    raise _provider_failure("oauth_cancelled")
                self._store_tokens(response)
                self._notify_listeners(response.user)
        finally:
            unregister()

    def _start_anonymous_session(
        self,
        captcha_token: Optional[str] = None,
        *,
        store: Optional[Callable[[TokenResponse], None]] = None,
    ) -> User:
        """``start_anonymous`` without notifying the auth-state listeners: the
        caller does that (``Talk2View.ensure_session`` first releases the
        callers waiting on it). ``store`` replaces ``_store_tokens`` for the
        validated answer (``Talk2View`` refuses to store once it is closed)."""
        body = {"captcha_token": captcha_token} if captcha_token else {}
        try:
            data = self._client.request(
                "/v1/auth/anonymous", method="POST", body=body, requires_auth=False
            )
        except T2VError as err:
            if err.type in ANONYMOUS_UNAVAILABLE_TYPES:
                unavailable = AnonymousUnavailableError(
                    str(err), err.type, err.status_code, err.code, err.detail
                )
                unavailable.request_id = err.request_id
                unavailable.retry_after = err.retry_after
                raise unavailable from err
            raise
        response = _parse_token_response(data if isinstance(data, dict) else {})
        # Checked before anything is stored: a half-written session (an empty
        # access token counts) would read as signed in to is_authenticated().
        if not response.user:
            raise AuthenticationError("Anonymous sign-in returned no user")
        if not response.access_token or not response.refresh_token:
            raise AuthenticationError("Anonymous sign-in returned no session")
        response.is_anonymous = True  # the route only ever answers with an anonymous session
        (store if store is not None else self._store_tokens)(response)
        return response.user

    def is_anonymous(self) -> bool:
        """True while the stored session is an anonymous demo session."""
        return self._client.storage.get("is_anonymous") == "true"

    def logout(self) -> None:
        """Sign out the current user.

        The server call is skipped when there is no access token: it could only
        401, and that 401 would trigger a pointless refresh attempt.
        """
        if self._client.storage.get("access_token"):
            try:
                self._client.request("/v1/auth/logout", method="POST")
            except Exception:  # noqa: S110 — logout failures are non-critical
                pass
        with self._session_lock:
            self._generation += 1
            self._client._clear_auth()
        self._notify_listeners(None)

    def get_user(self) -> Optional[User]:
        """Get the current user (from storage)."""
        raw = self._client.storage.get("user")
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return User(
                id=data["id"],
                email=data["email"],
                user_metadata=data.get("user_metadata"),
            )
        except (json.JSONDecodeError, KeyError):
            return None

    def is_authenticated(self) -> bool:
        """Check if a user is currently authenticated (an empty token is not a session)."""
        return bool(self._client.storage.get("access_token"))

    def get_valid_access_token(self, *, force_refresh: bool = False) -> Optional[str]:
        """An access token valid right now, for a request the SDK does not make for you (a
        direct call to another service). TS ``getValidAccessToken``.

        It refreshes first when the stored token is missing, expired, or within
        ``TOKEN_EXPIRY_SKEW_SECONDS`` of expiring; ``force_refresh=True`` refreshes whatever
        is stored (use it after a direct request still got a 401). A token whose expiry
        cannot be read is returned as it is, as in TS: the reactive 401 path handles it. So
        is one whose ``exp`` is too large for a float (TS reads it as Infinity).

        The refresh is the one the SDK's own 401 handling makes, under the same lock, so a
        direct call and a request in flight never both spend the rotating refresh token:
        a caller that waited out another's refresh gets the new token without a request.

        None when the session is dead (no refresh token, or the engine rejected it): the
        stored session is then cleared and, when there was one, the
        ``on_auth_state_change`` listeners hear ``None`` on this thread, as for any rejected
        refresh. A transient failure (the network, a 409, a 429, a 5xx, a 2xx answer that is
        not JSON) keeps the session and returns the stored token unchanged, possibly
        expired, so the caller can go ahead or retry; with no access token stored that is
        None as well, the session still in place. A 2xx answer that lacks either token
        raises ``AuthenticationError`` and stores nothing, and a token store that cannot be
        read raises its own error: neither is ever read as a token. A refresh the engine
        refuses because of the partner key raises ``PartnerKeyError`` and keeps the session:
        the integration's key is not the end-user's sign-in.
        """
        current = self._client.storage.get("access_token") or None
        if current is not None and not force_refresh and not _expiring_soon(current):
            return current
        result = self._client._try_refresh_token(current)
        if result == "refreshed":
            return self._client.storage.get("access_token") or None
        if result == "invalid":
            # After the refresh lock is released, as every other rejected refresh.
            self._client._sign_out_rejected()
            return None
        return current

    def on_auth_state_change(self, callback: AuthStateCallback) -> Callable[[], None]:
        """Subscribe to auth state changes. Returns an unsubscribe function.

        ``None`` means signed out: after ``logout()``, and when the engine rejects the
        refresh token (the request that met the rejection raises ``AuthenticationError``
        "Session expired", and the stored session is already cleared).

        The callback runs on whichever thread changed the auth state: the caller of
        ``login()``/``logout()``/``signup()``, or — for an anonymous start by
        ``Talk2View``, or a rejected refresh — the thread that made that request, which
        can be one of the SDK's own: a ``ChatController`` worker, the ``t2v-warm-up``
        daemon thread or a skills debounce timer. Qt/bpy hosts must marshal any UI work
        to their main thread (for example with ``run_on_main`` and the dispatcher they
        gave ``Talk2View``). A callback that raises is logged; the others still run.
        The SDK's own listeners (``Talk2View``'s, a ``ChatController``'s) run first.
        """
        return self._add_listener(callback)

    def _add_listener(
        self, callback: AuthStateCallback, *, early: bool = False
    ) -> Callable[[], None]:
        """``on_auth_state_change``, plus ``early`` for the SDK's own state holders
        (``Talk2View``'s session hook, a ``ChatController``): an early listener runs before
        every host listener, early ones in the order they were added. A host listener
        that blocks, or that changes the auth state from inside its delivery, then cannot
        run ahead of them: they have already reset for the change it is reacting to."""
        listeners = self._early_listeners if early else self._listeners
        listeners.append(callback)

        def unsubscribe() -> None:
            try:
                listeners.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def _store_tokens(self, response: TokenResponse) -> None:
        # The client's tokens lock too: a refresh answering meanwhile writes its tokens
        # only over the session it refreshed, never over this one (it checks under it).
        with self._session_lock, self._client._tokens_lock:
            if not response.is_anonymous:
                self._generation += 1  # the end-user chose an account (see __init__)
            # The flag first: a reader on another thread (a host polling is_authenticated,
            # the page) reads storage without this lock, and a guest's token seen before its
            # flag reads as a signed-in account (an account's, with the old flag, as a guest).
            self._client.storage.set("is_anonymous", "true" if response.is_anonymous else "false")
            self._client.storage.set("access_token", response.access_token)
            self._client.storage.set("refresh_token", response.refresh_token)
            if response.user:
                self._client.storage.set("user", json.dumps({
                    "id": response.user.id,
                    "email": response.user.email,
                    "user_metadata": response.user.user_metadata,
                }))
            if response.user_api_key:
                self._client.storage.set("user_api_key", response.user_api_key)

    def _notify_listeners(self, user: Optional[User]) -> None:
        # Each on its own: one that raises must not keep the rest (a ChatController
        # resetting approval consent among them) from hearing about the change.
        for listener in list(self._early_listeners) + list(self._listeners):
            try:
                listener(user)
            except Exception:
                logger.exception("on_auth_state_change listener raised")

    def _on_session_rejected(self) -> None:
        """The client's sign-out hook: a rejected refresh token has cleared the session."""
        self._notify_listeners(None)
