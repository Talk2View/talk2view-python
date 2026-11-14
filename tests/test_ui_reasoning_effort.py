"""The chat page's server and reasoning effort: the pick is a model and an effort."""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Iterator, List, Tuple

import pytest

from talk2view import Talk2View, UserPreferences
from talk2view.ui import GenericHost
from talk2view.ui.app_state import AppState
from talk2view.ui.hub import EventHub
from talk2view.ui.wire import CommandFailure

from .test_ui_app_state import count_passes, sign_out_when_picked
from .test_ui_router import Rig, error_type, ok
from .ui_engine import AccountEngine
from .ui_helpers import wait_for

G4H = {"id": "g4h", "reasoning_efforts": ["low", "medium", "high"],
       "default_reasoning_effort": "low", "gated_reasoning_efforts": ["high"]}
G4HP = {**G4H, "id": "g4h-private", "gated_reasoning_efforts": []}
PLAIN = {"id": "gpt", "reasoning_efforts": None, "default_reasoning_effort": None,
         "gated_reasoning_efforts": None}


@pytest.fixture
def t2v(account_engine: AccountEngine) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=account_engine.url())
    yield client
    client.close()


def attached(t2v: Talk2View, controller: Any = None) -> Tuple[EventHub, AppState]:
    hub = EventHub()
    app = AppState(t2v, GenericHost())
    app.attach(hub, controller)
    return hub, app


def state(hub: EventHub) -> Dict[str, Any]:
    result: Dict[str, Any] = hub.snapshot()["state"]
    return result


def offer(account_engine: AccountEngine, *entries: Dict[str, Any]) -> None:
    """The engine lists exactly ``entries``, as full ``/v1/models`` entries."""
    account_engine.models = [
        {"id": e["id"], "object": "model", "created": 1, "owned_by": "t2v", **e}
        for e in entries
    ]


def login(t2v: Talk2View, hub: EventHub) -> None:
    t2v.auth.login("ana@example.com", "pw")
    assert wait_for(lambda: state(hub)["models"]["list"] != [])


def test_the_models_slice_carries_the_effort_fields(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    offer(account_engine, G4H, PLAIN)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        listed = {m["id"]: m for m in state(hub)["models"]["list"]}
        assert listed["g4h"]["reasoning_efforts"] == ["low", "medium", "high"]
        assert listed["g4h"]["default_reasoning_effort"] == "low"
        assert listed["g4h"]["gated_reasoning_efforts"] == ["high"]
        assert listed["gpt"]["reasoning_efforts"] is None
        assert state(hub)["models"]["selectedEffort"] is None
    finally:
        app.detach()


def test_an_old_engine_list_behaves_as_before(t2v: Talk2View) -> None:
    t2v.preferences.update(model="m-deep")
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        assert wait_for(lambda: state(hub)["models"]["selected"] == "m-deep")
        none = {"reasoning_efforts": None, "default_reasoning_effort": None,
                "gated_reasoning_efforts": None}
        assert state(hub)["models"] == {
            "list": [
                {"id": "m-fast", "object": "model", "created": 1, "owned_by": "t2v",
                 "name": "Fast", **none},
                {"id": "m-deep", "object": "model", "created": 2, "owned_by": "t2v",
                 "name": None, **none},
            ],
            "selected": "m-deep",
            "selectedEffort": None,
        }
        assert (t2v.model, t2v.reasoning_effort) == ("m-deep", None)
        assert state(hub)["preferences"] == {"model": "m-deep"}
    finally:
        app.detach()


def test_a_pick_with_an_effort_is_remembered_applied_and_shown(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    offer(account_engine, G4H, PLAIN)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        app.select_model("g4h", "medium")
        assert state(hub)["models"]["selected"] == "g4h"
        assert state(hub)["models"]["selectedEffort"] == "medium"
        assert state(hub)["preferences"] == {"model": "g4h", "reasoningEffort": "medium"}
        assert (t2v.model, t2v.reasoning_effort) == ("g4h", "medium")
    finally:
        app.detach()


def test_a_pick_without_an_effort_shows_the_models_default_and_sends_none(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    offer(account_engine, G4H, PLAIN)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        app.select_model("g4h")
        assert state(hub)["models"]["selectedEffort"] == "low"
        assert state(hub)["preferences"] == {"model": "g4h"}
        assert (t2v.model, t2v.reasoning_effort) == ("g4h", None)
    finally:
        app.detach()


def test_picking_another_model_clears_the_saved_effort(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    offer(account_engine, G4H, PLAIN)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        app.select_model("g4h", "medium")
        app.select_model("gpt")
        assert state(hub)["preferences"] == {"model": "gpt"}
        assert (t2v.model, t2v.reasoning_effort) == ("gpt", None)
        assert state(hub)["models"]["selectedEffort"] is None
    finally:
        app.detach()


def test_a_gated_effort_can_be_picked_and_is_sent(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    offer(account_engine, G4H, PLAIN)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        app.select_model("g4h", "high")  # "high" is in gated_reasoning_efforts
        assert state(hub)["preferences"] == {"model": "g4h", "reasoningEffort": "high"}
        assert (t2v.model, t2v.reasoning_effort) == ("g4h", "high")
        assert state(hub)["models"]["selectedEffort"] == "high"
    finally:
        app.detach()


def test_a_stored_gated_effort_survives_a_refresh(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.preferences.update(model="g4h", reasoning_effort="high")
    offer(account_engine, G4H)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        assert wait_for(lambda: t2v.reasoning_effort == "high")
        app.refresh()
        assert wait_for(lambda: state(hub)["models"]["selectedEffort"] == "high")
        assert t2v.preferences.get() == UserPreferences(model="g4h", reasoning_effort="high")
    finally:
        app.detach()


def test_re_applying_the_stored_pick_keeps_the_session(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.preferences.update(model="g4h", reasoning_effort="medium")
    offer(account_engine, G4H)
    hub, app = EventHub(), AppState(t2v, GenericHost())
    passes = count_passes(app)
    app.attach(hub)
    try:
        login(t2v, hub)
        assert wait_for(lambda: t2v.reasoning_effort == "medium")
        version = t2v._defaults_version
        done = len(passes)
        app.refresh()
        assert wait_for(lambda: len(passes) > done)
        assert (t2v.model, t2v.reasoning_effort) == ("g4h", "medium")
        assert t2v._defaults_version == version
    finally:
        app.detach()


def test_an_effort_the_model_does_not_offer_is_refused_and_nothing_is_stored(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    offer(account_engine, G4H, PLAIN)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        with pytest.raises(CommandFailure) as unknown:
            app.select_model("g4h", "xhigh")
        assert unknown.value.type == "reasoning_effort_not_supported"
        with pytest.raises(CommandFailure):
            app.select_model("gpt", "low")  # declares no efforts
        assert state(hub)["preferences"] == {} and t2v.model is None
    finally:
        app.detach()


def test_a_stored_effort_the_model_no_longer_offers_is_forgotten(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.preferences.update(model="g4h", reasoning_effort="xhigh")
    offer(account_engine, G4H)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        assert wait_for(lambda: t2v.preferences.get().reasoning_effort is None)
        assert t2v.preferences.get().model == "g4h"
        assert (t2v.model, t2v.reasoning_effort) == ("g4h", None)
        assert wait_for(lambda: state(hub)["models"]["selectedEffort"] == "low")
    finally:
        app.detach()


def test_a_stored_blank_effort_reads_as_none(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.preferences.update(model="g4h", reasoning_effort="  ")
    offer(account_engine, G4H)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        assert wait_for(lambda: t2v.model == "g4h")
        assert t2v.reasoning_effort is None
        assert state(hub)["models"]["selectedEffort"] == "low"
    finally:
        app.detach()


def test_a_stored_legacy_id_is_shown_and_sent_as_its_base_and_effort(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.preferences.update(model="g4h-thinking")
    offer(account_engine, G4H)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        assert wait_for(lambda: state(hub)["models"]["selected"] == "g4h")
        assert state(hub)["models"]["selectedEffort"] == "medium"
        assert (t2v.model, t2v.reasoning_effort) == ("g4h", "medium")
        assert t2v.preferences.get().model == "g4h-thinking"  # storage is not rewritten
    finally:
        app.detach()


def test_a_stored_legacy_id_that_is_still_listed_is_sent_unchanged(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.preferences.update(model="g4h-thinking")
    offer(account_engine, {"id": "g4h"}, {"id": "g4h-thinking"})
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        assert wait_for(lambda: state(hub)["models"]["selected"] == "g4h-thinking")
        assert (t2v.model, t2v.reasoning_effort) == ("g4h-thinking", None)
    finally:
        app.detach()


def test_default_forgets_the_effort_and_a_legacy_pick(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.preferences.update(model="g4h-thinking")
    offer(account_engine, G4H)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        assert wait_for(lambda: (t2v.model, t2v.reasoning_effort) == ("g4h", "medium"))
        app.select_default_model()
        assert state(hub)["preferences"] == {}
        assert (t2v.model, t2v.reasoning_effort) == (None, None)
        assert state(hub)["models"]["selected"] is None
        assert state(hub)["models"]["selectedEffort"] is None
    finally:
        app.detach()


def test_default_forgets_a_stored_effort(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    offer(account_engine, G4H)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        app.select_model("g4h", "medium")
        app.select_default_model()
        assert t2v.preferences.get() == UserPreferences()
        assert (t2v.model, t2v.reasoning_effort) == (None, None)
    finally:
        app.detach()


def test_default_takes_back_a_legacy_pick_another_server_applied(t2v: Talk2View) -> None:
    t2v.set_chat_defaults(model="g4h", reasoning_effort="medium")  # an earlier server's pick
    t2v.preferences.update(model="g4h-thinking")
    hub, app = attached(t2v)  # signed out: its refresh applies nothing
    try:
        app.select_default_model()
        assert t2v.model is None and t2v.reasoning_effort is None
    finally:
        app.detach()


def test_a_refused_pick_puts_the_model_and_the_effort_back(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.preferences.update(model="g4h", reasoning_effort="medium")
    offer(account_engine, G4H, PLAIN)
    hub, app = attached(t2v)
    try:
        login(t2v, hub)
        assert wait_for(lambda: state(hub)["models"]["selected"] == "g4h")
        sign_out_when_picked(t2v, "gpt")
        with pytest.raises(LookupError):
            app.select_model("gpt")
        assert t2v.preferences.get() == UserPreferences(model="g4h", reasoning_effort="medium")
    finally:
        app.detach()


def test_the_router_takes_an_effort_and_reports_one_the_model_lacks(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    offer(account_engine, G4H, PLAIN)
    rig = Rig(t2v)
    try:
        rig.run(type="login", email="ana@example.com", password="pw")
        assert wait_for(lambda: rig.hub.snapshot()["state"]["models"]["list"] != [])
        assert ok(rig.run(type="selectModel", modelId="g4h", reasoningEffort="medium"))
        assert t2v.reasoning_effort == "medium"
        assert ok(rig.run(type="selectModel", modelId="g4h", reasoningEffort="high"))  # gated
        assert t2v.reasoning_effort == "high"
        failed = rig.run(type="selectModel", modelId="g4h", reasoningEffort="xhigh")
        assert error_type(failed) == "reasoning_effort_not_supported"
        assert failed["error"]["message"] == "That thinking level is not available for this model."
        assert ok(rig.run(type="selectModel", modelId="g4h"))  # an older page: no field
        assert (t2v.model, t2v.reasoning_effort) == ("g4h", None)
        assert ok(rig.run(type="selectModel", modelId=None, reasoningEffort="high"))
        assert (t2v.model, t2v.reasoning_effort) == (None, None)
    finally:
        rig.close()


class Notices:
    """A controller stand-in: ``on()`` keeps each callback, ``fire()`` runs them."""

    def __init__(self) -> None:
        self.callbacks: Dict[str, List[Callable[[], None]]] = {}

    def on(self, event: str, callback: Callable[[], None]) -> Callable[[], None]:
        self.callbacks.setdefault(event, []).append(callback)
        return lambda: self.callbacks[event].remove(callback)

    def fire(self, event: str) -> None:
        for callback in list(self.callbacks.get(event, [])):
            callback()


def test_a_refused_effort_is_forgotten_and_the_default_applies(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    # The 403 model_gated case: "high" is gated for the partner and this user does not pay.
    offer(account_engine, G4H)
    stub = Notices()
    hub, app = attached(t2v, stub)
    try:
        login(t2v, hub)
        app.select_model("g4h", "high")
        stub.fire("modelRefused")
        assert wait_for(lambda: t2v.preferences.get().reasoning_effort is None)
        assert t2v.preferences.get().model == "g4h"
        assert (t2v.model, t2v.reasoning_effort) == ("g4h", None)
        assert wait_for(lambda: state(hub)["models"]["selectedEffort"] == "low")
        assert state(hub)["preferences"] == {"model": "g4h"}
    finally:
        app.detach()


def test_a_refused_legacy_pick_is_stored_as_its_base(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.preferences.update(model="g4h-thinking-private")
    offer(account_engine, G4HP)
    stub = Notices()
    hub, app = attached(t2v, stub)
    try:
        login(t2v, hub)
        assert wait_for(lambda: t2v.reasoning_effort == "medium")
        stub.fire("modelRefused")
        assert wait_for(lambda: t2v.preferences.get().model == "g4h-private")
        assert t2v.preferences.get().reasoning_effort is None
        assert (t2v.model, t2v.reasoning_effort) == ("g4h-private", None)
    finally:
        app.detach()


def test_a_refusal_with_no_effort_in_force_changes_nothing(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    offer(account_engine, G4H, PLAIN)
    stub = Notices()
    hub, app = EventHub(), AppState(t2v, GenericHost())
    passes = count_passes(app)
    app.attach(hub, stub)
    try:
        login(t2v, hub)
        app.select_model("gpt")
        done = len(passes)
        stub.fire("modelRefused")
        assert wait_for(lambda: len(passes) > done)
        assert state(hub)["preferences"] == {"model": "gpt"}
        assert (t2v.model, t2v.reasoning_effort) == ("gpt", None)
        assert app._effort_refused is False
    finally:
        app.detach()


def test_a_model_at_its_default_effort_is_left_as_it_is_after_a_refusal(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    offer(account_engine, G4H)
    stub = Notices()
    hub, app = EventHub(), AppState(t2v, GenericHost())
    passes = count_passes(app)
    app.attach(hub, stub)
    try:
        login(t2v, hub)
        app.select_model("g4h")  # no effort sent: the whole model was refused
        done = len(passes)
        stub.fire("modelRefused")
        assert wait_for(lambda: len(passes) > done)
        assert state(hub)["preferences"] == {"model": "g4h"}
        assert (t2v.model, t2v.reasoning_effort) == ("g4h", None)
    finally:
        app.detach()


def test_an_auth_change_drops_a_pending_refusal(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.preferences.update(model="g4h", reasoning_effort="high")
    offer(account_engine, G4H)
    stub = Notices()
    hub, app = attached(t2v, stub)
    try:
        login(t2v, hub)
        assert wait_for(lambda: t2v.reasoning_effort == "high")
        gate = account_engine.config_gate = threading.Event()  # hold the refresh
        stub.fire("modelRefused")
        t2v.auth.login("bo@example.com", "pw")  # another identity before that pass records
        gate.set()
        assert wait_for(lambda: state(hub)["auth"]["user"]["id"] == "user-bo")
        assert wait_for(lambda: state(hub)["models"]["selected"] == "g4h")
        assert t2v.preferences.get() == UserPreferences(model="g4h", reasoning_effort="high")
        assert t2v.reasoning_effort == "high"
    finally:
        app.detach()
