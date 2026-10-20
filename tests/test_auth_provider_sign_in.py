"""T2VAuth.sign_in_with_provider: Apple and Google through the end-user's own browser,
a loopback hand-off and PKCE."""

from __future__ import annotations

import base64
import hashlib
import logging
import socket
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

import pytest

from talk2view import ProviderSignInError, T2VError, Talk2View, User
from talk2view.cancel import CancelToken
from talk2view.storage import MemoryStorage

from .fake_engine import FakeEngine, browse, follow_in_browser, open_check_screen


@pytest.fixture
def t2v(engine: FakeEngine) -> Iterator[Talk2View]:
    engine.desktop_providers = ["apple", "google"]
    client = Talk2View("pk_test", base_url=engine.url())
    yield client
    client.close()


def _sign_in(t2v: Talk2View, provider: str, **kwargs: Any) -> User:
    """Every call in this file: a 10 s sign-in timeout unless the test says otherwise, run
    on its own thread and given up on after 25 s, so a regression (a wait that never
    ends, a deadlock) fails in seconds instead of waiting out the 300 s default."""
    kwargs.setdefault("timeout", 10)
    result: List[Any] = []

    def run() -> None:
        try:
            result.append(t2v.auth.sign_in_with_provider(provider, **kwargs))
        except BaseException as exc:  # handed to the test's thread
            result.append(exc)

    signer = threading.Thread(target=run, name="test-sign-in", daemon=True)
    signer.start()
    signer.join(25)  # no test here passes a timeout above 10
    if signer.is_alive():
        pytest.fail("sign_in_with_provider did not return")
    if isinstance(result[0], BaseException):
        raise result[0]
    return result[0]


def _bodies(engine: FakeEngine, suffix: str) -> List[Any]:
    return [body for path, body in engine.desktop_requests if path.endswith(suffix)]


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _nothing_listens(port: int) -> bool:
    """The listener is closed. (Not a bind test: after a real browser connection the
    listener's side of it sits in TIME_WAIT, and a bind without SO_REUSEADDR then fails on
    macOS even though nothing listens.)"""
    with socket.socket() as s:
        try:
            s.connect(("127.0.0.1", port))
        except ConnectionRefusedError:
            return True
    return False


def test_a_sign_in_stores_the_session_and_tells_the_listeners(t2v: Talk2View,
                                                              engine: FakeEngine) -> None:
    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(heard.append)
    user = _sign_in(t2v, "apple", open_browser=follow_in_browser)
    assert user.id == "u-desktop"
    assert t2v.auth.is_authenticated() and not t2v.auth.is_anonymous()
    assert [u.id if u else None for u in heard] == ["u-desktop"]
    (begun,) = _bodies(engine, "/begin")
    (redeemed,) = _bodies(engine, "/token")
    assert begun["provider"] == "apple" and begun["code_challenge_method"] == "S256"
    assert 1024 <= begun["port"] <= 65535 and len(begun["state"]) == 43
    assert _s256(redeemed["code_verifier"]) == begun["code_challenge"]
    assert _nothing_listens(begun["port"])


def test_the_browser_gets_the_engines_start_url(t2v: Talk2View, engine: FakeEngine) -> None:
    opened: List[str] = []

    def browser(url: str) -> int:
        opened.append(url)
        return follow_in_browser(url)

    _sign_in(t2v, "google", open_browser=browser)
    assert len(opened) == 1
    assert opened[0].startswith(engine.url().rstrip("/") + "/v1/auth/oauth/desktop/go/")


@pytest.mark.parametrize("answer, kind", [("access_denied", "oauth_denied"),
                                          ("server_error", "oauth_failed"),
                                          ("expired", "oauth_expired"),
                                          ("desktop_sign_in_disabled",
                                           "desktop_sign_in_disabled"),
                                          ("something_new", "oauth_failed")])
def test_a_refusal_in_the_browser_signs_no_one_in(t2v: Talk2View, engine: FakeEngine,
                                                  answer: str, kind: str) -> None:
    engine.desktop_answer = answer
    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(heard.append)
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=follow_in_browser)
    assert caught.value.type == kind
    assert not t2v.auth.is_authenticated() and heard == []


def test_timeout(t2v: Talk2View, engine: FakeEngine) -> None:
    started = time.monotonic()
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=lambda url: None, timeout=0.3)
    assert caught.value.type == "oauth_timeout"
    assert time.monotonic() - started < 3
    assert _nothing_listens(_bodies(engine, "/begin")[0]["port"])


def test_cancel_while_the_browser_is_out(t2v: Talk2View) -> None:
    token = CancelToken()
    threading.Timer(0.2, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=lambda url: None, cancel=token)
    assert caught.value.type == "oauth_cancelled"
    assert time.monotonic() - started < 3


def test_a_cancelled_token_never_reaches_the_engine(t2v: Talk2View, engine: FakeEngine) -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=follow_in_browser, cancel=token)
    assert caught.value.type == "oauth_cancelled"
    assert engine.desktop_requests == []


def test_browser_unavailable(t2v: Talk2View, engine: FakeEngine,
                             caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=lambda url: False)
    assert caught.value.type == "oauth_browser_unavailable"

    def broken(url: str) -> None:
        raise RuntimeError("no display at " + url)

    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=broken)
    assert caught.value.type == "oauth_browser_unavailable"
    assert "no display" not in str(caught.value)  # the host's text could hold the URL
    assert "no display" not in caplog.text
    for begun in _bodies(engine, "/begin"):
        assert _nothing_listens(begun["port"])


def test_the_switch_off_is_its_own_error(t2v: Talk2View, engine: FakeEngine) -> None:
    engine.desktop_providers = []
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=follow_in_browser)
    assert caught.value.type == "desktop_sign_in_disabled"
    assert caught.value.status_code == 403


_GO = "/v1/auth/oauth/desktop/go/"
_HANDLE = "h" * 43


def _other_port(base: str) -> str:
    host, port = base.rsplit(":", 1)
    return "{}:{}{}{}".format(host, int(port) + 1, _GO, _HANDLE)


@pytest.mark.parametrize("start_url", [
    "https://evil.example" + _GO + _HANDLE,
    "{engine}/elsewhere/" + _HANDLE,
    "{engine}" + _GO + _HANDLE + "?next=https://evil.example",
    "{engine}" + _GO + _HANDLE + "#x",
    "javascript:alert(1)",
    # What urlsplit would clean up, and an opener might not: refused, not cleaned.
    chr(0) + "{engine}" + _GO + _HANDLE,
    " {engine}" + _GO + _HANDLE,
    lambda base: base.replace("localhost", "local" + chr(9) + "host") + _GO + _HANDLE,
    "{engine}" + _GO + _HANDLE + "?",
    "{engine}" + _GO + _HANDLE + "#",
    # Not the handle, not this engine's scheme or port.
    "{engine}" + _GO + "../../../elsewhere",
    "{engine}" + _GO + _HANDLE + "/more",
    lambda base: base.replace("http://", "https://") + _GO + _HANDLE,
    _other_port,
])
def test_a_start_url_elsewhere_is_never_opened(t2v: Talk2View, engine: FakeEngine,
                                               start_url: Any) -> None:
    base = engine.url().rstrip("/")
    url = start_url(base) if callable(start_url) else start_url.format(engine=base)
    engine.desktop_start_url = url
    opened: List[str] = []
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=opened.append, timeout=2)
    assert caught.value.type in ("oauth_failed", "engine_url_mismatch")
    assert opened == []


@pytest.mark.parametrize("start_url", [
    "https://evil.example" + _GO + _HANDLE,
    lambda base: base.replace("http://", "https://") + _GO + _HANDLE,
    _other_port,
])
def test_a_start_page_on_another_engine_address_says_so(
    t2v: Talk2View, engine: FakeEngine, caplog: pytest.LogCaptureFixture, start_url: Any,
) -> None:
    # A well-formed start page, only on another address: the engine's
    # oauth_login_redirect_base is not this SDK's base_url (a staging or local engine
    # left on the default). Refused all the same, and said plainly.
    caplog.set_level(logging.DEBUG, logger="talk2view")
    base = engine.url().rstrip("/")
    engine.desktop_start_url = start_url(base) if callable(start_url) else start_url
    opened: List[str] = []
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=opened.append, timeout=2)
    assert caught.value.type == "engine_url_mismatch" and opened == []
    assert "set up correctly" in str(caught.value)
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name != "werkzeug")
    assert "oauth_login_redirect_base" in logged and _HANDLE not in logged


@pytest.mark.parametrize("start_url", [
    "{engine}/elsewhere/" + _HANDLE,
    "{engine}" + _GO + _HANDLE + "?next=https://evil.example",
    "javascript:alert(1)",
    " {engine}" + _GO + _HANDLE,
    "https://evil.example" + _GO + "short",
    "https://evil.example" + _GO + _HANDLE + "#x",
])
def test_a_malformed_start_page_is_a_failed_sign_in(t2v: Talk2View, engine: FakeEngine,
                                                   start_url: str) -> None:
    engine.desktop_start_url = start_url.format(engine=engine.url().rstrip("/"))
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=lambda url: None, timeout=2)
    assert caught.value.type == "oauth_failed"


@pytest.mark.parametrize("base, url, expected", [
    ("https://engine.example", "https://engine.example" + _GO + _HANDLE, True),
    ("http://engine.example", "http://engine.example" + _GO + _HANDLE, False),  # not local
    ("https://engine.example", "http://engine.example" + _GO + _HANDLE, False),
    ("https://engine.example", "https://Engine.example" + _GO + _HANDLE, False),
    ("https://engine.example/api", "https://engine.example/api" + _GO + _HANDLE, False),
])
def test_which_start_urls_are_the_engines(base: str, url: str, expected: bool) -> None:
    with Talk2View("pk_test", base_url=base) as client:
        checked = client.auth._engine_start_url(url)
    assert checked == (url if expected else None)


@pytest.mark.parametrize("kwargs, error", [
    ({"provider": "github"}, ValueError),
    ({"timeout": 0}, ValueError),
    ({"timeout": -1}, ValueError),
    ({"timeout": True}, ValueError),
    ({"timeout": float("nan")}, ValueError),
    ({"timeout": 901}, ValueError),
    ({"open_browser": "firefox"}, TypeError),
])
def test_arguments_are_checked_before_anything_happens(t2v: Talk2View, engine: FakeEngine,
                                                       kwargs: dict, error: type) -> None:
    call = {"provider": "apple", "open_browser": follow_in_browser, **kwargs}
    provider = call.pop("provider")
    with pytest.raises(error):
        _sign_in(t2v, provider, **call)
    assert engine.desktop_requests == []


def test_desktop_sign_in_providers(t2v: Talk2View, engine: FakeEngine) -> None:
    engine.desktop_providers = ["google", "github", "apple"]
    assert t2v.auth.desktop_sign_in_providers() == ["apple", "google"]
    engine.desktop_providers = []
    assert t2v.auth.desktop_sign_in_providers() == []


@pytest.mark.parametrize("then", ["login", "logout"])
def test_signing_in_or_out_another_way_while_token_is_in_flight_wins(
    t2v: Talk2View, engine: FakeEngine, then: str
) -> None:
    # The UI server's order: cancel the waiting attempt, then sign in with a password or
    # sign out. The late provider session must never replace that choice.
    engine.desktop_token_gate = threading.Event()
    token = CancelToken()
    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(heard.append)

    def choose_another_way() -> None:
        while not _bodies(engine, "/token"):
            time.sleep(0.01)
        token.cancel()
        if then == "login":
            t2v.auth.login("ann@example.com", "pw")
        else:
            t2v.auth.logout()
        engine.desktop_token_gate.set()

    chooser = threading.Thread(target=choose_another_way, daemon=True)
    chooser.start()
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=follow_in_browser, cancel=token)
    chooser.join(5)
    assert caught.value.type == "oauth_cancelled"
    if then == "login":
        user = t2v.auth.get_user()
        assert user is not None and user.id == "user-ann"
        assert [u.id if u else None for u in heard] == ["user-ann"]
    else:
        assert not t2v.auth.is_authenticated() and heard == [None]


class _CancelAtTheLastCheck(CancelToken):
    """Cancels, and signs out, from another thread at the moment the SDK makes its last
    check before storing the session: the check and the store must be one step, so the
    sign-out lands after the store (clearing it), never between the two."""

    def __init__(self, engine: FakeEngine, t2v: Talk2View) -> None:
        super().__init__()
        self._engine, self._t2v, self._fired = engine, t2v, False
        self.signed_out = threading.Event()

    @property
    def cancelled(self) -> bool:
        value = super().cancelled
        if not self._fired and _bodies(self._engine, "/token"):
            self._fired = True

            def sign_out() -> None:
                self.cancel()
                self._t2v.auth.logout()
                self.signed_out.set()

            racer = threading.Thread(target=sign_out, daemon=True)
            racer.start()
            racer.join(0.5)  # an unguarded store lets the sign-out finish first
        return value


def test_a_sign_out_racing_the_store_is_never_undone(t2v: Talk2View, engine: FakeEngine) -> None:
    token = _CancelAtTheLastCheck(engine, t2v)
    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(heard.append)
    _sign_in(t2v, "apple", open_browser=follow_in_browser, cancel=token)
    assert token.signed_out.wait(5)
    assert not t2v.auth.is_authenticated()
    assert [u.id if u else None for u in heard] == ["u-desktop", None]


def test_a_cancel_after_begin_never_opens_the_browser(t2v: Talk2View,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    token = CancelToken()
    begin = t2v.auth._begin_desktop_sign_in

    def begin_then_cancel(*args: Any) -> str:
        start_url = begin(*args)
        token.cancel()
        return start_url

    monkeypatch.setattr(t2v.auth, "_begin_desktop_sign_in", begin_then_cancel)
    opened: List[str] = []
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=opened.append, cancel=token)
    assert caught.value.type == "oauth_cancelled"
    assert opened == []


def _serving_threads() -> List[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "t2v-oauth-loopback"]


def _interrupt(url: str) -> None:
    raise KeyboardInterrupt


@pytest.mark.parametrize("setup, browser, kwargs, raised", [
    ({}, follow_in_browser, {}, None),
    ({"desktop_answer": "access_denied"}, follow_in_browser, {}, ProviderSignInError),
    ({"desktop_refuse_token": True}, follow_in_browser, {}, ProviderSignInError),
    ({}, lambda url: None, {"timeout": 0.3}, ProviderSignInError),
    ({}, lambda url: False, {}, ProviderSignInError),
    ({}, _interrupt, {}, KeyboardInterrupt),
    ({"desktop_start_url": "https://evil.example/"}, follow_in_browser, {}, ProviderSignInError),
])
def test_the_listener_is_always_closed(t2v: Talk2View, engine: FakeEngine, setup: dict,
                                       browser: Any, kwargs: dict,
                                       raised: Optional[type]) -> None:
    for name, value in setup.items():
        setattr(engine, name, value)
    if raised is None:
        _sign_in(t2v, "apple", open_browser=browser, **kwargs)
    else:
        with pytest.raises(raised):
            _sign_in(t2v, "apple", open_browser=browser, **kwargs)
    assert _serving_threads() == []
    assert _nothing_listens(_bodies(engine, "/begin")[0]["port"])


def test_a_cancel_mid_wait_closes_the_listener(t2v: Talk2View, engine: FakeEngine) -> None:
    token = CancelToken()
    threading.Timer(0.2, token.cancel).start()
    with pytest.raises(ProviderSignInError):
        _sign_in(t2v, "apple", open_browser=lambda url: None, cancel=token)
    assert _serving_threads() == []
    assert _nothing_listens(_bodies(engine, "/begin")[0]["port"])


# ── Another sign-in wins, with no cancel (the SDK protects itself) ──


def _hold_token_then(engine: FakeEngine, act: Any) -> threading.Thread:
    """Hold /token, run ``act`` once it is in flight, then let /token answer."""
    engine.desktop_token_gate = threading.Event()

    def run() -> None:
        while not _bodies(engine, "/token"):
            time.sleep(0.01)
        try:
            act()
        finally:
            assert engine.desktop_token_gate is not None
            engine.desktop_token_gate.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("token", [None, CancelToken])
def test_a_login_while_token_is_in_flight_wins_without_a_cancel(t2v: Talk2View,
                                                                engine: FakeEngine,
                                                                token: Any) -> None:
    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(heard.append)
    chooser = _hold_token_then(engine, lambda: t2v.auth.login("ann@example.com", "pw"))
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=follow_in_browser,
                 cancel=token() if token else None)
    chooser.join(5)
    assert caught.value.type == "oauth_cancelled"
    user = t2v.auth.get_user()
    assert user is not None and user.id == "user-ann"
    assert [u.id if u else None for u in heard] == ["user-ann"]


@pytest.mark.parametrize("token", [None, CancelToken])
def test_a_logout_while_token_is_in_flight_wins_without_a_cancel(t2v: Talk2View,
                                                                 engine: FakeEngine,
                                                                 token: Any) -> None:
    t2v.auth.login("bob@example.com", "pw")
    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(heard.append)
    chooser = _hold_token_then(engine, t2v.auth.logout)
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=follow_in_browser,
                 cancel=token() if token else None)
    chooser.join(5)
    assert caught.value.type == "oauth_cancelled"
    assert not t2v.auth.is_authenticated() and heard == [None]


@pytest.mark.parametrize("token", [None, CancelToken])
def test_a_login_while_the_browser_is_out_wins(t2v: Talk2View, engine: FakeEngine,
                                               token: Any) -> None:
    def browser(url: str) -> int:
        t2v.auth.login("ann@example.com", "pw")  # before the browser comes back
        return follow_in_browser(url)

    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=browser, cancel=token() if token else None)
    assert caught.value.type == "oauth_cancelled"
    user = t2v.auth.get_user()
    assert user is not None and user.id == "user-ann"


def test_a_guest_session_started_meanwhile_is_replaced(t2v: Talk2View,
                                                        engine: FakeEngine) -> None:
    def browser(url: str) -> int:
        t2v.auth.start_anonymous()  # the host started a guest chat while waiting
        return follow_in_browser(url)

    user = _sign_in(t2v, "apple", open_browser=browser)
    assert user.id == "u-desktop" and not t2v.auth.is_anonymous()


def test_a_rejected_refresh_meanwhile_does_not_block_the_sign_in(t2v: Talk2View,
                                                                  engine: FakeEngine) -> None:
    t2v.auth.login("bob@example.com", "pw")

    def browser(url: str) -> int:
        engine.expire("user-bob")  # nobody chose this sign-out
        assert t2v.auth.get_valid_access_token(force_refresh=True) is None
        return follow_in_browser(url)

    user = _sign_in(t2v, "apple", open_browser=browser)
    assert user.id == "u-desktop" and t2v.auth.is_authenticated()


# ── The check, the store and the notify are one step ──


class _SlowStorage(MemoryStorage):
    """Writing the provider's access token takes 0.3 s, and a cancel-then-logout starts on
    another thread just as it begins: that logout must land after the whole store."""

    def __init__(self) -> None:
        super().__init__()
        self.token: Optional[CancelToken] = None
        self.t2v: Optional[Talk2View] = None
        self.racer: Optional[threading.Thread] = None

    def set(self, key: str, value: str) -> None:
        if key == "access_token" and value.startswith("tok-u-desktop") and self.racer is None:
            def sign_out() -> None:
                assert self.token is not None and self.t2v is not None
                self.token.cancel()
                self.t2v.auth.logout()

            self.racer = threading.Thread(target=sign_out, daemon=True)
            self.racer.start()
            time.sleep(0.3)
        super().set(key, value)


def test_a_sign_out_during_a_slow_store_lands_after_it(engine: FakeEngine) -> None:
    engine.desktop_providers = ["apple", "google"]
    storage = _SlowStorage()
    with Talk2View("pk_test", base_url=engine.url(), storage=storage) as t2v:
        storage.token, storage.t2v = CancelToken(), t2v
        heard: List[Optional[User]] = []
        t2v.auth.on_auth_state_change(heard.append)
        _sign_in(t2v, "apple", open_browser=follow_in_browser, cancel=storage.token)
        assert storage.racer is not None
        storage.racer.join(5)
        assert not storage.racer.is_alive()
        assert not t2v.auth.is_authenticated()
        assert [u.id if u else None for u in heard] == ["u-desktop", None]


def test_the_listeners_hear_the_provider_account_before_a_sign_out_that_follows(
    t2v: Talk2View,
) -> None:
    # The first listener is slow and, as it starts, a cancel-then-logout begins on another
    # thread. Store and notify are one step, so every listener hears the provider account
    # first and the sign-out second, matching the store (signed out at the end).
    token = CancelToken()
    racer: List[threading.Thread] = []

    def slow(user: Optional[User]) -> None:
        if user is not None and user.id == "u-desktop" and not racer:
            def sign_out() -> None:
                token.cancel()
                t2v.auth.logout()

            racer.append(threading.Thread(target=sign_out, daemon=True))
            racer[0].start()
            time.sleep(0.3)

    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(slow)
    t2v.auth.on_auth_state_change(heard.append)
    _sign_in(t2v, "apple", open_browser=follow_in_browser, cancel=token)
    racer[0].join(5)
    assert not racer[0].is_alive()
    assert [u.id if u else None for u in heard] == ["u-desktop", None]
    assert not t2v.auth.is_authenticated()


@pytest.mark.parametrize("act", ["cancel", "logout", "login"])
def test_a_listener_may_change_things_from_the_signing_thread(t2v: Talk2View, act: str) -> None:
    # Both locks are re-entrant: a listener run inside the store may cancel the same
    # token, or sign in or out, on this thread without hanging it.
    token = CancelToken()

    def listener(user: Optional[User]) -> None:
        if user is None or user.id != "u-desktop":
            return
        if act == "cancel":
            token.cancel()
        elif act == "logout":
            t2v.auth.logout()
        else:
            t2v.auth.login("ann@example.com", "pw")

    t2v.auth.on_auth_state_change(listener)
    signer = threading.Thread(
        target=lambda: _sign_in(t2v, "apple", open_browser=follow_in_browser, cancel=token),
        daemon=True,
    )
    signer.start()
    signer.join(8)
    assert not signer.is_alive()
    expected = {"cancel": "u-desktop", "logout": None, "login": "user-ann"}[act]
    user = t2v.auth.get_user()
    assert (user.id if user else None) == expected


# ── What /token and the other routes answer ──


@pytest.mark.parametrize("missing", ["user", "access_token", "refresh_token"])
def test_half_a_session_from_token_stores_nothing(t2v: Talk2View, engine: FakeEngine,
                                                  missing: str) -> None:
    body: Dict[str, Any] = {"access_token": "tok-x", "refresh_token": "ref-x",
                            "user": {"id": "u-desktop", "email": "d@example.com"}}
    del body[missing]
    engine.desktop_token_body = body
    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(heard.append)
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=follow_in_browser)
    assert caught.value.type == "oauth_failed"
    assert not t2v.auth.is_authenticated() and t2v.auth.get_user() is None and heard == []


def test_the_switch_turned_off_before_token(t2v: Talk2View, engine: FakeEngine) -> None:
    # The browser came back with a code; the partner turns the switch off while /token is
    # on its way: the engine refuses it.
    def switch_off() -> None:
        engine.desktop_providers = []

    helper = _hold_token_then(engine, switch_off)
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=follow_in_browser)
    helper.join(5)
    assert caught.value.type == "desktop_sign_in_disabled"
    assert caught.value.status_code == 403


def _continue_after(act: Any) -> Any:
    """A browser that shows the check screen, lets ``act`` run (an admin, the clock),
    then presses Continue."""
    def browser(url: str) -> int:
        target = open_check_screen(url)
        act()
        return follow_in_browser(target)

    return browser


def test_the_switch_turned_off_before_continue_is_told_at_once(t2v: Talk2View,
                                                               engine: FakeEngine) -> None:
    started = time.monotonic()
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=_continue_after(
            lambda: setattr(engine, "desktop_providers", [])))
    assert caught.value.type == "desktop_sign_in_disabled"
    assert str(caught.value) == "Sign-in with Apple or Google isn't turned on for this app."
    assert time.monotonic() - started < 5  # the loopback told it, not the timeout


def test_continue_after_the_window_is_an_expired_link(t2v: Talk2View,
                                                      engine: FakeEngine) -> None:
    started = time.monotonic()
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=_continue_after(
            lambda: setattr(engine, "desktop_open_window", 0.0)))
    assert caught.value.type == "oauth_expired"
    assert str(caught.value) == "That sign-in link expired. Try again."
    assert time.monotonic() - started < 5


def test_a_switch_off_before_the_check_screen_is_not_told(t2v: Talk2View,
                                                          engine: FakeEngine) -> None:
    # As the engine does: /go only reads, so its refusal page cannot reach the app's
    # loopback (nothing proves that browser is this sign-in's). The app waits, and the
    # end-user's Cancel (or the timeout) ends it.
    def browser(url: str) -> int:
        engine.desktop_providers = []
        return follow_in_browser(url)

    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=browser, timeout=1)
    assert caught.value.type == "oauth_timeout"


@pytest.mark.parametrize("status, error_type", [(429, "rate_limit_exceeded"),
                                                (429, "something_else"),
                                                (403, "rate_limit_exceeded")])
def test_too_many_sign_ins_from_this_network(t2v: Talk2View, engine: FakeEngine,
                                             status: int, error_type: str) -> None:
    engine.desktop_begin_refusal = (status, error_type)
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=follow_in_browser)
    assert caught.value.type == "rate_limited" and caught.value.status_code == status
    assert "10 per 1 minute" not in str(caught.value)  # never the limiter's own text
    assert str(caught.value) == (
        "Too many sign-in attempts from this network. Wait a minute and try again."
    )


def test_providers_that_are_not_a_list_are_none(t2v: Talk2View,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(t2v.auth._client, "request", lambda *a, **k: {"providers": "apple"})
    assert t2v.auth.desktop_sign_in_providers() == []


def test_another_begin_refusal_is_the_engines_own_error(t2v: Talk2View,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise T2VError("Try again", "engine_unavailable", 503)

    monkeypatch.setattr(t2v.auth._client, "request", refuse)
    with pytest.raises(T2VError) as caught:
        _sign_in(t2v, "apple", open_browser=follow_in_browser)
    assert not isinstance(caught.value, ProviderSignInError)
    assert caught.value.type == "engine_unavailable"


# ── A token refresh in flight never touches a session stored after it began ──


class _HeldRefresh:
    """Holds this client's next ``/v1/auth/refresh`` on the wire until ``finish()``, as a
    slow network would: the refresh has spent the stored refresh token, and whatever is
    stored meanwhile is not what it refreshed."""

    def __init__(self, t2v: Talk2View, monkeypatch: pytest.MonkeyPatch) -> None:
        self.t2v = t2v
        self.started = threading.Event()
        self.gate = threading.Event()
        self.result: List[Optional[str]] = []
        real = t2v._client._http.post

        def post(url: Any, **kwargs: Any) -> Any:
            if str(url).endswith("/v1/auth/refresh") and not self.started.is_set():
                self.started.set()
                self.gate.wait(10)
            return real(url, **kwargs)

        monkeypatch.setattr(t2v._client._http, "post", post)
        self.thread = threading.Thread(
            target=lambda: self.result.append(
                t2v.auth.get_valid_access_token(force_refresh=True)
            ),
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()
        assert self.started.wait(5)

    def finish(self) -> None:
        self.gate.set()
        self.thread.join(10)
        assert not self.thread.is_alive()


def _owner(engine: FakeEngine, table: Dict[str, str], token: Optional[str]) -> Optional[str]:
    with engine.lock:
        return table.get(token or "")


@pytest.mark.parametrize("store", ["provider", "login"])
@pytest.mark.parametrize("refresh", ["answered", "rejected"])
def test_a_guest_refresh_in_flight_never_touches_the_new_session(
    t2v: Talk2View, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch,
    store: str, refresh: str,
) -> None:
    guest = t2v.auth.start_anonymous()
    if refresh == "rejected":
        engine.expire(guest.id)  # the engine will refuse the guest's refresh token
    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(heard.append)
    held = _HeldRefresh(t2v, monkeypatch)
    held.start()
    if store == "provider":
        expected = _sign_in(t2v, "apple", open_browser=follow_in_browser).id
    else:
        expected = t2v.auth.login("ann@example.com", "pw").id
    held.finish()
    stored = t2v.storage
    user = t2v.auth.get_user()
    assert user is not None and user.id == expected
    assert _owner(engine, engine.tokens, stored.get("access_token")) == expected
    assert _owner(engine, engine.refresh, stored.get("refresh_token")) == expected
    assert stored.get("is_anonymous") == "false"
    assert [u.id if u else None for u in heard] == [expected]  # no sign-out heard


def test_a_refresh_with_nothing_racing_still_rotates_the_tokens(
    t2v: Talk2View, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    guest = t2v.auth.start_anonymous()
    before = t2v.storage.get("access_token")
    held = _HeldRefresh(t2v, monkeypatch)
    held.start()
    held.finish()
    after = t2v.storage.get("access_token")
    assert after != before and held.result == [after]
    assert _owner(engine, engine.tokens, after) == guest.id
    assert _owner(engine, engine.refresh, t2v.storage.get("refresh_token")) == guest.id


def test_a_rejected_refresh_with_nothing_racing_still_signs_out(
    t2v: Talk2View, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    guest = t2v.auth.start_anonymous()
    engine.expire(guest.id)
    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(heard.append)
    held = _HeldRefresh(t2v, monkeypatch)
    held.start()
    held.finish()
    assert held.result == [None]
    assert not t2v.auth.is_authenticated() and heard == [None]


@pytest.mark.parametrize("refresh", ["answered", "rejected"])
def test_a_refresh_racing_a_logout_never_brings_the_session_back(
    t2v: Talk2View, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch, refresh: str,
) -> None:
    t2v.auth.login("bob@example.com", "pw")
    if refresh == "rejected":
        # Only the refresh token: the logout's own request still goes through.
        with engine.lock:
            for spent in [t for t, u in engine.refresh.items() if u == "user-bob"]:
                del engine.refresh[spent]
    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(heard.append)
    held = _HeldRefresh(t2v, monkeypatch)
    held.start()
    t2v.auth.logout()
    held.finish()
    assert held.result == [None]
    assert not t2v.auth.is_authenticated() and t2v.auth.get_user() is None
    assert t2v.storage.get("refresh_token") is None
    assert heard == [None]  # the logout's, and no second one


def test_a_listener_may_refresh_while_another_refresh_waits(
    t2v: Talk2View, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The refresh never waits for the lock the provider store holds while its listeners
    # run: a listener that refreshes (it needs the refresh lock, which the waiting
    # refresh holds) would otherwise deadlock the two.
    t2v.auth.start_anonymous()
    held = _HeldRefresh(t2v, monkeypatch)
    held.start()
    refreshed: List[Optional[str]] = []

    def listener(user: Optional[User]) -> None:
        if user is not None and user.id == "u-desktop" and not refreshed:
            threading.Timer(0.3, held.gate.set).start()  # the slow network answers
            refreshed.append(t2v.auth.get_valid_access_token(force_refresh=True))

    t2v.auth.on_auth_state_change(listener)
    _sign_in(t2v, "apple", open_browser=follow_in_browser)
    held.finish()
    assert refreshed and refreshed[0] == t2v.storage.get("access_token")
    assert _owner(engine, engine.tokens, refreshed[0]) == "u-desktop"


# ── A login landing between the provider's check and its store ──


class _LoginAtTheLastCheck(CancelToken):
    """Never cancelled. At the SDK's last check before storing, a ``login()`` (with no
    cancel) starts on another thread: it must wait for the provider's store, then win."""

    def __init__(self, engine: FakeEngine, t2v: Talk2View) -> None:
        super().__init__()
        self._engine, self._t2v, self._fired = engine, t2v, False
        self.logged_in = threading.Event()

    @property
    def cancelled(self) -> bool:
        value = super().cancelled
        if not self._fired and _bodies(self._engine, "/token"):
            self._fired = True

            def log_in() -> None:
                self._t2v.auth.login("ann@example.com", "pw")
                self.logged_in.set()

            racer = threading.Thread(target=log_in, daemon=True)
            racer.start()
            racer.join(0.5)  # a login that could slip in has finished by now
        return value


def test_a_login_at_the_last_check_waits_for_the_store_then_wins(t2v: Talk2View,
                                                                  engine: FakeEngine) -> None:
    token = _LoginAtTheLastCheck(engine, t2v)
    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(heard.append)
    user = _sign_in(t2v, "apple", open_browser=follow_in_browser, cancel=token)
    assert user.id == "u-desktop"
    assert token.logged_in.wait(5)
    final = t2v.auth.get_user()
    assert final is not None and final.id == "user-ann"
    assert [u.id if u else None for u in heard] == ["u-desktop", "user-ann"]


class _SlowRefreshWrite(MemoryStorage):
    """Once armed, the refresh's write of the guest's new access token takes 0.3 s, and
    ``act`` (a ``login()``, a ``logout()``) starts on another thread just as it begins. Its
    store or clear must wait for the refresh's whole write (access and refresh token)."""

    def __init__(self, act: str = "login") -> None:
        super().__init__()
        self.act = act
        self.t2v: Optional[Talk2View] = None
        self.racer: Optional[threading.Thread] = None

    def set(self, key: str, value: str) -> None:
        if (self.t2v is not None and self.racer is None and key == "access_token"
                and value.startswith("tok-anon")):
            t2v = self.t2v
            act = (t2v.auth.logout if self.act == "logout"
                   else lambda: t2v.auth.login("ann@example.com", "pw"))
            self.racer = threading.Thread(target=act, daemon=True)
            self.racer.start()
            time.sleep(0.3)
        super().set(key, value)


def test_a_store_waits_for_a_refresh_write_under_way(engine: FakeEngine) -> None:
    storage = _SlowRefreshWrite()
    with Talk2View("pk_test", base_url=engine.url(), storage=storage) as t2v:
        t2v.auth.start_anonymous()
        storage.t2v = t2v  # armed: the next guest token written is the refresh's
        t2v.auth.get_valid_access_token(force_refresh=True)
        assert storage.racer is not None
        storage.racer.join(5)
        assert not storage.racer.is_alive()
        assert _owner(engine, engine.tokens, storage.get("access_token")) == "user-ann"
        assert _owner(engine, engine.refresh, storage.get("refresh_token")) == "user-ann"


def test_a_logout_waits_for_a_refresh_write_under_way(engine: FakeEngine) -> None:
    # The logout's clear takes the tokens lock too: it cannot land between the refresh's
    # two writes and leave a session with tokens but no user.
    storage = _SlowRefreshWrite("logout")
    with Talk2View("pk_test", base_url=engine.url(), storage=storage) as t2v:
        t2v.auth.start_anonymous()
        storage.t2v = t2v
        t2v.auth.get_valid_access_token(force_refresh=True)
        assert storage.racer is not None
        storage.racer.join(5)
        assert not storage.racer.is_alive()
        assert not t2v.auth.is_authenticated()
        assert storage.get("refresh_token") is None and storage.get("user") is None


# ── The done page, and a cancel while /token is in flight ──


def test_the_browser_ends_on_the_engines_done_page(t2v: Talk2View, engine: FakeEngine) -> None:
    seen: List[str] = []

    def browser(url: str) -> int:
        status, visited = browse(url)
        seen.extend(visited)
        return status

    _sign_in(t2v, "apple", open_browser=browser)
    assert seen[-1] == engine.url().rstrip("/") + "/v1/auth/oauth/desktop/done"  # no code
    assert "code=" in seen[-2]  # the listener's own URL, answered with a 303


def test_a_refusal_ends_on_the_engines_done_page_marked_failed(t2v: Talk2View,
                                                               engine: FakeEngine) -> None:
    # A refusal the engine hands the loopback from its Continue page (a meta refresh, not
    # a redirect) still reaches the listener, which sends the browser to `done?failed=1`.
    seen: List[str] = []

    def browser(url: str) -> int:
        target = open_check_screen(url)
        engine.desktop_providers = []
        status, visited = browse(target)
        seen.extend(visited)
        return status

    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=browser)
    assert caught.value.type == "desktop_sign_in_disabled"
    assert seen[-1] == engine.url().rstrip("/") + "/v1/auth/oauth/desktop/done?failed=1"
    assert "error=desktop_sign_in_disabled" in seen[-2]


def test_a_cancel_while_token_is_in_flight_stores_nothing(t2v: Talk2View,
                                                           engine: FakeEngine) -> None:
    # The browser came back and /token is on its way when the end-user signs in with a
    # password instead (the router cancels first): the late session must not be stored.
    engine.desktop_token_gate = threading.Event()
    token = CancelToken()
    heard: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(heard.append)

    def cancel_once_token_is_asked() -> None:
        while not _bodies(engine, "/token"):
            time.sleep(0.01)
        token.cancel()
        assert engine.desktop_token_gate is not None
        engine.desktop_token_gate.set()

    threading.Thread(target=cancel_once_token_is_asked, daemon=True).start()
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=follow_in_browser, cancel=token)
    assert caught.value.type == "oauth_cancelled"
    assert not t2v.auth.is_authenticated() and heard == []


def test_a_refused_code_is_a_failed_sign_in(t2v: Talk2View, engine: FakeEngine) -> None:
    engine.desktop_refuse_token = True
    with pytest.raises(ProviderSignInError) as caught:
        _sign_in(t2v, "apple", open_browser=follow_in_browser)
    assert caught.value.type == "oauth_failed"
    assert not t2v.auth.is_authenticated()


def test_nothing_secret_is_logged(t2v: Talk2View, engine: FakeEngine,
                                  caplog: pytest.LogCaptureFixture,
                                  capfd: pytest.CaptureFixture[str]) -> None:
    caplog.set_level(logging.DEBUG)
    opened: List[str] = []

    def browser(url: str) -> int:
        opened.append(url)
        return follow_in_browser(url)

    _sign_in(t2v, "apple", open_browser=browser)
    (begun,) = _bodies(engine, "/begin")
    (redeemed,) = _bodies(engine, "/token")
    out, err = capfd.readouterr()
    secrets = (opened[0].rsplit("/", 1)[1], begun["state"], begun["code_challenge"],
               redeemed["code"], redeemed["code_verifier"])
    # The fake engine's own access log (werkzeug) stands in for the engine's, which by
    # design sees /go/<handle>; nothing else may hold any of them.
    logged = "\n".join(
        record.getMessage() for record in caplog.records if record.name != "werkzeug"
    )
    for secret in secrets:
        assert secret not in logged and secret not in out and secret not in err
