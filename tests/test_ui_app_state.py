"""talk2view.ui.app_state: auth, models, config, capabilities, theme, preferences, results."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest

from talk2view import ChatController, MemoryStorage, Talk2View, User, UserPreferences
from talk2view.ui import GenericHost, Theme
from talk2view.ui.app_state import APP_KEYS, MAX_RESULTS, AppState, UnknownModelError
from talk2view.ui.hub import CHAT_KEYS, EventHub

from .ui_engine import CONFIG, AccountEngine
from .ui_helpers import PageModel, wait_for


class ClipboardHost(GenericHost):
    supports_clipboard = True


@pytest.fixture
def t2v(account_engine: AccountEngine) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=account_engine.url())
    yield client
    client.close()


def attached(t2v: Optional[Talk2View], host: Optional[GenericHost] = None) -> Any:
    hub = EventHub()
    app = AppState(t2v, host or GenericHost())
    app.attach(hub)
    return hub, app


def state(hub: EventHub) -> Dict[str, Any]:
    result: Dict[str, Any] = hub.snapshot()["state"]
    return result


SIGNED_OUT = {"user": None, "isAuthenticated": False, "isAnonymous": False,
              "oauthPending": None, "oauthError": None}


def count_passes(app: AppState) -> List[int]:
    """The generation of every refresh pass ``app`` completes (install before ``attach``)."""
    passes: List[int] = []
    run_once = app._refresh_once

    def counted(generation: int) -> None:
        run_once(generation)
        passes.append(generation)

    app._refresh_once = counted  # type: ignore[method-assign]
    return passes


def guest_starts(engine: AccountEngine) -> List[str]:
    return [line for line in engine.paths() if " /v1/auth/anonymous " in line]


def sign_out_when_picked(t2v: Talk2View, model_id: str) -> None:
    """A host listener that signs out as ``model_id`` is remembered: the list goes away
    before ``select_model`` records the pick."""

    def listener(preferences: UserPreferences) -> None:
        if preferences.model == model_id and t2v.auth.is_authenticated():
            t2v.auth.logout()

    t2v.preferences.on_change(listener)


def test_app_keys_never_collide_with_the_chat() -> None:
    assert set(APP_KEYS).isdisjoint(CHAT_KEYS)


def test_attach_seeds_every_slice_signed_out(t2v: Talk2View) -> None:
    hub, app = attached(t2v, GenericHost(theme=Theme(mode="dark")))
    try:
        assert state(hub) == {
            "theme": {"mode": "dark", "tokens": {}},
            "capabilities": {"audio": False, "oauth": False, "oauthProviders": [],
                             "clipboard": False, "skillImport": False},
            "results": [],
            "config": None,
            "models": {"list": [], "selected": None},
            "audioModels": {"list": [], "state": "loading"},
            "auth": {"user": None, "isAuthenticated": False, "isAnonymous": False,
                     "oauthPending": None, "oauthError": None},
            "preferences": {},
            "options": {
                "welcome": {"heading": None, "subheading": None, "suggestions": []},
                "features": {"dictation": True, "attachments": True, "settings": True,
                             "account": True, "threadList": True, "providerSignIn": True,
                             "skills": True},
                "allowAnonymous": True,
                "resetPasswordUrl": None,
                "footer": None,
                "appName": None,
            },
            "tools": {},
            "limits": {"demoLimitReached": False, "anonymousUnavailable": None},
        }
    finally:
        app.detach()


def test_clipboard_capability_follows_the_host() -> None:
    hub, app = attached(None, ClipboardHost())
    try:
        assert state(hub)["capabilities"]["clipboard"] is True
    finally:
        app.detach()


def test_login_fills_auth_config_and_models(t2v: Talk2View) -> None:
    hub, app = attached(t2v)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert state(hub)["auth"] == {
            "user": {"id": "user-ana", "email": "ana@example.com"},
            "isAuthenticated": True,
            "isAnonymous": False,
            "oauthPending": None,
            "oauthError": None,
        }
        assert wait_for(lambda: state(hub)["config"] is not None)
        assert state(hub)["config"] == CONFIG
        assert state(hub)["models"] == {
            "list": [
                {"id": "m-fast", "object": "model", "created": 1, "owned_by": "t2v",
                 "name": "Fast"},
                {"id": "m-deep", "object": "model", "created": 2, "owned_by": "t2v",
                 "name": None},
            ],
            "selected": None,
        }
    finally:
        app.detach()


def test_logout_clears_the_account_slices_at_once(t2v: Talk2View) -> None:
    hub, app = attached(t2v)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: state(hub)["models"]["list"] != [])
        t2v.auth.logout()
        current = state(hub)
        assert current["auth"]["isAuthenticated"] is False
        assert current["models"] == {"list": [], "selected": None}
        assert current["config"] is None
    finally:
        app.detach()


def test_a_new_identity_never_shows_the_previous_ones_models(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    hub, app = attached(t2v)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: state(hub)["config"] is not None)
        gate = account_engine.config_gate = threading.Event()  # hold bo's refresh
        t2v.auth.login("bo@example.com", "pw")  # a switch, with no sign-out in between
        current = state(hub)
        assert current["auth"]["user"] == {"id": "user-bo", "email": "bo@example.com"}
        assert current["models"] == {"list": [], "selected": None}
        assert current["config"] is None
        with pytest.raises(UnknownModelError):
            app.select_model("m-deep")  # ana's list is not bo's
        gate.set()
        assert wait_for(lambda: state(hub)["config"] is not None)
        assert [m["id"] for m in state(hub)["models"]["list"]] == ["m-fast", "m-deep"]
    finally:
        app.detach()


def test_select_model_applies_and_remembers_a_listed_model(t2v: Talk2View) -> None:
    hub, app = attached(t2v)
    try:
        with pytest.raises(UnknownModelError):
            app.select_model("m-fast")  # nothing is listed while signed out
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: state(hub)["models"]["list"] != [])
        with pytest.raises(UnknownModelError):
            app.select_model("m-secret")
        app.select_model("m-deep")
        assert state(hub)["models"]["selected"] == "m-deep"
        assert state(hub)["preferences"] == {"model": "m-deep"}
        assert t2v._model == "m-deep"
    finally:
        app.detach()


def test_a_pick_an_auth_change_overtook_is_refused(t2v: Talk2View) -> None:
    hub, app = attached(t2v)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: state(hub)["models"]["list"] != [])
        sign_out_when_picked(t2v, "m-deep")
        with pytest.raises(UnknownModelError):
            app.select_model("m-deep")
        assert state(hub)["models"] == {"list": [], "selected": None}
        assert t2v._model is None
    finally:
        app.detach()


def test_a_refused_pick_is_not_remembered(t2v: Talk2View) -> None:
    t2v.preferences.update(model="m-fast")
    hub, app = attached(t2v)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: state(hub)["models"]["selected"] == "m-fast")
        sign_out_when_picked(t2v, "m-deep")
        with pytest.raises(UnknownModelError):
            app.select_model("m-deep")
        assert t2v.preferences.get().model == "m-fast"  # as it was before the refused pick
        assert state(hub)["preferences"] == {"model": "m-fast"}
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: state(hub)["models"]["selected"] is not None)
        assert state(hub)["models"]["selected"] == "m-fast"  # never the refused one, later
        assert t2v._model == "m-fast"
    finally:
        app.detach()


def test_a_stored_choice_is_restored_only_when_listed(t2v: Talk2View) -> None:
    t2v.preferences.update(model="m-deep")
    hub, app = attached(t2v)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: state(hub)["models"]["selected"] == "m-deep")
        assert t2v._model == "m-deep"
    finally:
        app.detach()


def test_an_unlisted_stored_choice_is_ignored(t2v: Talk2View) -> None:
    t2v.preferences.update(model="m-gone")
    hub, app = attached(t2v)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: state(hub)["models"]["list"] != [])
        assert state(hub)["models"]["selected"] is None
        assert t2v._model is None
    finally:
        app.detach()


def test_preferences_follow_every_change(t2v: Talk2View) -> None:
    hub, app = attached(t2v)
    try:
        t2v.preferences.update(font_size="large", stt_language="en")
        assert state(hub)["preferences"] == {"sttLanguage": "en", "fontSize": "large"}
        t2v.preferences.update(font_size=None)
        assert state(hub)["preferences"] == {"sttLanguage": "en"}
    finally:
        app.detach()


def test_a_change_just_before_attach_subscribes_is_not_missed(t2v: Talk2View) -> None:
    subscribe_auth = t2v.auth.on_auth_state_change
    subscribe_preferences = t2v.preferences.on_change

    def auth_late(callback: Callable[..., None]) -> Callable[[], None]:
        t2v.auth.login("ana@example.com", "pw")  # another thread, a moment before
        return subscribe_auth(callback)

    def preferences_late(callback: Callable[..., None]) -> Callable[[], None]:
        t2v.preferences.update(font_size="large")
        return subscribe_preferences(callback)

    t2v.auth.on_auth_state_change = auth_late  # type: ignore[method-assign]
    t2v.preferences.on_change = preferences_late  # type: ignore[method-assign]
    hub, app = attached(t2v)
    try:
        assert state(hub)["auth"]["user"] == {"id": "user-ana", "email": "ana@example.com"}
        assert state(hub)["preferences"] == {"fontSize": "large"}
    finally:
        app.detach()


def test_theme_and_results(t2v: Talk2View) -> None:
    hub, app = attached(t2v)
    try:
        app.set_theme(Theme(mode="dark", tokens={"--t2v-bg": "#021C25"}))
        assert state(hub)["theme"] == {"mode": "dark", "tokens": {"--t2v-bg": "#021C25"}}
        for i in range(MAX_RESULTS + 5):
            app.record_result({"id": str(i), "type": "cancel", "ok": True})
        results = state(hub)["results"]
        assert len(results) == MAX_RESULTS
        assert results[0]["id"] == "5" and results[-1]["id"] == str(MAX_RESULTS + 4)
    finally:
        app.detach()


def test_a_result_json_cannot_carry_is_refused_and_not_kept(t2v: Talk2View) -> None:
    hub, app = attached(t2v)
    try:
        with pytest.raises(TypeError):
            app.record_result({"id": "a", "type": "copy", "ok": True, "data": object()})
        with pytest.raises(ValueError):
            app.record_result({"id": "b", "type": "copy", "ok": True, "data": float("nan")})
        kept = {"id": "c", "type": "cancel", "ok": True}
        app.record_result(kept)
        kept["ok"] = False  # the caller's dict, changed after it was recorded
        app.record_result({"id": "d", "type": "cancel", "ok": True})
        assert state(hub)["results"] == [
            {"id": "c", "type": "cancel", "ok": True},
            {"id": "d", "type": "cancel", "ok": True},
        ]
    finally:
        app.detach()


class RecordingHub(EventHub):
    """Records the chat messages its mirror held at each ``forget_replay()``."""

    def __init__(self) -> None:
        super().__init__()
        self.forgotten: List[Any] = []

    def forget_replay(self) -> None:
        self.forgotten.append(self.snapshot()["state"].get("messages"))
        super().forget_replay()


def first_kind(hub: EventHub, last_event_id: int) -> str:
    client = hub.attach(last_event_id=last_event_id)
    event = hub.next_event(client, 0.0)
    hub.detach(client)
    assert event is not None
    return event[0]


def test_a_new_account_on_show_forgets_the_replay_history_and_the_results(
    t2v: Talk2View,
) -> None:
    hub = RecordingHub()
    app = AppState(t2v, GenericHost())
    app.attach(hub)
    try:
        t2v.auth.login("ana@example.com", "pw")  # from signed out: another account on show
        assert len(hub.forgotten) == 1
        app.record_result({"id": "ana-1", "type": "cancel", "ok": True})
        t2v.auth.login("ana@example.com", "pw")  # the same account again: nothing forgotten
        assert len(hub.forgotten) == 1
        assert [r["id"] for r in state(hub)["results"]] == ["ana-1"]
        t2v.auth.login("bo@example.com", "pw")  # a switch, with no sign-out between
        assert len(hub.forgotten) == 2
        assert state(hub)["results"] == []
        assert first_kind(hub, 0) == "snapshot"
        app.record_result({"id": "bo-1", "type": "login", "ok": True})  # the switch's own
        assert [r["id"] for r in state(hub)["results"]] == ["bo-1"]
        t2v.auth.logout()
        assert len(hub.forgotten) == 3
        assert state(hub)["results"] == []
        t2v.auth.logout()  # signed out already: nothing on show changed
        assert len(hub.forgotten) == 3
    finally:
        app.detach()


def test_the_replay_history_is_forgotten_after_the_controller_drops_the_conversation(
    t2v: Talk2View,
) -> None:
    """The controller's auth listener is an early one, AppState's an ordinary one: by the
    time the hub forgets, the batch that dropped the conversation is in its mirror."""
    controller = ChatController(t2v)
    hub = RecordingHub()
    unsubscribe = hub.join(controller)
    app = AppState(t2v, GenericHost())
    app.attach(hub)
    try:
        t2v.auth.login("ana@example.com", "pw")
        controller.send("ANA-PRIVATE-NOTE")
        assert controller.wait_idle(10)
        assert "ANA-PRIVATE-NOTE" in json.dumps(state(hub)["messages"])
        for change in ("switch", "sign-out"):
            before = len(hub.forgotten)
            if change == "switch":
                t2v.auth.login("bo@example.com", "pw")
            else:
                t2v.auth.logout()
            assert len(hub.forgotten) == before + 1, change
            assert hub.forgotten[-1] == [], change
            t2v.auth.login("ana@example.com", "pw")
            controller.send("ANA-PRIVATE-NOTE")
            assert controller.wait_idle(10)
    finally:
        app.detach()
        unsubscribe()
        controller.close()


def test_a_refresh_an_auth_change_overtook_is_dropped(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    hub = EventHub()
    app = AppState(t2v, GenericHost())
    passes: List[int] = []
    run_once = app._refresh_once

    def counted(generation: int) -> None:
        run_once(generation)
        passes.append(generation)

    app._refresh_once = counted  # type: ignore[method-assign]
    app.attach(hub)
    gate = account_engine.config_gate = threading.Event()
    try:
        assert wait_for(lambda: len(passes) == 1)  # the signed-out pass at attach
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: account_engine.config_requests == 1)  # held at /v1/config
        t2v.auth.logout()
        page = PageModel()
        client = hub.attach()  # from here on, every models value a page sees
        page.pump(hub, client)
        seen: List[Any] = []
        gate.set()  # the stale pass now gets ana's config back
        assert wait_for(lambda: len(passes) == 3)  # the stale pass, then the signed-out one
        while True:
            event = hub.next_event(client, 0.01)
            if event is None or event[0] == "hb":
                break
            ops = json.loads(event[2])["ops"]
            seen += [op for op in ops if op["path"][0] in ("models", "config")]
            page.feed(*event)
        leaked = [op for op in seen if op["value"] not in (None, {"list": [], "selected": None})]
        assert leaked == [], leaked
        assert state(hub)["models"] == {"list": [], "selected": None}
        assert state(hub)["config"] is None
    finally:
        app.detach()


def test_a_refresh_never_starts_a_guest_session(account_engine: AccountEngine) -> None:
    stale = MemoryStorage()  # a stored session whose refresh token the engine rejects
    stale.set("access_token", "tok-stale")
    stale.set("refresh_token", "ref-stale")
    stale.set("user", json.dumps({"id": "user-stale", "email": "old@example.com"}))
    client = Talk2View(
        "pk_test", base_url=account_engine.url(), storage=stale, anonymous_auto_start=True
    )
    hub = EventHub()
    app = AppState(client, GenericHost())
    passes = count_passes(app)
    app.attach(hub)
    try:
        # The attach pass signs out on the refresh thread (in /v1/config), then the
        # signed-out pass: neither may go on to /v1/models, which would start a guest.
        assert wait_for(lambda: len(passes) >= 2)
        app.detach()
        assert any(" /v1/auth/refresh " in line for line in account_engine.paths())
        assert guest_starts(account_engine) == []
        assert not client.auth.is_authenticated()
        assert state(hub)["auth"] == SIGNED_OUT
    finally:
        app.detach()
        client.close()


def test_a_refresh_a_sign_out_overtook_never_starts_a_guest_session(
    account_engine: AccountEngine,
) -> None:
    client = Talk2View("pk_test", base_url=account_engine.url(), anonymous_auto_start=True)
    hub = EventHub()
    app = AppState(client, GenericHost())
    passes = count_passes(app)
    app.attach(hub)
    gate = account_engine.config_gate = threading.Event()
    try:
        assert wait_for(lambda: len(passes) == 1)  # the signed-out pass at attach
        client.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: account_engine.config_requests == 1)  # held at /v1/config
        client.auth.logout()
        gate.set()  # the overtaken pass gets its config back, with nobody signed in
        assert wait_for(lambda: len(passes) == 3)
        assert guest_starts(account_engine) == []
        assert not client.auth.is_authenticated()
    finally:
        app.detach()
        client.close()


def test_a_refresh_for_a_sign_in_not_yet_delivered_is_dropped(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    reached, release = threading.Event(), threading.Event()

    def slow_host_listener(user: Optional[User]) -> None:
        if user is not None and user.id == "user-bo":
            reached.set()
            release.wait(5)

    t2v.auth.on_auth_state_change(slow_host_listener)  # runs ahead of AppState's
    hub = EventHub()
    app = AppState(t2v, GenericHost())
    passes = count_passes(app)
    app.attach(hub)
    switch = threading.Thread(target=t2v.auth.login, args=("bo@example.com", "pw"), daemon=True)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: len(passes) == 2 and state(hub)["config"] is not None)
        account_engine.models = [{"id": "m-bo", "object": "model", "created": 3, "owned_by": "t2v"}]
        switch.start()
        assert reached.wait(5)  # bo's tokens are stored; AppState has not been told yet
        app.refresh()
        assert wait_for(lambda: len(passes) == 3)
        current = state(hub)
        assert current["auth"]["user"]["id"] == "user-ana"
        assert [m["id"] for m in current["models"]["list"]] == ["m-fast", "m-deep"]  # not bo's
        release.set()
        switch.join(5)
        assert wait_for(lambda: [m["id"] for m in state(hub)["models"]["list"]] == ["m-bo"])
        assert state(hub)["auth"]["user"]["id"] == "user-bo"
    finally:
        release.set()
        app.detach()


def test_a_pick_while_another_account_is_signed_in_but_not_yet_shown_is_refused(
    t2v: Talk2View,
) -> None:
    """Bo's tokens are stored, and a host listener ahead of AppState's still runs: the
    page shows ana, and her list, but a pick from it would land in bo's session."""
    reached, release = threading.Event(), threading.Event()

    def slow_host_listener(user: Optional[User]) -> None:
        if user is not None and user.id == "user-bo":
            reached.set()
            release.wait(5)

    t2v.auth.on_auth_state_change(slow_host_listener)  # runs ahead of AppState's
    hub, app = attached(t2v)
    switch = threading.Thread(target=t2v.auth.login, args=("bo@example.com", "pw"), daemon=True)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: state(hub)["models"]["list"] != [])
        switch.start()
        assert reached.wait(5)
        assert state(hub)["auth"]["user"]["id"] == "user-ana"  # not told of bo yet
        with pytest.raises(UnknownModelError):
            app.select_model("m-deep")  # listed for ana, who is no longer signed in
        assert t2v.preferences.get().model is None  # nothing remembered
        assert t2v._model is None  # nothing applied
        release.set()
        switch.join(5)
        assert wait_for(lambda: state(hub)["models"]["list"] != [])
        app.select_model("m-deep")  # bo's own list, shown: taken
        assert t2v._model == "m-deep"
    finally:
        release.set()
        app.detach()


class UnreadableStorage(MemoryStorage):
    def get(self, key: str) -> Optional[str]:
        raise OSError("sharing violation")


class CorruptibleStorage(MemoryStorage):
    """Once ``corrupt`` is set, the stored user reads back with a NaN id, as from a
    hand-edited store (Python's json reads NaN; the page's JSON.parse would not)."""

    corrupt = False

    def get(self, key: str) -> Optional[str]:
        if key == "user" and self.corrupt:
            return '{"id": NaN, "email": "bo@example.com"}'
        return super().get(key)


def test_a_stored_user_json_cannot_carry_reads_as_signed_out(
    account_engine: AccountEngine,
) -> None:
    storage = CorruptibleStorage()
    client = Talk2View("pk_test", base_url=account_engine.url(), storage=storage)
    hub = EventHub()
    app = AppState(client, GenericHost())
    passes = count_passes(app)
    app.attach(hub)
    try:
        client.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: len(passes) == 2 and state(hub)["config"] is not None)
        storage.corrupt = True
        client.auth.login("bo@example.com", "pw")
        current = state(hub)
        assert current["auth"] == SIGNED_OUT
        assert current["models"] == {"list": [], "selected": None}  # not ana's
        assert current["config"] is None
        assert wait_for(lambda: len(passes) == 3)  # bo's refresh: nothing shown for a
        assert state(hub)["models"] == {"list": [], "selected": None}  # user it cannot show
        assert state(hub)["config"] is None
    finally:
        app.detach()
        client.close()


class BrokenHub(EventHub):
    def set_slice(self, key: str, value: Any) -> None:
        raise RuntimeError("the hub is gone")


def test_an_attach_that_fails_leaves_no_listener_behind(
    t2v: Talk2View, caplog: pytest.LogCaptureFixture
) -> None:
    app = AppState(t2v, GenericHost())
    with pytest.raises(RuntimeError):
        app.attach(BrokenHub())
    with caplog.at_level(logging.WARNING):
        t2v.auth.login("ana@example.com", "pw")
        t2v.preferences.update(font_size="small")
    assert "listener raised" not in caplog.text
    app.detach()


def test_an_unreadable_token_store_reads_as_signed_out(account_engine: AccountEngine) -> None:
    client = Talk2View("pk_test", base_url=account_engine.url(), storage=UnreadableStorage())
    hub, app = attached(client)
    try:
        assert state(hub)["auth"] == {
            "user": None,
            "isAuthenticated": False,
            "isAnonymous": False,
            "oauthPending": None,
            "oauthError": None,
        }
    finally:
        app.detach()
        client.close()


def test_user_metadata_json_cannot_carry_is_left_out(account_engine: AccountEngine) -> None:
    storage = MemoryStorage()
    storage.set("access_token", "tok-x")
    storage.set("refresh_token", "ref-x")
    # Python's json reads NaN, which JSON.parse on the page refuses.
    storage.set("user", '{"id": "user-x", "email": "x@example.com", "user_metadata": {"n": NaN}}')
    with account_engine.lock:
        account_engine.tokens["tok-x"] = "user-x"  # the refresh at attach stays signed in
    client = Talk2View("pk_test", base_url=account_engine.url(), storage=storage)
    hub, app = attached(client)
    try:
        assert state(hub)["auth"] == {
            "user": {"id": "user-x", "email": "x@example.com"},
            "isAuthenticated": True,
            "isAnonymous": False,
            "oauthPending": None,
            "oauthError": None,
        }
    finally:
        app.detach()
        client.close()


def followed_by(t2v: Talk2View, app: AppState) -> List[str]:
    """Which of ``t2v``'s listener lists hold one of ``app``'s listeners."""
    lists: Dict[str, List[Any]] = {
        "auth": t2v.auth._listeners,
        "preferences": t2v.preferences._listeners,
        "anonymous": t2v._anonymous_unavailable_listeners,
    }
    mine = (app._on_auth, app._on_preferences, app.note_anonymous_unavailable)
    return [name for name, listeners in lists.items() if any(f in mine for f in listeners)]


def test_an_app_state_is_single_use(t2v: Talk2View) -> None:
    """A second UIServer handed a stopped one's AppState used to start with a refresh
    thread that returned at once: models and config never filled, every pick refused."""
    hub, app = attached(t2v)
    app.detach()
    with pytest.raises(RuntimeError, match="single use"):
        app.attach(EventHub())
    assert followed_by(t2v, app) == []
    assert app._thread is None


def test_a_detach_racing_attach_leaves_no_listener_behind(t2v: Talk2View) -> None:
    """``stop()`` on another thread lands after attach subscribed and before it recorded
    the subscriptions: attach unsubscribes them itself, and refuses."""
    app = AppState(t2v, GenericHost())
    subscribe = t2v.preferences.on_change

    def detached_meanwhile(callback: Callable[..., None]) -> Callable[[], None]:
        unsubscribe = subscribe(callback)
        app.detach()  # no lock of the app is held here: it could be any thread's
        return unsubscribe

    t2v.preferences.on_change = detached_meanwhile  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="single use"):
        app.attach(EventHub())
    assert followed_by(t2v, app) == []
    assert app._thread is None and app._hub is None


class RefusingNotices:
    """A controller whose ``on()`` raises."""

    def on(self, event: str, callback: Callable[[], None]) -> Callable[[], None]:
        raise RuntimeError("no notices here")


def test_an_attach_whose_controller_refuses_the_notice_leaves_no_listener_behind(
    t2v: Talk2View,
) -> None:
    app = AppState(t2v, GenericHost())
    with pytest.raises(RuntimeError, match="no notices here"):
        app.attach(EventHub(), RefusingNotices())
    assert followed_by(t2v, app) == []
    assert app._thread is None and app._hub is None
    app.detach()


def test_detach_unsubscribes_what_attach_recorded(t2v: Talk2View) -> None:
    hub, app = attached(t2v)
    assert followed_by(t2v, app) == ["auth", "preferences", "anonymous"]
    app.detach()
    assert followed_by(t2v, app) == []


def test_without_a_talk2view_every_slice_is_signed_out() -> None:
    hub, app = attached(None)
    try:
        assert state(hub)["auth"]["isAuthenticated"] is False
        assert state(hub)["preferences"] == {}
        with pytest.raises(UnknownModelError):
            app.select_model("m-fast")
    finally:
        app.detach()


def test_detach_stops_following(t2v: Talk2View) -> None:
    hub, app = attached(t2v)
    app.detach()
    before = hub.seq
    t2v.auth.login("ana@example.com", "pw")
    t2v.preferences.update(font_size="small")
    assert hub.seq == before


def test_the_controller_exposes_its_talk2view(t2v: Talk2View) -> None:
    controller = ChatController(t2v)
    try:
        assert controller.t2v is t2v
    finally:
        controller.close()


def test_capabilities_audio_follows_the_host_and_audio_models_load_signed_in(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    hub = EventHub()
    app = AppState(t2v, GenericHost(), audio=True)
    app.attach(hub)
    try:
        assert state(hub)["capabilities"]["audio"] is True
        # "loading" before anyone is signed in: not "ready" with nothing yet.
        assert state(hub)["audioModels"] == {"list": [], "state": "loading"}
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: state(hub)["audioModels"]["list"] != [])
        assert state(hub)["audioModels"]["list"] == [
            {"id": "scribe_v2", "name": "Scribe v2", "supported_languages": ["en", "vi"],
             "description": "Fast"},
            {"id": "faster-whisper-base", "name": None, "supported_languages": None,
             "description": None},
        ]
        assert state(hub)["audioModels"]["state"] == "ready"
        app.select_stt_model("scribe_v2")
        app.select_stt_language("vi")
        assert wait_for(lambda: state(hub)["preferences"].get("sttLanguage") == "vi")
        assert state(hub)["preferences"]["sttModel"] == "scribe_v2"
        app.select_stt_model(None)
        app.select_stt_language(None)
        assert wait_for(
            lambda: "sttModel" not in state(hub)["preferences"]
            and "sttLanguage" not in state(hub)["preferences"]
        )
        t2v.auth.logout()
        # Signed out again: emptied AND put back to "loading", not left "ready" with an
        # empty list — a partner's real empty catalogue and "nobody has asked yet" must
        # never look the same.
        assert wait_for(lambda: state(hub)["audioModels"] == {"list": [], "state": "loading"})
    finally:
        app.detach()


def test_audio_models_state_is_ready_even_when_the_list_comes_back_empty(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    """A partner with no voice models configured at all: "ready", never stuck "loading"."""
    account_engine.audio_models = []
    hub = EventHub()
    app = AppState(t2v, GenericHost(), audio=True)
    app.attach(hub)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: state(hub)["audioModels"]["state"] == "ready")
        assert state(hub)["audioModels"]["list"] == []
    finally:
        app.detach()


def test_audio_models_state_is_error_when_the_list_could_not_be_read(
    t2v: Talk2View, account_engine: AccountEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed read is not "loading" either — the page needs to say so."""

    def _boom() -> List[Any]:
        raise RuntimeError("the voice catalogue is unreachable")

    monkeypatch.setattr(t2v.voice, "list_models", _boom)
    hub = EventHub()
    app = AppState(t2v, GenericHost(), audio=True)
    app.attach(hub)
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: state(hub)["audioModels"]["state"] == "error")
        assert state(hub)["audioModels"]["list"] == []
    finally:
        app.detach()


def test_capabilities_audio_is_false_without_capture(t2v: Talk2View) -> None:
    hub, app = attached(t2v)
    try:
        assert state(hub)["capabilities"]["audio"] is False
    finally:
        app.detach()
