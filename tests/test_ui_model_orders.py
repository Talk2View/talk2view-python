"""The three model mechanisms together, in every order the page can reach: a pick, "Default"
(``DEFAULT_MODEL``'s recycle, consumed only on a confirmed ``DELETE``) and a pick made while
a session is being created (the defaults' version count). A real ``Talk2View``, ``ChatController``,
``AppState`` and ``CommandRouter`` (``execute()`` runs on the test thread, as it would on the
router's), against an engine that keeps a session's model. Each test ends on the model the
page shows, with one recycle where one is due and none where none is.

A pick while an approval is pending keeps the session the approval waits on: the approve
runs the tool, and the next turn switches to the pick."""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, Iterator, List, Optional, Set

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view import Capabilities, ChatController, Talk2View, tool
from talk2view.ui import AppState, GenericHost, UIServer
from talk2view.ui.hub import EventHub
from talk2view.ui.router import CommandRouter, UploadRegistry
from talk2view.ui.wire import parse_command

from .fake_engine import TOOL
from .test_ui_e2e import STUB
from .ui_engine import AccountEngine
from .ui_helpers import wait_for

PARTNER_DEFAULT = "m-fast"


class ModelEngine(AccountEngine):
    """``AccountEngine`` that keeps each session's model under the engine's rule
    (``resolve_session_model``): a new session starts on the partner's default, a create or
    a message naming a model switches the session to it, and one naming none keeps it.

    ``calls`` keeps the session routes in order, as ``"<METHOD> <route>"`` with the route
    one of ``create``, ``delete``, ``message`` and ``resume``; ``turns`` keeps the model each
    message ran on. The creates whose 0-based index is in ``hold_creates`` wait for
    ``release``, after setting ``creating``."""

    def __init__(self, server: HTTPServer) -> None:
        super().__init__(server)
        self.session_models: Dict[str, str] = {}
        self.turns: List[str] = []
        self.calls: List[str] = []
        self.resumed: List[str] = []  # the session id each resume named
        self.hold_creates: Set[int] = set()
        self.creates = 0
        self.creating = threading.Event()
        self.release = threading.Event()

    def _handle(self, request: Request) -> Response:
        body = request.get_json(silent=True)
        requested = body.get("model") if isinstance(body, dict) else None
        parts = request.path.split("/")
        route = None
        if request.path == "/v1/sessions" and request.method == "POST":
            route = "create"
        elif len(parts) == 4 and parts[2] == "sessions" and request.method == "DELETE":
            route = "delete"
        elif len(parts) == 5 and parts[2] == "sessions" and parts[4] in ("messages", "resume"):
            route = "message" if parts[4] == "messages" else "resume"
        if route is not None:
            with self.lock:
                self.calls.append("{} {}".format(request.method, route))
                if route == "resume":
                    self.resumed.append(parts[3])
        if route == "create":
            with self.lock:
                index = self.creates
                self.creates += 1
            if index in self.hold_creates:
                self.creating.set()
                self.release.wait(10)
        response = super()._handle(request)
        if response.status_code != 200 or route not in ("create", "message"):
            return response
        session_id = parts[3] if route == "message" else json.loads(response.get_data())[
            "session_id"
        ]
        with self.lock:
            self.session_models.setdefault(session_id, PARTNER_DEFAULT)
            if requested:
                self.session_models[session_id] = requested
            if route == "message":
                self.turns.append(self.session_models[session_id])
        return response

    def take(self) -> List[str]:
        """The session calls since the last ``take``, as their HTTP methods."""
        with self.lock:
            taken, self.calls = self.calls, []
        return [call.split()[0] for call in taken]


class Rig:
    """The page's stack over one ``Talk2View``, with the ``delete_volume`` client tool
    registered behind ``permission=True`` (a message holding "delete" asks for it)."""

    def __init__(self, engine: ModelEngine, *, model: Optional[str] = None) -> None:
        self.engine = engine
        self.ran: List[str] = []
        ran = self.ran

        @tool(permission=True)
        def delete_volume(name: str) -> str:
            """Delete a volume.

            Args:
                name: The volume.
            """
            ran.append(name)
            return "deleted"

        self.t2v = Talk2View("pk_test", base_url=engine.url(), model=model)
        self.t2v.tools.register_from_functions([delete_volume])
        self.controller = ChatController(self.t2v)
        self.host = GenericHost()
        self.hub, self.app = EventHub(), AppState(self.t2v, self.host)
        self.app.attach(self.hub, self.controller)
        self.router = CommandRouter(self.controller, self.app, self.host, UploadRegistry())
        self._ids = 0

    def cmd(self, **body: Any) -> Dict[str, Any]:
        self._ids += 1
        result: Dict[str, Any] = self.router.execute(
            parse_command(dict(body, id="c{}".format(self._ids)))
        )
        assert result["ok"], result
        return result

    def models(self) -> List[str]:
        return [m["id"] for m in self.hub.snapshot()["state"]["models"]["list"]]

    def selected(self) -> Optional[str]:
        value: Optional[str] = self.hub.snapshot()["state"]["models"]["selected"]
        return value

    def login(self) -> None:
        self.t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: self.models() == ["m-fast", "m-deep"])

    def pick(self, model_id: Optional[str]) -> None:
        self.cmd(type="selectModel", modelId=model_id)

    def send(self, text: str = "hi") -> None:
        """A whole turn, which must end with no error."""
        self.cmd(type="send", text=text)
        self.idle()

    def idle(self) -> None:
        assert self.controller.wait_idle(10)
        assert self.controller.snapshot()["error"] is None

    def close(self) -> None:
        self.engine.release.set()
        self.router.stop()
        self.app.detach()
        self.controller.close()
        self.t2v.close()


@pytest.fixture
def engine(fake_engine_server: HTTPServer) -> Iterator[ModelEngine]:
    fake = ModelEngine(fake_engine_server).start()
    try:
        yield fake
    finally:
        fake.release.set()
        fake.stop()


@pytest.fixture
def rig(engine: ModelEngine) -> Iterator[Rig]:
    stack = Rig(engine)
    try:
        yield stack
    finally:
        stack.close()


# ── A pick while an approval is pending ──


def test_approving_after_a_pick_runs_the_tool_and_the_next_turn_uses_the_pick(
    rig: Rig, engine: ModelEngine
) -> None:
    rig.login()
    rig.send("delete CT")
    pending = rig.controller.snapshot()["pendingApproval"]
    assert pending is not None and pending["toolName"] == TOOL
    asked_on = rig.t2v.get_session()
    assert asked_on is not None
    rig.pick("m-deep")  # Settings stays open while the card waits (TS)
    engine.take()
    rig.cmd(type="approve", toolCallId=pending["toolCallId"], action="once")
    rig.idle()
    assert rig.ran == ["CT"]
    assert rig.controller.snapshot()["pendingApproval"] is None
    assert engine.take() == ["POST"] and engine.resumed == [asked_on.id]
    rig.send("next")
    # The session the approval waited on was built before the pick: the next turn's create
    # switches it (the engine keeps one per end-user), and the conversation carries on.
    assert engine.take() == ["POST", "POST"]
    assert engine.turns == [PARTNER_DEFAULT, "m-deep"]
    assert rig.selected() == "m-deep"


def test_approving_after_a_pick_then_default_takes_the_short_path(
    rig: Rig, engine: ModelEngine
) -> None:
    rig.login()
    rig.pick("m-deep")
    rig.send("delete CT")
    pending = rig.controller.snapshot()["pendingApproval"]
    assert pending is not None
    rig.pick("m-fast")
    rig.cmd(type="approve", toolCallId=pending["toolCallId"], action="once")
    rig.idle()
    assert rig.ran == ["CT"]
    rig.pick(None)
    engine.take()
    rig.send("next")
    assert engine.take() == ["DELETE", "POST", "POST"]
    assert engine.turns == ["m-deep", PARTNER_DEFAULT]
    assert rig.selected() is None


def test_default_while_an_approval_is_pending_applies_after_it(
    rig: Rig, engine: ModelEngine
) -> None:
    rig.login()
    rig.pick("m-deep")
    rig.send("delete CT")
    pending = rig.controller.snapshot()["pendingApproval"]
    assert pending is not None
    rig.pick(None)
    rig.cmd(type="approve", toolCallId=pending["toolCallId"], action="once")
    rig.idle()
    assert rig.ran == ["CT"]
    engine.take()
    rig.send("next")
    assert engine.take() == ["DELETE", "POST", "POST"]
    assert engine.turns == ["m-deep", PARTNER_DEFAULT]


def test_a_pick_then_default_on_a_held_session_takes_the_short_path(
    rig: Rig, engine: ModelEngine
) -> None:
    """A pick keeps the session object (stale for the next turn); Default then deletes it
    by id before the create, as when nothing was picked in between."""
    rig.login()
    rig.send("one")
    rig.pick("m-deep")
    rig.pick(None)
    engine.take()
    rig.send("two")
    assert engine.take() == ["DELETE", "POST", "POST"]
    assert engine.turns == [PARTNER_DEFAULT, PARTNER_DEFAULT]
    rig.send("three")
    assert engine.take() == ["POST"]


# ── Twelve more orders ──


def test_s1_pick_then_default_then_a_turn(rig: Rig, engine: ModelEngine) -> None:
    rig.login()
    rig.pick("m-deep")
    rig.pick(None)
    assert rig.selected() is None
    engine.take()
    rig.send()
    # No session held: the create hands back one the engine may keep, recycled once.
    assert engine.take() == ["POST", "DELETE", "POST", "POST"]
    assert engine.turns == [PARTNER_DEFAULT]
    rig.send()
    assert engine.take() == ["POST"]
    rig.pick("m-deep")
    rig.send()
    assert engine.take() == ["POST", "POST"]
    assert engine.turns == [PARTNER_DEFAULT, PARTNER_DEFAULT, "m-deep"]


def test_s2_a_turn_on_a_pick_then_default(rig: Rig, engine: ModelEngine) -> None:
    rig.login()
    rig.pick("m-deep")
    rig.send()
    rig.pick(None)
    engine.take()
    rig.send()
    assert engine.take() == ["DELETE", "POST", "POST"]
    assert engine.turns == ["m-deep", PARTNER_DEFAULT]
    rig.send()
    assert engine.take() == ["POST"]


def test_s3_default_while_the_turns_create_is_in_flight(rig: Rig, engine: ModelEngine) -> None:
    rig.login()
    rig.pick("m-deep")
    engine.hold_creates = {0}
    rig.cmd(type="send", text="one")
    assert engine.creating.wait(5)
    rig.pick(None)
    engine.release.set()
    rig.idle()
    rig.send("two")
    assert engine.turns == [PARTNER_DEFAULT, PARTNER_DEFAULT]
    assert rig.t2v._fresh_session is False


def test_s4_default_while_a_turn_streams(rig: Rig, engine: ModelEngine) -> None:
    rig.login()
    rig.pick("m-deep")
    rig.cmd(type="send", text="slow please")
    assert engine.slow_started.wait(5)
    rig.pick(None)
    engine.release_slow.set()
    rig.idle()
    engine.take()
    rig.send("two")
    # The streaming turn kept its model; the next one starts on a fresh session.
    assert engine.take() == ["DELETE", "POST", "POST"]
    assert engine.turns == ["m-deep", PARTNER_DEFAULT]


def test_s5_a_pick_and_default_during_the_first_create(rig: Rig, engine: ModelEngine) -> None:
    rig.login()
    engine.hold_creates = {0}
    rig.cmd(type="send", text="one")
    assert engine.creating.wait(5)
    rig.pick("m-deep")
    rig.pick(None)
    engine.release.set()
    rig.idle()
    assert engine.take() == ["POST", "DELETE", "POST", "POST"]
    rig.send("two")
    assert engine.take() == ["POST"]
    assert engine.turns == [PARTNER_DEFAULT, PARTNER_DEFAULT]


def test_s6_a_pick_during_the_create_applies_next_turn_then_default(
    rig: Rig, engine: ModelEngine
) -> None:
    rig.login()
    engine.hold_creates = {0}
    rig.cmd(type="send", text="one")
    assert engine.creating.wait(5)
    rig.pick("m-deep")
    engine.release.set()
    rig.idle()
    rig.send("two")
    assert engine.turns == [PARTNER_DEFAULT, "m-deep"]
    rig.pick(None)
    engine.take()
    rig.send("three")
    assert engine.take() == ["DELETE", "POST", "POST"]
    assert engine.turns == [PARTNER_DEFAULT, "m-deep", PARTNER_DEFAULT]


def test_s7_a_guests_pending_reset_carries_across_a_login(rig: Rig, engine: ModelEngine) -> None:
    rig.cmd(type="startAnonymous")
    assert wait_for(lambda: rig.models() == ["m-fast", "m-deep"])
    rig.pick("m-deep")
    rig.send("guest turn")
    assert engine.turns == ["m-deep"]
    rig.pick(None)
    assert rig.t2v._fresh_session is True
    rig.cmd(type="login", email="ana@example.com", password="pw")
    assert wait_for(lambda: rig.models() == ["m-fast", "m-deep"])
    engine.take()
    rig.send("account turn")
    # The guest's session object went with the guest; the account's create is recycled.
    assert engine.take() == ["POST", "DELETE", "POST", "POST"]
    assert engine.turns == ["m-deep", PARTNER_DEFAULT]
    rig.send("again")
    assert engine.take() == ["POST"]


def test_s8_a_capabilities_change_with_a_reset_pending(rig: Rig, engine: ModelEngine) -> None:
    rig.login()
    rig.pick("m-deep")
    rig.send("one")
    rig.pick(None)
    rig.t2v.set_chat_defaults(capabilities=Capabilities(reasoning=True))  # the host
    engine.take()
    rig.send("two")
    assert engine.take() == ["DELETE", "POST", "POST"]  # one DELETE, by the held id
    assert engine.turns == ["m-deep", PARTNER_DEFAULT]
    rig.send("three")
    assert engine.take() == ["POST"]


def test_s9_pick_default_and_the_same_pick_again(rig: Rig, engine: ModelEngine) -> None:
    rig.login()
    rig.pick("m-deep")
    rig.send("one")
    rig.pick(None)
    rig.pick("m-deep")
    engine.take()
    rig.send("two")
    assert "DELETE" not in engine.take()
    assert engine.turns == ["m-deep", "m-deep"]


def test_s10_a_pick_during_the_recycles_create(rig: Rig, engine: ModelEngine) -> None:
    rig.login()
    rig.pick("m-deep")
    rig.send("one")
    rig.pick(None)
    engine.hold_creates = {1}  # turn two: the DELETE, then this create
    rig.cmd(type="send", text="two")
    assert engine.creating.wait(5)
    rig.pick("m-deep")
    engine.release.set()
    rig.idle()
    engine.take()
    rig.send("three")
    assert engine.take() == ["POST", "POST"]
    assert engine.turns == ["m-deep", PARTNER_DEFAULT, "m-deep"]


def test_s11_default_goes_to_the_partners_model_not_the_hosts(engine: ModelEngine) -> None:
    rig = Rig(engine, model="m-deep")
    try:
        rig.login()
        rig.send("one")
        rig.pick(None)  # nothing of the end-user's was applied: the host's model stays
        rig.send("two")
        rig.pick("m-fast")
        rig.send("three")
        rig.pick(None)
        rig.send("four")
        assert engine.turns == ["m-deep", "m-deep", "m-fast", PARTNER_DEFAULT]
    finally:
        rig.close()


def test_s12_a_second_default_during_the_recycles_create(rig: Rig, engine: ModelEngine) -> None:
    rig.login()
    rig.pick("m-deep")
    rig.send("one")
    rig.pick(None)
    engine.hold_creates = {1}
    rig.cmd(type="send", text="two")
    assert engine.creating.wait(5)
    rig.pick(None)
    engine.release.set()
    rig.idle()
    engine.take()
    rig.send("three")
    assert engine.take() == ["POST"]  # no second recycle
    assert engine.turns == ["m-deep", PARTNER_DEFAULT, PARTNER_DEFAULT]


# ── A UI server rebuilt over the same Talk2View ──


def run(server: UIServer, **body: Any) -> None:
    """A page command on this thread, as the server's router thread runs it."""
    result = server._router.execute(parse_command(dict(body, id="x")))
    assert result["ok"], result


def page(server: UIServer) -> Dict[str, Any]:
    state: Dict[str, Any] = server._hub.snapshot()["state"]
    return state


def offline() -> Any:
    raise OSError("offline")


def turn(server: UIServer, controller: ChatController, text: str) -> None:
    run(server, type="send", text=text)
    assert controller.wait_idle(10)
    assert controller.snapshot()["error"] is None


def test_default_takes_back_a_pick_an_earlier_server_applied(engine: ModelEngine) -> None:
    """The host closes its chat panel and opens it again: a new ``UIServer`` (a new
    ``AppState``) over the same ``Talk2View``, whose first model list fails. The pick the
    first one applied is still live, and it is the end-user's own: "Default" takes it back
    and it stays taken back once the list is read."""
    t2v = Talk2View("pk_test", base_url=engine.url())
    controller = ChatController(t2v)
    host = GenericHost()
    first = UIServer(controller, host, static_dir=STUB)
    first.start()
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: page(first)["models"]["list"] != [])
        run(first, type="selectModel", modelId="m-deep")
        turn(first, controller, "one")
    finally:
        first.stop()
    t2v.list_models = offline  # type: ignore[method-assign]
    second = UIServer(controller, host, static_dir=STUB)
    second.start()
    try:
        assert wait_for(lambda: page(second)["config"] is not None)  # its refresh has run
        assert page(second)["models"] == {"list": [], "selected": None}
        assert page(second)["preferences"] == {"model": "m-deep"}
        run(second, type="selectModel", modelId=None)
        turn(second, controller, "two")
        del t2v.list_models  # the class's own again: the list is read
        second._app.refresh()
        assert wait_for(lambda: page(second)["models"]["list"] != [])
        assert page(second)["models"]["selected"] is None
        turn(second, controller, "three")
        assert engine.turns == ["m-deep", PARTNER_DEFAULT, PARTNER_DEFAULT]
    finally:
        second.stop()
        controller.close()
        t2v.close()


def test_default_leaves_a_host_model_that_no_preference_names(engine: ModelEngine) -> None:
    """A model the host set is not the end-user's to take back, even when a pick
    of theirs (a different model, never applied here) is stored."""
    t2v = Talk2View("pk_test", base_url=engine.url(), model="m-deep")
    t2v.preferences.update(model="m-fast")
    t2v.list_models = offline  # type: ignore[method-assign]
    controller = ChatController(t2v)
    server = UIServer(controller, GenericHost(), static_dir=STUB)
    server.start()
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: page(server)["config"] is not None)
        run(server, type="selectModel", modelId=None)
        assert page(server)["preferences"] == {}
        turn(server, controller, "one")
        assert engine.turns == ["m-deep"]
        assert t2v._model == "m-deep" and t2v._fresh_session is False
    finally:
        server.stop()
        controller.close()
        t2v.close()
