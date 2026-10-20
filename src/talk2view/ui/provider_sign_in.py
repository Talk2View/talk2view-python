"""One provider sign-in at a time for the UI server (``oauthStart`` / ``oauthCancel``).

``T2VAuth.sign_in_with_provider`` blocks until the browser comes back, up to five
minutes, so it cannot run on the router's worker: that thread runs commands strictly in
order, and ``oauthCancel`` would wait behind it. It runs on its own ``t2v-ui-oauth``
thread and reports through ``AppState``: ``auth.oauthPending`` while it waits, then
``auth.oauthError`` if it ends without a session (a cancel is not an error). A success
needs no report of its own: the auth slice shows the account, recorded by ``AppState``'s
auth listener before the pending mark is cleared. The browser is opened through the host
on its main thread (``host.post``), as ``openLink`` opens a link.

Lock order: ``ProviderSignIn._lock``, then ``AppState._lock`` (``set_oauth``). The
pending mark is set and cleared under ``_lock``, so an attempt that ends can never clear
the mark of the next one.

``cancel()`` and ``close()`` cancel the token with NO lock held. A cancel that lands while
``sign_in_with_provider`` stores the session waits for the store and its auth listeners
(``AppState._on_auth`` among them, which takes ``AppState._lock``), so a caller holding a
lock one of those listeners takes would deadlock. The router calls them from its handlers,
which hold nothing.

Each attempt's token is cancelled by ONE caller: the first ``cancel()`` or ``close()``
claims it under ``_lock``, and a later one leaves it alone. A second cancel would wait for
the first one's closers (the token's run lock), and the first can be waiting for the store
on the thread that makes the second: a host auth listener that stops the server
(``UIServer.stop`` -> ``close()``) while the router's ``login`` cancels. A cancel that
lands during the store does not undo the sign-in: the session is stored, and what the
canceller does next (``login``, ``logout``) replaces it.

The store holds the SDK's store step, then ``T2VAuth._session_lock``, while the auth
listeners run; ``login`` and ``logout`` take ``_session_lock`` too. Neither
``ProviderSignIn._lock`` nor ``AppState._lock`` is ever held while taking either, and a
listener that stops the server only takes ``ProviderSignIn._lock`` briefly (a claim),
so the whole order is ``step`` -> ``_session_lock`` -> ``ProviderSignIn._lock`` ->
``AppState._lock`` -> the hub's lock.
"""

from __future__ import annotations

import functools
import logging
import threading
from typing import TYPE_CHECKING, Dict, Optional

from ..cancel import CancelToken
from ..errors import ProviderSignInError, T2VError
from .host import HostAdapter

if TYPE_CHECKING:
    from .. import Talk2View
    from .app_state import AppState

__all__ = ["ProviderSignIn"]

logger = logging.getLogger("talk2view.ui")

_INTERNAL_ERROR = "Something went wrong."
#: How long to wait for the host's main thread to report whether the browser opened.
OPEN_WAIT_S = 10.0


class ProviderSignIn:
    def __init__(self, app: "AppState", host: HostAdapter) -> None:
        self._app = app
        self._host = host
        self._lock = threading.Lock()
        self._cancel: Optional[CancelToken] = None
        self._claimed: Optional[CancelToken] = None  # the token one caller is cancelling
        self._thread: Optional[threading.Thread] = None
        self._closed = False

    def start(self, t2v: "Talk2View", provider: str) -> bool:
        """Start one; False when one is already waiting, or this is closed."""
        with self._lock:
            if self._closed or self._cancel is not None:
                return False
            token = CancelToken()
            self._cancel = token
            thread = threading.Thread(
                target=self._run, args=(t2v, provider, token), name="t2v-ui-oauth", daemon=True
            )
            self._thread = thread
            self._app.set_oauth(pending=provider, error=None)
            # Started under the lock: a close() that follows joins a started thread.
            thread.start()
        return True

    def cancel(self) -> None:
        """Cancel the waiting one, if any. Returns at once, unless the session is being
        stored just then: it waits for the store and its auth listeners, so call it with
        no lock held (see the module docstring)."""
        with self._lock:
            token = self._claim_locked()
        if token is not None:
            token.cancel()

    def close(self, timeout: float = 2.0) -> None:
        """Cancel, refuse new ones, and wait up to ``timeout`` for the thread. Idempotent."""
        with self._lock:
            self._closed = True
            token, thread = self._claim_locked(), self._thread
        if token is not None:
            token.cancel()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _claim_locked(self) -> Optional[CancelToken]:
        """The waiting attempt's token for this caller to cancel, or None: nothing waits,
        or another caller has claimed it (and cancels it, or already has)."""
        token = self._cancel
        if token is None or token is self._claimed or token.cancelled:
            return None
        self._claimed = token
        return token

    def _open_browser(self, token: CancelToken, url: str) -> bool:
        """Open ``url`` on the host's main thread (``host.post``) and wait, briefly, for
        what ``open_external`` says: ``False`` or an exception means no browser opened
        (``webbrowser.open`` with none), reported at once instead of after the sign-in's
        timeout. A main thread too busy to answer within ``OPEN_WAIT_S`` is taken to
        open it later; a cancel ends the wait at once (the listener is aborted by then)."""
        host = self._host
        answered = threading.Event()
        result: Dict[str, object] = {}

        def open_on_main() -> None:
            try:
                if token.cancelled:  # a busy main thread got to it after a cancel or stop
                    return
                result["opened"] = host.open_external(url) is not False
            except Exception as exc:
                logger.warning("the host could not open the browser: %s", type(exc).__name__)
                result["opened"] = False
            finally:
                answered.set()

        unregister = token.on_cancel(answered.set)
        try:
            host.post(open_on_main)
            if not answered.wait(OPEN_WAIT_S):
                return True
        finally:
            unregister()
        return result.get("opened") is not False

    def _run(self, t2v: "Talk2View", provider: str, token: CancelToken) -> None:
        error: Optional[Dict[str, str]] = None
        try:
            t2v.auth.sign_in_with_provider(
                provider, open_browser=functools.partial(self._open_browser, token), cancel=token
            )
        except ProviderSignInError as exc:
            if exc.type == "desktop_sign_in_disabled":
                self._app.note_desktop_sign_in_disabled()
            if exc.type != "oauth_cancelled":
                error = {"type": exc.type, "message": str(exc)}
        except T2VError as exc:
            error = {"type": exc.type or "engine_error", "message": str(exc)}
        except Exception as exc:
            # The type alone: an exception's text could quote a URL.
            logger.error("provider sign-in failed: %s", type(exc).__name__)
            error = {"type": "internal_error", "message": _INTERNAL_ERROR}
        finally:
            with self._lock:
                if self._cancel is token:
                    self._cancel = None
                self._app.set_oauth(pending=None, error=error)
