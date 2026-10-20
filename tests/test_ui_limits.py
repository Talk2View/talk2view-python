"""The ``limits`` slice: the two ways a guest is turned away, as TS
``chat/auth-gate.tsx`` tracks them, and the page flow that follows: gate, sign in, reload."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view import DEMO_LIMIT_REACHED, ChatController, ChatEvent, MemoryStorage, Talk2View
from talk2view.ui import AppState, GenericHost, UIServer
from talk2view.ui.app_state import APP_KEYS
from talk2view.ui.hub import EventHub
from talk2view.ui.router import CommandRouter, UploadRegistry
from talk2view.ui.testing import FakeController
from talk2view.ui.wire import parse_command

from .test_ui_e2e import STUB, Wire
from .ui_engine import AccountEngine

NO_LIMITS = {"demoLimitReached": False, "anonymousUnavailable": None}


def inline(job: Callable[[], None]) -> None:
    job()


class LimitEngine(AccountEngine):
    """``AccountEngine`` whose guests run out of allowance: a guest's message holding
    "budget" gets the engine's ``budget_exceeded`` error chunk. An account's does not."""

    def _handle(self, request: Request) -> Response:
        if request.path.endswith("/messages"):
            auth = request.headers.get("Authorization", "")
            with self.lock:
                user_id = self.tokens.get(auth[len("Bearer "):], "")
            messages = (request.get_json(silent=True) or {}).get("messages") or [{}]
            if user_id.startswith("anon-") and "budget" in str(messages[-1].get("content")):
                chunk = {
                    "id": "c",
                    "choices": [{"index": 0, "delta": {"content": "Demo limit reached"},
                                 "finish_reason": "stop"}],
                    "error": {"type": "budget_exceeded", "message": "Demo limit reached"},
                }
                body = ["data: " + json.dumps(chunk) + "\n\n", "data: [DONE]\n\n"]
                return Response(iter(body), mimetype="text/event-stream")
        return super()._handle(request)


@pytest.fixture
def limit_engine(fake_engine_server: HTTPServer) -> Iterator[LimitEngine]:
    fake = LimitEngine(fake_engine_server).start()
    yield fake
    fake.stop()


@pytest.fixture
def t2v(account_engine: AccountEngine) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=account_engine.url(), anonymous_auto_start=True)
    yield client
    client.close()


def limits(hub: EventHub) -> Dict[str, Any]:
    value: Dict[str, Any] = hub.snapshot()["state"]["limits"]
    return value


def attached(t2v: Optional[Talk2View], controller: Any = None) -> Tuple[EventHub, AppState]:
    hub = EventHub()
    app = AppState(t2v, GenericHost())
    app.attach(hub, controller)
    return hub, app


def guest_over_budget(t2v: Talk2View) -> ChatController:
    """A guest whose next turn hits the demo limit (the engine's ``budget_exceeded``)."""
    t2v.auth.start_anonymous()
    t2v.chat = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda *a, **k: iter(
            [ChatEvent(type="error", message="Budget exceeded", error_type="budget_exceeded")]
        )
    )
    return ChatController(t2v, executor=inline)


def set_paths(hub: EventHub, action: Callable[[], None]) -> List[str]:
    """The top-level key of every ``set`` op ``action`` makes, in ``seq`` order."""
    client = hub.attach(hub.seq)
    try:
        action()
        keys: List[str] = []
        while True:
            event = hub.next_event(client, 0.2)
            if event is None or event[0] == "hb":
                return keys
            keys += [op["path"][0] for op in json.loads(event[2])["ops"] if op["type"] == "set"]
    finally:
        hub.detach(client)


def gated(state: Dict[str, Any]) -> bool:
    """TS ``useAuthGate``, as the page computes it from the state."""
    signed_in = state["auth"]["isAuthenticated"]
    refused = state["limits"]["anonymousUnavailable"] is not None
    return (not signed_in and (not state["options"]["allowAnonymous"] or refused)) or state[
        "limits"
    ]["demoLimitReached"]


# ── The slice ──


def test_attach_seeds_no_limits() -> None:
    hub, app = attached(None)
    try:
        assert "limits" in APP_KEYS
        assert limits(hub) == NO_LIMITS
    finally:
        app.detach()


def test_a_refused_guest_start_sets_anonymous_unavailable(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    account_engine.anonymous_refusal = (403, "captcha_required")
    hub, app = attached(t2v)
    try:
        assert t2v.ensure_session() is False
        assert limits(hub) == {
            "demoLimitReached": False,
            "anonymousUnavailable": {"type": "captcha_required"},
        }
    finally:
        app.detach()


def test_the_start_anonymous_command_sets_it_too(
    account_engine: AccountEngine,
) -> None:
    account_engine.anonymous_refusal = (403, "anonymous_access_disabled")
    t2v = Talk2View("pk_test", base_url=account_engine.url())  # no auto-start: the page asks
    hub, app = attached(t2v)
    router = CommandRouter(FakeController(executor=inline), app, GenericHost(), UploadRegistry())
    try:
        result = router.execute(parse_command({"type": "startAnonymous", "id": "g"}))
        assert result["error"]["type"] == "anonymous_access_disabled"
        assert limits(hub)["anonymousUnavailable"] == {"type": "anonymous_access_disabled"}
    finally:
        router.stop()
        app.detach()
        t2v.close()


def test_a_refusal_that_a_sign_in_overtook_is_not_kept(t2v: Talk2View) -> None:
    hub, app = attached(t2v)
    try:
        t2v.auth.login("ana@example.com", "pw")
        app.note_anonymous_unavailable("captcha_required")
        assert limits(hub) == NO_LIMITS
    finally:
        app.detach()


def test_a_refusal_while_a_guest_is_signed_in_is_kept_until_the_next_auth_change(
    t2v: Talk2View,
) -> None:
    """Only a real account makes a refusal moot. A guest is not gated by it while signed
    in (the page's gate needs signed out), and signing out lifts it."""
    hub, app = attached(t2v)
    try:
        t2v.auth.start_anonymous()
        app.note_anonymous_unavailable("captcha_required")
        assert limits(hub)["anonymousUnavailable"] == {"type": "captcha_required"}
        assert not gated(hub.snapshot()["state"])
        t2v.auth.logout()
        assert limits(hub) == NO_LIMITS
    finally:
        app.detach()


class FlakyStorage(MemoryStorage):
    """Reads raise once ``broken`` is set (a ``FileStorage`` sharing violation, say)."""

    broken = False

    def get(self, key: str) -> Optional[str]:
        if self.broken:
            raise OSError("sharing violation")
        return super().get(key)


def test_a_refusal_with_an_unreadable_token_store_still_gates(
    account_engine: AccountEngine,
) -> None:
    """Fail closed: only a store that reads as a real account makes a refusal moot. The auth
    slice shows an unreadable store as signed out, so the gate must open with it."""
    account_engine.anonymous_refusal = (403, "captcha_required")
    storage = FlakyStorage()
    t2v = Talk2View("pk_test", base_url=account_engine.url(), storage=storage)
    # Ahead of the app's listener: the store turns unreadable as the refusal is announced.
    t2v.on_anonymous_unavailable(lambda _type: setattr(storage, "broken", True))
    hub, app = attached(t2v)
    try:
        assert t2v.ensure_session() is False
        assert storage.broken
        assert limits(hub) == {
            "demoLimitReached": False,
            "anonymousUnavailable": {"type": "captcha_required"},
        }
    finally:
        storage.broken = False
        app.detach()
        t2v.close()


def test_the_demo_limit_notice_sets_demo_limit_reached(t2v: Talk2View) -> None:
    controller = guest_over_budget(t2v)
    hub, app = attached(t2v, controller)
    try:
        controller.send("hello")
        assert limits(hub) == {"demoLimitReached": True, "anonymousUnavailable": None}
        assert controller.snapshot()["error"] is None  # a notice, not an error
    finally:
        app.detach()
        controller.close()


def test_signing_in_to_an_account_lifts_the_limits_after_showing_it(t2v: Talk2View) -> None:
    controller = guest_over_budget(t2v)
    hub, app = attached(t2v, controller)
    try:
        controller.send("hello")
        assert limits(hub)["demoLimitReached"] is True
        keys = set_paths(hub, lambda: t2v.auth.login("ana@example.com", "pw"))
        assert limits(hub) == NO_LIMITS
        # The account first, then the lifted limits: the page's cue to send `reload`.
        assert keys.index("auth") < keys.index("limits")
    finally:
        app.detach()
        controller.close()


class ConvertedEngine(AccountEngine):
    """Signing in hands back the latest guest's own user id, no longer a guest: the demo
    became this account (``signup()``'s convert, confirmed by email, then a sign-in)."""

    def _handle(self, request: Request) -> Response:
        if request.path == "/v1/auth/login":
            with self.lock:
                return self._issue("anon-{}".format(self._anonymous), "ana@example.com", False)
        return super()._handle(request)


def test_the_demo_becoming_an_account_lifts_them_too(fake_engine_server: HTTPServer) -> None:
    engine = ConvertedEngine(fake_engine_server).start()
    t2v = Talk2View("pk_test", base_url=engine.url())
    controller = guest_over_budget(t2v)
    hub, app = attached(t2v, controller)
    try:
        controller.send("hello")
        assert limits(hub)["demoLimitReached"] is True
        guest = t2v.auth.get_user()
        t2v.auth.login("ana@example.com", "pw")
        user = t2v.auth.get_user()
        # The same end-user, no longer a guest: no identity change, a real account on show.
        assert guest is not None and user is not None and user.id == guest.id
        assert not t2v.auth.is_anonymous()
        assert limits(hub) == NO_LIMITS
    finally:
        app.detach()
        controller.close()
        t2v.close()
        engine.stop()


def test_a_sign_out_lifts_them_too(t2v: Talk2View) -> None:
    controller = guest_over_budget(t2v)
    hub, app = attached(t2v, controller)
    try:
        controller.send("hello")
        t2v.auth.logout()
        assert limits(hub) == NO_LIMITS
    finally:
        app.detach()
        controller.close()


def test_detach_stops_following_both_sources(t2v: Talk2View) -> None:
    controller = guest_over_budget(t2v)
    hub, app = attached(t2v, controller)
    app.detach()
    assert controller._listeners[DEMO_LIMIT_REACHED] == []
    assert t2v._anonymous_unavailable_listeners == []
    controller.close()


def test_a_controller_without_notices_feeds_nothing() -> None:
    hub, app = attached(None, object())
    try:
        assert limits(hub) == NO_LIMITS
    finally:
        app.detach()


# ── FakeController: `/limit` ──


def test_the_fake_controller_scripts_the_demo_limit() -> None:
    controller = FakeController(executor=inline)
    notices: List[str] = []
    off = controller.on(DEMO_LIMIT_REACHED, lambda: notices.append("limit"))
    controller.send("/limit please")
    state = controller.snapshot()
    assert notices == ["limit"]
    assert [m["role"] for m in state["messages"]] == ["user"]  # the empty reply is dropped
    assert state["isLoading"] is False and state["error"] is None
    off()
    controller.send("/limit again")
    assert notices == ["limit"]


def test_the_fake_controllers_notice_runs_before_its_batch_reaches_the_subscribers() -> None:
    """As ``ChatController.on`` promises: the page gets the limit before the batch that ends
    the turn (the page's runtime guard runs against the fake)."""
    controller = FakeController(executor=inline)
    seen: List[str] = []
    controller.on(DEMO_LIMIT_REACHED, lambda: seen.append("notice"))

    def on_batch(batch: Any) -> None:
        loading = [op["value"] for op in batch["ops"] if op["path"] == ["isLoading"]]
        seen.append("turn ends" if loading == [False] else "batch")

    controller.subscribe(on_batch)
    controller.send("/limit")
    assert seen == ["batch", "notice", "turn ends"]


def test_the_fake_controller_behind_a_server_fills_the_slice() -> None:
    controller = FakeController(executor=inline)
    server = UIServer(controller, GenericHost(), static_dir=STUB)
    server.start()
    try:
        controller.send("/limit")
        assert server._hub.snapshot()["state"]["limits"]["demoLimitReached"] is True
    finally:
        server.stop()


# ── The page flow over the wire: gate, sign in, reload ──


@pytest.mark.parametrize("limit", ["anonymous-unavailable", "demo-limit"])
def test_gate_sign_in_and_reload(limit_engine: LimitEngine, limit: str) -> None:
    refused = limit == "anonymous-unavailable"
    if refused:
        limit_engine.anonymous_refusal = (403, "captcha_required")
    t2v = Talk2View("pk_test", base_url=limit_engine.url(), anonymous_auto_start=refused)
    controller = ChatController(t2v)
    server = UIServer(controller, GenericHost(), static_dir=STUB)
    server.start()
    wire = Wire(server)
    text = "hello" if refused else "over budget"
    try:
        assert wire.page.wait_until(lambda s: "limits" in s and "messages" in s)
        if not refused:
            wire.command(type="startAnonymous", id="guest")
            assert wire.result("guest")["ok"] is True
        wire.command(type="send", text=text, id="s1")
        assert wire.page.wait_until(lambda s: gated(s) and not s["isLoading"])
        state = wire.page.state()
        assert [m["content"] for m in state["messages"]] == [text]
        if refused:
            assert state["limits"]["anonymousUnavailable"] == {"type": "captcha_required"}
            assert state["error"] == "Sign in to continue."
        else:
            assert state["limits"]["demoLimitReached"] is True and state["error"] is None

        wire.command(type="login", email="ana@example.com", password="pw", id="in")
        assert wire.page.wait_until(
            lambda s: s["auth"]["isAuthenticated"] and not s["auth"]["isAnonymous"]
            and s["limits"] == NO_LIMITS
        )
        assert not gated(wire.page.state())
        wire.command(type="reload", id="again")
        assert wire.page.wait_until(
            lambda s: [m["content"] for m in s["messages"]] == [text, "ok"]
            and not s["isLoading"]
        )
        assert wire.result("again")["ok"] is True
        assert wire.page.state()["error"] is None
        assert wire.page.error is None
    finally:
        server.stop()
        controller.close()
        t2v.close()
