"""``selectModel{modelId: null}`` on the wire: back to the partner's default. And
a model the end-user's list drops no longer stays applied. And a pick
the router makes while the chat worker is creating the engine session is not lost."""

from __future__ import annotations

import threading
from typing import Any, Dict, Iterator, List, Optional

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view import ChatController, Talk2View
from talk2view.ui import AppState, GenericHost
from talk2view.ui.hub import EventHub
from talk2view.ui.router import CommandRouter, UploadRegistry
from talk2view.ui.testing import FakeController
from talk2view.ui.wire import CommandError, parse_command

from .ui_engine import AccountEngine
from .ui_helpers import wait_for


@pytest.fixture
def t2v(account_engine: AccountEngine) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=account_engine.url())
    yield client
    client.close()


def models(hub: EventHub) -> Dict[str, Any]:
    value: Dict[str, Any] = hub.snapshot()["state"]["models"]
    return value


def ids(hub: EventHub) -> list:  # type: ignore[type-arg]
    return [m["id"] for m in models(hub)["list"]]


def test_model_id_may_be_null_but_not_missing_or_empty() -> None:
    assert parse_command({"type": "selectModel", "modelId": None}).args == {"modelId": None}
    for body in ({"type": "selectModel"}, {"type": "selectModel", "modelId": ""}):
        with pytest.raises(CommandError):
            parse_command(body)


def test_the_default_forgets_the_pick_and_the_applied_model(t2v: Talk2View) -> None:
    hub, app = EventHub(), AppState(t2v, GenericHost())
    app.attach(hub)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: ids(hub) != [])
        app.select_model("m-deep")
        assert t2v._model == "m-deep"
        app.select_default_model()
        assert models(hub)["selected"] is None
        assert hub.snapshot()["state"]["preferences"] == {}
        assert t2v.preferences.get().model is None
        assert t2v._model is None and t2v._fresh_session is True
    finally:
        app.detach()


def test_the_default_leaves_a_model_the_host_chose(account_engine: AccountEngine) -> None:
    t2v = Talk2View("pk_test", base_url=account_engine.url(), model="host-model")
    hub, app = EventHub(), AppState(t2v, GenericHost())
    app.attach(hub)
    try:
        app.select_default_model()  # nothing of the end-user's was applied
        assert t2v._model == "host-model"
    finally:
        app.detach()
        t2v.close()


def test_the_router_takes_a_null_model(t2v: Talk2View) -> None:
    host = GenericHost()
    app = AppState(t2v, host)
    app.attach(EventHub())
    router = CommandRouter(FakeController(), app, host, UploadRegistry())
    bare_app = AppState(None, host)
    bare_app.attach(EventHub())
    bare = CommandRouter(FakeController(), bare_app, host, UploadRegistry())
    try:
        command = parse_command({"type": "selectModel", "modelId": None, "id": "d"})
        assert router.execute(command) == {"id": "d", "type": "selectModel", "ok": True}
        assert bare.execute(command)["error"]["type"] == "account_unavailable"
    finally:
        router.stop()
        bare.stop()
        app.detach()
        bare_app.detach()


def test_a_model_the_list_drops_is_no_longer_applied(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.preferences.update(model="m-deep")
    hub, app = EventHub(), AppState(t2v, GenericHost())
    app.attach(hub)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: models(hub)["selected"] == "m-deep")
        assert t2v._model == "m-deep"
        kept = [m for m in account_engine.models if m["id"] != "m-deep"]
        account_engine.models = kept
        app.refresh()
        assert wait_for(lambda: ids(hub) == ["m-fast"])
        assert models(hub)["selected"] is None and t2v._model is None
        assert t2v.preferences.get().model == "m-deep"  # remembered, for when it returns
        account_engine.models = kept + [{"id": "m-deep", "object": "model", "created": 2,
                                         "owned_by": "t2v"}]
        app.refresh()
        assert wait_for(lambda: models(hub)["selected"] == "m-deep")
        assert t2v._model == "m-deep"
    finally:
        app.detach()


def test_a_list_that_failed_keeps_the_applied_model(t2v: Talk2View) -> None:
    t2v.preferences.update(model="m-deep")
    hub, app = EventHub(), AppState(t2v, GenericHost())
    app.attach(hub)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: models(hub)["selected"] == "m-deep")
        passes = []
        original = t2v.list_models

        def failing() -> Any:
            passes.append(1)
            raise OSError("offline")

        t2v.list_models = failing  # type: ignore[method-assign]
        app.refresh()
        assert wait_for(lambda: passes != [] and models(hub)["list"] == [])
        assert t2v._model == "m-deep"  # unknown is not "dropped"
        t2v.list_models = original  # type: ignore[method-assign]
    finally:
        app.detach()


class HeldCreate(AccountEngine):
    """``AccountEngine`` that holds ``POST /v1/sessions`` until ``release`` is set, and
    keeps the ``model`` each message named."""

    def __init__(self, server: HTTPServer) -> None:
        super().__init__(server)
        self.creating = threading.Event()
        self.release = threading.Event()
        self.message_models: List[Optional[str]] = []

    def _handle(self, request: Request) -> Response:
        if request.path == "/v1/sessions" and request.method == "POST":
            self.creating.set()
            self.release.wait(10)
        elif request.path.endswith("/messages"):
            with self.lock:
                self.message_models.append((request.get_json(silent=True) or {}).get("model"))
        return super()._handle(request)


def test_a_pick_made_while_the_session_is_created_applies_from_the_next_turn(
    fake_engine_server: HTTPServer,
) -> None:
    engine = HeldCreate(fake_engine_server).start()
    t2v = Talk2View("pk_test", base_url=engine.url())
    controller = ChatController(t2v)
    host = GenericHost()
    hub, app = EventHub(), AppState(t2v, host)
    app.attach(hub, controller)
    router = CommandRouter(controller, app, host, UploadRegistry())
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: ids(hub) == ["m-fast", "m-deep"])
        # The turn runs on the controller's worker; the pick on this (the router's) thread.
        assert router.execute(parse_command({"type": "send", "text": "one", "id": "1"}))["ok"]
        assert engine.creating.wait(5)  # the worker's session create is in flight
        pick = parse_command({"type": "selectModel", "modelId": "m-deep", "id": "m"})
        assert router.execute(pick)["ok"] is True
        engine.release.set()
        assert wait_for(lambda: len(engine.message_models) == 1)
        assert controller.wait_idle(5)
        assert router.execute(parse_command({"type": "send", "text": "two", "id": "2"}))["ok"]
        assert wait_for(lambda: len(engine.message_models) == 2)
        assert controller.wait_idle(5)
        # The first turn had started on the partner's default; the next one has the pick.
        assert engine.message_models == [None, "m-deep"]
    finally:
        engine.release.set()
        router.stop()
        app.detach()
        controller.close()
        t2v.close()
        engine.stop()
