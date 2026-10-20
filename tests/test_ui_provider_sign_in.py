"""Provider sign-in through the UI server: oauthStart / oauthCancel, auth.oauthPending,
auth.oauthError, capabilities.oauth."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest

from talk2view import Talk2View
from talk2view.ui import AppState, GenericHost
from talk2view.ui.hub import EventHub
from talk2view.ui.options import ChatFeatures, ChatOptions
from talk2view.ui.router import CommandRouter, UploadRegistry
from talk2view.ui.testing import FakeController
from talk2view.ui.wire import CommandError, parse_command

from .fake_engine import FakeEngine, follow_in_browser, open_check_screen
from .ui_helpers import wait_for


def inline(job: Callable[[], None]) -> None:
    job()


class BrowserHost(GenericHost):
    """Runs posted work at once. "Opens" a URL with a browser that follows the engine's
    redirect to the loopback listener, unless `follow` is off (the end-user is still in
    the browser); `broken` makes opening raise."""

    def __init__(self) -> None:
        super().__init__()
        self.opened: List[str] = []
        self.follow = True
        self.broken = False
        self.refuse = False  # open_external returns False, as webbrowser.open with no browser

    def post(self, fn: Callable[[], None]) -> None:
        fn()

    def open_external(self, url: str) -> Any:
        if self.broken:
            raise RuntimeError("no browser")
        if self.refuse:
            return False
        self.opened.append(url)
        if self.follow:
            threading.Thread(target=follow_in_browser, args=(url,), daemon=True).start()
        return True


class MainThreadHost(BrowserHost):
    """Posts work to a separate "main" thread, as Qt and GenericHost do, so the open's
    result has to travel back across threads."""

    def post(self, fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name="fake-main", daemon=True).start()


class Rig:
    def __init__(self, engine: FakeEngine, features: Optional[ChatFeatures] = None) -> None:
        self.engine = engine
        self.t2v = Talk2View("pk_test", base_url=engine.url())
        self.host = BrowserHost()
        self.hub = EventHub()
        controller = FakeController(executor=inline)
        self.app = AppState(self.t2v, self.host,
                            options=ChatOptions(features=features or ChatFeatures()))
        self.app.attach(self.hub, controller)
        self.router = CommandRouter(controller, self.app, self.host, UploadRegistry())
        self.deadlocked = False  # close() would hang on the stuck locks: left to leak

    def run(self, **command: Any) -> Dict[str, Any]:
        return self.router.execute(parse_command(command))

    def state(self) -> Dict[str, Any]:
        value: Dict[str, Any] = self.hub.snapshot()["state"]
        return value

    def auth(self) -> Dict[str, Any]:
        value: Dict[str, Any] = self.state()["auth"]
        return value

    def offered(self) -> bool:
        return wait_for(lambda: self.state()["capabilities"]["oauth"] is True)

    def close(self) -> None:
        self.router.stop()
        self.app.detach()
        self.t2v.close()


@pytest.fixture
def rig(engine: FakeEngine) -> Iterator[Rig]:
    engine.desktop_providers = ["apple", "google"]
    r = Rig(engine)
    yield r
    if not r.deadlocked:
        r.close()


def error_type(result: Dict[str, Any]) -> Optional[str]:
    return None if result["ok"] else result["error"]["type"]


def test_the_slices_carry_the_new_keys_from_the_start(rig: Rig) -> None:
    auth = rig.auth()
    assert auth["oauthPending"] is None and auth["oauthError"] is None
    assert "oauthProviders" in rig.state()["capabilities"]
    assert rig.state()["options"]["features"]["providerSignIn"] is True


def test_capabilities_follow_the_engine(rig: Rig) -> None:
    assert rig.offered()
    assert rig.state()["capabilities"]["oauthProviders"] == ["apple", "google"]


def test_probe_404_leaves_oauth_off(engine: FakeEngine,
                                    caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="talk2view.ui")
    engine.desktop_providers = None  # an engine older than this SDK
    r = Rig(engine)
    try:
        assert wait_for(lambda: engine.desktop_probes >= 1)
        r.app.refresh()  # an auth change's refresh: the 404 is not asked again
        assert not wait_for(lambda: engine.desktop_probes >= 2, timeout=0.5)
        probe_logs = [rec for rec in caplog.records if "desktop sign-in" in rec.getMessage()]
        assert [rec.levelno for rec in probe_logs] == [logging.INFO]
        assert r.state()["capabilities"]["oauth"] is False
        assert r.state()["capabilities"]["oauthProviders"] == []
        assert error_type(r.run(type="oauthStart", provider="apple")) == "oauth_unavailable"
    finally:
        r.close()


def test_the_integrator_can_turn_the_buttons_off(engine: FakeEngine) -> None:
    engine.desktop_providers = ["apple", "google"]
    r = Rig(engine, ChatFeatures(provider_sign_in=False))
    try:
        assert r.state()["options"]["features"]["providerSignIn"] is False
        assert r.state()["capabilities"]["oauth"] is False
        assert error_type(r.run(type="oauthStart", provider="apple")) == "oauth_disabled"
        assert engine.desktop_requests == []
    finally:
        r.close()


def test_a_sign_in_arrives_through_the_auth_slice(rig: Rig, engine: FakeEngine) -> None:
    assert rig.offered()
    assert rig.run(type="oauthStart", provider="apple", id="o1")["ok"] is True
    assert wait_for(lambda: rig.auth()["isAuthenticated"] is True)
    assert rig.auth()["user"]["id"] == "u-desktop"
    assert wait_for(lambda: rig.auth()["oauthPending"] is None)
    assert rig.auth()["oauthError"] is None
    assert rig.host.opened[0].startswith(
        engine.url().rstrip("/") + "/v1/auth/oauth/desktop/go/"
    )


def test_pending_shows_while_the_browser_is_out_and_cancel_is_no_error(rig: Rig) -> None:
    assert rig.offered()
    rig.host.follow = False
    assert rig.run(type="oauthStart", provider="google")["ok"] is True
    assert rig.auth()["oauthPending"] == {"provider": "google"}
    assert rig.run(type="oauthCancel")["ok"] is True
    assert wait_for(lambda: rig.auth()["oauthPending"] is None)
    assert rig.auth()["oauthError"] is None
    assert rig.auth()["isAuthenticated"] is False


def test_cancel_then_start_again(rig: Rig) -> None:
    assert rig.offered()
    rig.host.follow = False
    rig.run(type="oauthStart", provider="apple")
    rig.run(type="oauthCancel")
    assert wait_for(lambda: rig.auth()["oauthPending"] is None)
    rig.host.follow = True
    assert rig.run(type="oauthStart", provider="apple")["ok"] is True
    assert wait_for(lambda: rig.auth()["isAuthenticated"] is True)


def test_a_second_start_while_one_waits_is_busy(rig: Rig) -> None:
    assert rig.offered()
    rig.host.follow = False
    assert rig.run(type="oauthStart", provider="apple")["ok"] is True
    assert error_type(rig.run(type="oauthStart", provider="google")) == "busy"
    rig.run(type="oauthCancel")


def test_oauth_cancel_with_nothing_waiting_is_fine(rig: Rig) -> None:
    assert rig.run(type="oauthCancel")["ok"] is True


def test_the_wire_takes_only_known_providers() -> None:
    with pytest.raises(CommandError):
        parse_command({"type": "oauthStart", "provider": "github"})
    with pytest.raises(CommandError):
        parse_command({"type": "oauthStart"})
    with pytest.raises(CommandError):
        parse_command({"type": "oauthCancel", "provider": "apple"})
    with pytest.raises(CommandError):
        parse_command({"type": "oauthDismissError", "id": 7})


def test_a_provider_the_engine_does_not_offer_is_unavailable(engine: FakeEngine) -> None:
    engine.desktop_providers = ["google"]
    r = Rig(engine)
    try:
        assert r.offered()
        assert error_type(r.run(type="oauthStart", provider="apple")) == "oauth_unavailable"
    finally:
        r.close()


def test_a_refusal_in_the_browser_is_reported(rig: Rig, engine: FakeEngine) -> None:
    assert rig.offered()
    engine.desktop_answer = "access_denied"
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: rig.auth()["oauthError"] is not None)
    assert rig.auth()["oauthError"]["type"] == "oauth_denied"
    assert rig.auth()["oauthPending"] is None and rig.auth()["isAuthenticated"] is False


def test_the_error_clears_when_the_next_attempt_starts(rig: Rig, engine: FakeEngine) -> None:
    assert rig.offered()
    engine.desktop_answer = "access_denied"
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: rig.auth()["oauthError"] is not None)
    engine.desktop_answer = "code"
    rig.host.follow = False
    rig.run(type="oauthStart", provider="apple")
    assert rig.auth()["oauthError"] is None
    rig.run(type="oauthCancel")


def test_browser_unavailable_is_shown(rig: Rig) -> None:
    assert rig.offered()
    rig.host.broken = True
    assert rig.run(type="oauthStart", provider="apple")["ok"] is True
    assert wait_for(lambda: rig.auth()["oauthError"] is not None)
    assert rig.auth()["oauthError"]["type"] == "oauth_browser_unavailable"
    assert rig.auth()["oauthPending"] is None


def test_switch_turned_off_before_continue(rig: Rig, engine: FakeEngine) -> None:
    # The end-user is on the engine's check screen when an admin turns the switch off;
    # Continue then hands the refusal to the loopback, so the page says so at once.
    assert rig.offered()
    rig.host.follow = False
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: len(rig.host.opened) == 1)
    target = open_check_screen(rig.host.opened[0])
    engine.desktop_providers = []  # an admin turns the switch off
    follow_in_browser(target)
    assert wait_for(lambda: rig.auth()["oauthError"] is not None)
    assert rig.auth()["oauthError"] == {
        "type": "desktop_sign_in_disabled",
        "message": "Sign-in with Apple or Google isn't turned on for this app.",
    }
    assert rig.auth()["oauthPending"] is None
    assert rig.state()["capabilities"]["oauth"] is False


def test_continue_after_the_window_says_the_link_expired(rig: Rig, engine: FakeEngine) -> None:
    assert rig.offered()
    rig.host.follow = False
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: len(rig.host.opened) == 1)
    target = open_check_screen(rig.host.opened[0])
    engine.desktop_open_window = 0.0  # the end-user waited too long before Continue
    follow_in_browser(target)
    assert wait_for(lambda: rig.auth()["oauthError"] is not None)
    assert rig.auth()["oauthError"] == {
        "type": "oauth_expired", "message": "That sign-in link expired. Try again.",
    }
    assert rig.auth()["oauthPending"] is None


def test_switch_turned_off_before_the_check_screen_waits_for_cancel(rig: Rig,
                                                                    engine: FakeEngine) -> None:
    # As the engine does: the check screen's refusal cannot reach the app (nothing proves
    # that browser is this sign-in's), so the page stays pending until Cancel.
    assert rig.offered()
    rig.host.follow = False
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: len(rig.host.opened) == 1)
    engine.desktop_providers = []
    assert follow_in_browser(rig.host.opened[0]) == 400  # "That sign-in has expired"
    assert not wait_for(lambda: rig.auth()["oauthPending"] is None, timeout=0.5)
    assert rig.run(type="oauthCancel")["ok"] is True
    assert wait_for(lambda: rig.auth()["oauthPending"] is None)
    assert rig.auth()["oauthError"] is None


def test_too_many_sign_ins_is_said_plainly(rig: Rig, engine: FakeEngine) -> None:
    assert rig.offered()
    engine.desktop_begin_refusal = (429, "rate_limit_exceeded")
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: rig.auth()["oauthError"] is not None)
    assert rig.auth()["oauthError"] == {
        "type": "rate_limited",
        "message": "Too many sign-in attempts from this network. Wait a minute and try again.",
    }


def test_login_cancels_a_waiting_provider_sign_in(rig: Rig) -> None:
    assert rig.offered()
    rig.host.follow = False
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: len(rig.host.opened) == 1)
    assert rig.run(type="login", email="ana@example.com", password="pw")["ok"] is True
    assert wait_for(lambda: rig.auth()["oauthPending"] is None)
    # The browser comes back late: the listener is gone, nothing changes.
    assert follow_in_browser(rig.host.opened[0]) == 0
    assert rig.auth()["user"]["id"] == "user-ana"


@pytest.mark.parametrize("command", [
    {"type": "logout"},
    {"type": "signup", "email": "b@example.com", "password": "pw"},
])
def test_logout_and_signup_cancel_it_too(rig: Rig, command: Dict[str, Any]) -> None:
    assert rig.offered()
    rig.host.follow = False
    rig.run(type="oauthStart", provider="apple")
    rig.router.execute(parse_command(command))
    assert wait_for(lambda: rig.auth()["oauthPending"] is None)


def test_stopping_the_router_ends_a_waiting_sign_in(rig: Rig) -> None:
    assert rig.offered()
    rig.host.follow = False
    rig.run(type="oauthStart", provider="apple")
    rig.router.stop()
    assert wait_for(lambda: rig.auth()["oauthPending"] is None, timeout=3)
    assert not any(t.name == "t2v-ui-oauth" for t in threading.enumerate())


def test_a_host_whose_browser_refuses_is_reported_at_once(engine: FakeEngine) -> None:
    # Review Focus 5 on a host that runs posted work on its main thread: the False from
    # webbrowser.open must come back, not end in the 300 s timeout.
    engine.desktop_providers = ["apple", "google"]
    r = Rig(engine)
    r.host = MainThreadHost()
    r.router = CommandRouter(FakeController(executor=inline), r.app, r.host, UploadRegistry())
    try:
        assert r.offered()
        r.host.refuse = True
        r.run(type="oauthStart", provider="apple")
        assert wait_for(lambda: r.auth()["oauthError"] is not None, timeout=3)
        assert r.auth()["oauthError"]["type"] == "oauth_browser_unavailable"
    finally:
        r.close()


def test_dismiss_clears_the_message_and_nothing_else_does(rig: Rig, engine: FakeEngine) -> None:
    assert rig.offered()
    engine.desktop_answer = "access_denied"
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: rig.auth()["oauthError"] is not None)
    # An account change keeps it (owner ruling: next attempt or dismiss only).
    rig.run(type="login", email="ana@example.com", password="pw")
    assert wait_for(lambda: rig.auth()["isAuthenticated"] is True)
    assert rig.auth()["oauthError"]["type"] == "oauth_denied"
    assert rig.run(type="oauthDismissError")["ok"] is True
    assert rig.auth()["oauthError"] is None
    assert rig.run(type="oauthDismissError")["ok"] is True  # nothing to dismiss: fine


def test_dismiss_leaves_a_waiting_sign_in_alone(rig: Rig) -> None:
    assert rig.offered()
    rig.host.follow = False
    rig.run(type="oauthStart", provider="apple")
    rig.run(type="oauthDismissError")
    assert rig.auth()["oauthPending"] == {"provider": "apple"}
    rig.run(type="oauthCancel")


def test_a_login_while_token_is_in_flight_keeps_the_chosen_account(rig: Rig,
                                                                     engine: FakeEngine) -> None:
    # The browser came back and /token is in flight when the end-user signs in with
    # a password. The router cancels first; the late provider session is dropped.
    assert rig.offered()
    engine.desktop_token_gate = threading.Event()
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: any(p.endswith("/token") for p, _ in engine.desktop_requests))
    assert rig.run(type="login", email="ana@example.com", password="pw")["ok"] is True
    engine.desktop_token_gate.set()
    assert wait_for(lambda: rig.auth()["oauthPending"] is None)
    assert rig.auth()["user"]["id"] == "user-ana"
    assert rig.t2v.auth.get_user().id == "user-ana"


def test_logout_while_token_is_in_flight_stays_signed_out(rig: Rig, engine: FakeEngine) -> None:
    assert rig.offered()
    rig.run(type="login", email="ana@example.com", password="pw")
    engine.desktop_token_gate = threading.Event()
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: any(p.endswith("/token") for p, _ in engine.desktop_requests))
    rig.run(type="logout")
    engine.desktop_token_gate.set()
    assert wait_for(lambda: rig.auth()["oauthPending"] is None)
    assert rig.auth()["isAuthenticated"] is False


def test_a_failed_probe_is_tried_again(engine: FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    import talk2view.ui.app_state as app_state

    monkeypatch.setattr(app_state, "PROBE_RETRY_S", 0.2)
    engine.desktop_providers = ["apple", "google"]
    engine.desktop_probe_status = 503  # the first probe fails (the engine is starting)
    r = Rig(engine)
    try:
        assert wait_for(lambda: engine.desktop_probes >= 1)
        engine.desktop_probe_status = None  # the engine is back
        assert r.offered()  # without any auth change
    finally:
        r.close()


def test_chat_features_checks_the_flag() -> None:
    with pytest.raises(TypeError):
        ChatFeatures(provider_sign_in="yes")  # type: ignore[arg-type]
    assert ChatOptions().to_wire()["features"]["providerSignIn"] is True


@pytest.mark.parametrize("command", [
    {"type": "oauthCancel"},
    {"type": "login", "email": "ana@example.com", "password": "pw"},
    {"type": "logout"},
])
def test_a_cancel_racing_a_successful_store_never_deadlocks(rig: Rig,
                                                            command: Dict[str, Any]) -> None:
    # A cancel that lands while the session is stored waits for the store and the
    # listeners, AppState's among them. The router cancels holding no lock, so the wait
    # ends; the command then runs after the sign-in, and its choice is the one that stays.
    assert rig.offered()
    in_notify, release = threading.Event(), threading.Event()

    def holds_the_store(user: Any) -> None:  # runs before AppState's listener
        if user is not None and user.id == "u-desktop" and not in_notify.is_set():
            in_notify.set()
            release.wait(5)

    rig.t2v.auth._add_listener(holds_the_store, early=True)
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: in_notify.is_set())
    results: List[Dict[str, Any]] = []
    router = threading.Thread(target=lambda: results.append(rig.run(**command)), daemon=True)
    router.start()
    router.join(0.3)
    assert router.is_alive()  # the cancel waits for the store in progress
    # Meanwhile the page's reads and a dismissal go through: nothing waits on the store.
    seen: List[Any] = []

    def page() -> None:
        rig.app.dismiss_oauth_error()
        seen.append(rig.auth()["oauthPending"])

    reader = threading.Thread(target=page, daemon=True)
    reader.start()
    reader.join(2)
    release.set()
    router.join(5)
    if reader.is_alive() or router.is_alive():  # fail, never hang (stuck daemon threads)
        rig.deadlocked = True
        pytest.fail("the cancel deadlocked against the store")
    assert seen == [{"provider": "apple"}]
    assert results[0]["ok"] is True
    assert wait_for(lambda: rig.auth()["oauthPending"] is None)
    assert rig.auth()["oauthError"] is None  # the sign-in itself succeeded
    expected = {"oauthCancel": "u-desktop", "login": "user-ana", "logout": None}[command["type"]]
    user = rig.t2v.auth.get_user() if rig.t2v.auth.is_authenticated() else None
    assert (user.id if user is not None else None) == expected
    assert wait_for(lambda: (rig.auth()["user"] or {}).get("id") == expected)


def test_generic_host_reports_what_its_opener_returned() -> None:
    assert GenericHost(open_url=lambda url: False).open_external("https://x.test") is False
    assert GenericHost(open_url=lambda url: True).open_external("https://x.test") is True
    assert GenericHost(open_url=lambda url: None).open_external("https://x.test") is None


def test_a_pumped_generic_host_with_no_browser_is_reported_at_once(engine: FakeEngine) -> None:
    # The real GenericHost, pumped on its own "main" thread, with webbrowser.open's "no
    # browser" answer: reported through the posted open, well before any timeout.
    engine.desktop_providers = ["apple", "google"]
    built = threading.Event()
    hosts: List[GenericHost] = []

    def main() -> None:
        hosts.append(GenericHost(open_url=lambda url: False))
        built.set()
        hosts[0].run_forever(poll_s=0.01)

    pump = threading.Thread(target=main, daemon=True)
    pump.start()
    assert built.wait(2)
    r = Rig(engine)
    r.router = CommandRouter(FakeController(executor=inline), r.app, hosts[0], UploadRegistry())
    try:
        assert r.offered()
        assert r.run(type="oauthStart", provider="google")["ok"] is True
        assert wait_for(lambda: r.auth()["oauthError"] is not None, timeout=3)
        assert r.auth()["oauthError"]["type"] == "oauth_browser_unavailable"
        assert r.auth()["oauthPending"] is None
    finally:
        r.close()
        hosts[0].stop()
        pump.join(2)


def test_nothing_of_the_sign_in_reaches_the_log(rig: Rig, engine: FakeEngine,
                                               caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    assert rig.offered()
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: rig.auth()["isAuthenticated"] is True)
    assert wait_for(lambda: rig.auth()["oauthPending"] is None)
    rig.host.broken = True
    rig.run(type="oauthStart", provider="google")
    assert wait_for(lambda: rig.auth()["oauthError"] is not None)
    handle = rig.host.opened[0].rsplit("/", 1)[1]
    sent = [body for _, body in engine.desktop_requests]
    secrets_seen = [handle] + [str(b[k]) for b in sent for k in
                               ("state", "code_challenge", "code", "code_verifier") if k in b]
    for record in caplog.records:
        if record.name.startswith("werkzeug"):
            continue  # the fake engine's access log, standing in for the engine's own
        text = record.getMessage() + str(record.exc_info or "")
        assert not any(s in text for s in secrets_seen), record.name
        assert "?" not in text or "/t2v/callback" not in text


def test_cancel_is_prompt_while_the_host_has_not_opened_the_browser(engine: FakeEngine) -> None:
    # A host whose main thread is busy (never runs the posted open): the wait for its
    # answer ends with the cancel, not after OPEN_WAIT_S.
    posted = threading.Event()

    class BusyHost(BrowserHost):
        def post(self, fn: Callable[[], None]) -> None:
            posted.set()

    engine.desktop_providers = ["apple", "google"]
    r = Rig(engine)
    r.router = CommandRouter(FakeController(executor=inline), r.app, BusyHost(), UploadRegistry())
    try:
        assert r.offered()
        r.run(type="oauthStart", provider="apple")
        assert posted.wait(3)  # the sign-in now waits for the host's answer
        assert r.run(type="oauthCancel")["ok"] is True
        assert wait_for(lambda: r.auth()["oauthPending"] is None, timeout=2)
        assert r.auth()["oauthError"] is None
    finally:
        r.close()


# ── Account changes, sign-in failures and retries ──


@pytest.mark.parametrize("command", [
    {"type": "login", "email": "ana@example.com", "password": "pw"},
    {"type": "logout"},
    {"type": "startAnonymous"},
])
def test_the_router_cancels_before_it_changes_the_account(engine: FakeEngine,
                                                          command: Dict[str, Any]) -> None:
    # /token answers the moment the account changes, and its store gets its chance
    # to land: only a cancel made BEFORE the change keeps the late session out. For
    # login and logout the SDK also drops it (they supersede a provider
    # sign-in); a guest session does not, so for startAnonymous the router's
    # cancel is the only guard.
    engine.desktop_providers = ["apple", "google"]
    r = Rig(engine)
    try:
        assert r.offered()
        if command["type"] == "logout":
            r.run(type="login", email="ana@example.com", password="pw")
        engine.desktop_token_gate = threading.Event()
        r.run(type="oauthStart", provider="apple")
        assert wait_for(lambda: any(p.endswith("/token") for p, _ in engine.desktop_requests))
        landed = threading.Event()

        def on_change(user: Any) -> None:
            if user is not None and user.id == "u-desktop":
                landed.set()
            elif not landed.is_set() and engine.desktop_token_gate is not None:
                gate, engine.desktop_token_gate = engine.desktop_token_gate, None
                gate.set()  # /token answers right after the account change...
                landed.wait(1)  # ...and its store gets a chance to land

        r.t2v.auth.on_auth_state_change(on_change)
        assert r.run(**command)["ok"] is True
        assert wait_for(lambda: r.auth()["oauthPending"] is None)
        user = r.t2v.auth.get_user() if r.t2v.auth.is_authenticated() else None
        if command["type"] == "startAnonymous":
            assert user is not None and r.t2v.auth.is_anonymous()
        else:
            want = "user-ana" if command["type"] == "login" else None
            assert (user.id if user is not None else None) == want
    finally:
        r.close()


def test_start_anonymous_cancels_a_waiting_provider_sign_in(rig: Rig) -> None:
    assert rig.offered()
    rig.host.follow = False
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: len(rig.host.opened) == 1)
    assert rig.run(type="startAnonymous")["ok"] is True
    assert wait_for(lambda: rig.auth()["oauthPending"] is None)
    assert rig.auth()["oauthError"] is None
    assert follow_in_browser(rig.host.opened[0]) == 0  # the listener is gone
    assert rig.auth()["isAnonymous"] is True


def test_start_anonymous_while_token_is_in_flight_stays_a_guest(rig: Rig,
                                                                engine: FakeEngine) -> None:
    # A guest session does not supersede a provider sign-in in the SDK (it replaces
    # one), so only the router's cancel keeps the late session from replacing the guest
    # the end-user just chose.
    assert rig.offered()
    engine.desktop_token_gate = threading.Event()
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: any(p.endswith("/token") for p, _ in engine.desktop_requests))
    assert rig.run(type="startAnonymous")["ok"] is True
    engine.desktop_token_gate.set()
    assert wait_for(lambda: rig.auth()["oauthPending"] is None)
    assert rig.t2v.auth.is_anonymous() is True
    assert rig.auth()["isAnonymous"] is True


def test_start_anonymous_refused_by_the_options_leaves_the_sign_in_alone(
    engine: FakeEngine,
) -> None:
    engine.desktop_providers = ["apple", "google"]
    r = Rig(engine)
    r.app.detach()  # replaced by one whose options refuse guests
    r.app = AppState(r.t2v, r.host, options=ChatOptions(allow_anonymous=False))
    r.app.attach(r.hub, FakeController(executor=inline))
    r.router = CommandRouter(FakeController(executor=inline), r.app, r.host, UploadRegistry())
    try:
        assert r.offered()
        r.host.follow = False
        r.run(type="oauthStart", provider="apple")
        assert error_type(r.run(type="startAnonymous")) == "anonymous_not_allowed"
        assert r.auth()["oauthPending"] == {"provider": "apple"}
        r.run(type="oauthCancel")
    finally:
        r.close()


def test_a_host_that_raises_on_its_main_thread_is_reported_at_once(engine: FakeEngine) -> None:
    # The exception is raised on the host's own thread, not the sign-in's: it must
    # still come back as "no browser", not end in the 300 s wait.
    engine.desktop_providers = ["apple", "google"]
    r = Rig(engine)
    host = MainThreadHost()
    host.broken = True
    r.router = CommandRouter(FakeController(executor=inline), r.app, host, UploadRegistry())
    try:
        assert r.offered()
        r.run(type="oauthStart", provider="apple")
        assert wait_for(lambda: r.auth()["oauthError"] is not None, timeout=3)
        assert r.auth()["oauthError"]["type"] == "oauth_browser_unavailable"
    finally:
        r.close()


def test_an_engine_error_keeps_its_type(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    # A T2VError that is not a ProviderSignInError reaches oauthError as it is.
    from talk2view.errors import T2VError

    def refused(*args: Any, **kwargs: Any) -> Any:
        raise T2VError("Too many sign-ins. Try again later.", "rate_limited", 429)

    assert rig.offered()
    monkeypatch.setattr(rig.t2v.auth, "sign_in_with_provider", refused)
    rig.run(type="oauthStart", provider="google")
    assert wait_for(lambda: rig.auth()["oauthError"] is not None)
    assert rig.auth()["oauthError"] == {"type": "rate_limited",
                                        "message": "Too many sign-ins. Try again later."}


def test_a_host_slow_to_answer_is_taken_to_open_the_browser(
    engine: FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No answer within OPEN_WAIT_S is not a refusal: the sign-in keeps waiting.
    import talk2view.ui.provider_sign_in as provider_sign_in

    monkeypatch.setattr(provider_sign_in, "OPEN_WAIT_S", 0.1)
    posted = threading.Event()

    class BusyHost(BrowserHost):
        def post(self, fn: Callable[[], None]) -> None:
            posted.set()

    engine.desktop_providers = ["apple", "google"]
    r = Rig(engine)
    r.router = CommandRouter(FakeController(executor=inline), r.app, BusyHost(), UploadRegistry())
    try:
        assert r.offered()
        r.run(type="oauthStart", provider="apple")
        assert posted.wait(3)
        assert not wait_for(lambda: r.auth()["oauthPending"] is None, timeout=0.5)
        assert r.auth()["oauthError"] is None
        r.run(type="oauthCancel")
        assert wait_for(lambda: r.auth()["oauthPending"] is None)
    finally:
        r.close()


def test_a_closed_provider_sign_in_starts_nothing(rig: Rig, engine: FakeEngine) -> None:
    # Once closed (the router stopped), a start is refused and nothing is begun.
    from talk2view.ui.provider_sign_in import ProviderSignIn

    assert rig.offered()
    signer = ProviderSignIn(rig.app, rig.host)
    signer.close()
    assert signer.start(rig.t2v, "apple") is False
    assert rig.auth()["oauthPending"] is None
    assert engine.desktop_requests == []


def test_stop_returns_after_the_sign_in_thread_has_ended(rig: Rig) -> None:
    # stop() waits for the thread (bounded), so none is left the moment it returns.
    assert rig.offered()
    rig.host.follow = False
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: len(rig.host.opened) == 1)
    rig.router.stop()
    assert not any(t.name == "t2v-ui-oauth" for t in threading.enumerate())
    assert rig.auth()["oauthPending"] is None


def test_the_probe_is_retried_at_most_probe_retries_times(engine: FakeEngine,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    # Three retries in a row, then left alone; a success resets the count.
    import talk2view.ui.app_state as app_state

    monkeypatch.setattr(app_state, "PROBE_RETRY_S", 0.05)
    engine.desktop_providers = ["apple", "google"]
    engine.desktop_probe_status = 503
    r = Rig(engine)
    try:
        assert wait_for(lambda: engine.desktop_probes >= 1 + app_state.PROBE_RETRIES)
        assert not wait_for(lambda: engine.desktop_probes > 1 + app_state.PROBE_RETRIES,
                            timeout=0.5)
        engine.desktop_probe_status = None
        r.app.refresh()
        assert r.offered()
        before = engine.desktop_probes
        engine.desktop_probe_status = 503
        r.app.refresh()  # the count starts again after the success
        assert wait_for(lambda: engine.desktop_probes == before + 1 + app_state.PROBE_RETRIES)
    finally:
        r.close()


def test_detach_cancels_the_probe_retry(engine: FakeEngine,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    # No retry timer outlives the AppState.
    import talk2view.ui.app_state as app_state

    monkeypatch.setattr(app_state, "PROBE_RETRY_S", 30.0)
    engine.desktop_providers = ["apple", "google"]
    engine.desktop_probe_status = 503
    r = Rig(engine)

    def timers() -> List[threading.Thread]:
        return [t for t in threading.enumerate() if isinstance(t, threading.Timer)
                and getattr(t, "function", None) == r.app.refresh and t.is_alive()]

    try:
        assert wait_for(lambda: len(timers()) == 1)
        r.app.detach()
        assert wait_for(lambda: timers() == [], timeout=2)
    finally:
        r.close()


def record_slices(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """Every slice the hub is handed from now on, in order, as (key, value)."""
    seen: List[Any] = []
    original = rig.hub.set_slice

    def recording(key: str, value: Any) -> Any:
        seen.append((key, value))
        return original(key, value)

    monkeypatch.setattr(rig.hub, "set_slice", recording)
    return seen


def test_the_account_is_shown_before_the_pending_mark_goes(rig: Rig,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    # The page returns to the chat when oauthPending clears with an account on show: the
    # auth slice must carry the account BEFORE (or together with) the pending mark going,
    # never a signed-out slice with no pending mark in between.
    assert rig.offered()
    seen = record_slices(rig, monkeypatch)
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: rig.auth()["isAuthenticated"] and rig.auth()["oauthPending"] is None)
    auths = [value for key, value in seen if key == "auth"]
    assert auths[0]["oauthPending"] == {"provider": "apple"}
    cleared = next(i for i, a in enumerate(auths) if a["oauthPending"] is None)
    assert auths[cleared]["isAuthenticated"] is True
    assert auths[cleared]["user"]["id"] == "u-desktop"
    signed_in = next(i for i, a in enumerate(auths) if a["isAuthenticated"])
    assert signed_in < cleared  # the account arrived while still pending
    assert auths[signed_in]["oauthPending"] == {"provider": "apple"}
    assert all(a["oauthError"] is None for a in auths)


def test_a_switch_turned_off_mid_attempt_records_the_error(rig: Rig, engine: FakeEngine,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    # The buttons go (capabilities first), and the failure IS recorded in oauthError,
    # together with the pending mark going.
    assert rig.offered()
    rig.host.follow = False
    seen = record_slices(rig, monkeypatch)
    rig.run(type="oauthStart", provider="apple")
    assert wait_for(lambda: len(rig.host.opened) == 1)
    target = open_check_screen(rig.host.opened[0])  # the switch goes off before Continue
    engine.desktop_providers = []
    follow_in_browser(target)
    assert wait_for(lambda: rig.auth()["oauthError"] is not None)
    off = next(i for i, (k, v) in enumerate(seen) if k == "capabilities" and not v["oauth"])
    failed = next(i for i, (k, v) in enumerate(seen)
                  if k == "auth" and v["oauthError"] is not None)
    assert off < failed
    assert seen[failed][1]["oauthError"]["type"] == "desktop_sign_in_disabled"
    assert seen[failed][1]["oauthPending"] is None
    assert rig.auth()["oauthError"]["message"]
