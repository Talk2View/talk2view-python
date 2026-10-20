"""ChatController across end-user identities.

Approval consent ("always allow") belongs to one end-user and is reset on every change of
user id. The conversation is dropped locally on a sign-out and on a switch to a different
account (anonymous to another anonymous, account to anonymous, account to another account);
it is kept when someone signs in from signed-out and when an anonymous demo becomes an
account. A real ``Talk2View`` against a stateful fake engine.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, List, Optional

from talk2view import MemoryStorage, Talk2View, tool
from talk2view.controller import ChatController
from talk2view.types import Decision

from .controller_fakes import BatchLog, inline_executor
from .fake_engine import TOOL, FakeEngine


class Host:
    """A real Talk2View + ChatController, with the ``delete_volume`` client tool registered
    behind ``permission=True``: every call asks unless the end-user said "always"."""

    def __init__(
        self,
        engine: FakeEngine,
        *,
        executor: Optional[Callable[[Callable[[], None]], None]] = inline_executor,
        before_controller: Optional[Callable[[Talk2View], None]] = None,
        **kwargs: Any,
    ) -> None:
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

        self.t2v = Talk2View("pk_test", base_url=engine.url(), **kwargs)
        self.t2v.tools.register_from_functions([delete_volume])
        if before_controller is not None:
            before_controller(self.t2v)
        self.controller = ChatController(self.t2v, executor=executor)
        self.log = BatchLog()
        self.controller.subscribe(self.log)

    def turn(self, text: str) -> None:
        self.controller.send(text)
        assert self.controller.wait_idle(5.0)

    def approve_always(self) -> None:
        self.controller.approve(Decision("always"))
        assert self.controller.wait_idle(5.0)

    def snap(self) -> Any:
        return self.controller.snapshot()

    def user_texts(self) -> List[str]:
        return [m["content"] for m in self.snap()["messages"] if m["role"] == "user"]

    def close(self) -> None:
        self.controller.close()
        self.t2v.close()


def _host(engine: FakeEngine, **kwargs: Any) -> Host:
    return Host(engine, **kwargs)


def _assert_cleared(snapshot: Any) -> None:
    assert snapshot["messages"] == []
    assert snapshot["pendingApproval"] is None
    assert snapshot["threadId"] is None
    assert snapshot["alwaysAllowedTools"] == []
    assert snapshot["isLoading"] is False
    assert snapshot["error"] is None


# ── Consent ─────────────────────────────────────────────────────────────────


def test_an_always_allow_does_not_carry_over_to_the_next_account(engine: FakeEngine) -> None:
    """probe_always_allow: A answers "always" and signs out; B signs in on the same
    controller and triggers the same tool. B must be asked, and nothing may run."""
    h = _host(engine)
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("delete CT")
        assert h.snap()["pendingApproval"] is not None
        h.approve_always()
        assert h.ran == ["CT"]
        assert h.snap()["alwaysAllowedTools"] == [TOOL]

        h.t2v.auth.logout()
        h.t2v.auth.login("b@x.y", "pw")
        h.turn("delete CT")

        snapshot = h.snap()
        assert snapshot["pendingApproval"] is not None  # B is asked
        assert snapshot["pendingApproval"]["toolName"] == TOOL
        assert h.ran == ["CT"]  # and B's call has not run
        assert snapshot["alwaysAllowedTools"] == []
    finally:
        h.close()


def test_an_event_for_the_same_user_keeps_consent_and_conversation(engine: FakeEngine) -> None:
    h = _host(engine)
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("delete CT")
        h.approve_always()
        h.t2v.auth.login("a@x.y", "pw")  # the same account again: same user id
        snapshot = h.snap()
        assert snapshot["alwaysAllowedTools"] == [TOOL]
        assert h.user_texts() == ["delete CT"]
    finally:
        h.close()


# ── Conversation: cleared ───────────────────────────────────────────────────


def test_sign_out_drops_the_conversation_locally_without_an_engine_delete(
    engine: FakeEngine,
) -> None:
    h = _host(engine)
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("hello")
        h.turn("delete CT")
        h.approve_always()
        before = engine.paths()

        h.t2v.auth.logout()

        _assert_cleared(h.snap())
        assert engine.paths()[len(before):] == ["POST /v1/auth/logout user-a"]  # no DELETE

        # Signed in again, the old turns are not replayed into the engine.
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("again")
        assert engine.histories_of("user-a")[-1] == [("user", "again")]
    finally:
        h.close()


def test_sign_out_drops_a_pending_approval(engine: FakeEngine) -> None:
    h = _host(engine)
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("delete CT")
        assert h.snap()["pendingApproval"] is not None
        h.t2v.auth.logout()
        _assert_cleared(h.snap())
        h.controller.approve(Decision("once"))  # a stale click on the old card: a no-op
        assert h.controller.wait_idle(5.0)
        assert h.ran == []
    finally:
        h.close()


def test_sign_out_abandons_the_run_in_flight(engine: FakeEngine) -> None:
    h = _host(engine, executor=None)  # the controller's own worker thread
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.controller.send("slow reply please")
        assert engine.slow_started.wait(5.0)
        assert h.snap()["isLoading"] is True

        h.t2v.auth.logout()

        _assert_cleared(h.snap())
        assert h.controller.wait_idle(5.0)
        engine.release_slow.set()
        h.controller.send("next")  # usable at once: the old run is gone
        assert h.controller.wait_idle(5.0)
        # Signed out, the new turn fails; the abandoned run wrote nothing meanwhile.
        assert h.user_texts() == ["next"]
    finally:
        h.close()


def test_a_switch_to_another_account_clears(engine: FakeEngine) -> None:
    h = _host(engine)
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("hello from a")
        h.t2v.auth.login("b@x.y", "pw")  # no logout in between
        _assert_cleared(h.snap())
        h.turn("hello from b")
        assert engine.histories_of("user-b") == [[("user", "hello from b")]]
    finally:
        h.close()


def test_a_switch_from_an_account_to_anonymous_clears(engine: FakeEngine) -> None:
    h = _host(engine)
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("hello from a")
        h.t2v.auth.start_anonymous()
        snapshot = h.snap()
        _assert_cleared(snapshot)
        assert snapshot["isAnonymous"] is True
    finally:
        h.close()


def test_a_switch_to_another_anonymous_session_clears(engine: FakeEngine) -> None:
    h = _host(engine)
    try:
        h.t2v.auth.start_anonymous()
        h.turn("hello from anon-1")
        h.t2v.auth.start_anonymous()
        _assert_cleared(h.snap())
        h.turn("hello from anon-2")
        assert engine.histories_of("anon-2") == [[("user", "hello from anon-2")]]
    finally:
        h.close()


# ── Conversation: kept ──────────────────────────────────────────────────────


def test_anonymous_to_account_keeps_the_conversation_but_resets_consent(
    engine: FakeEngine,
) -> None:
    """The demo -> signup funnel: signup() from an anonymous session converts, and a 409
    signs into the existing account instead (a different user id)."""
    h = _host(engine)
    try:
        h.t2v.auth.start_anonymous()
        h.turn("delete CT")
        h.approve_always()
        assert h.snap()["alwaysAllowedTools"] == [TOOL]

        outcome = h.t2v.auth.signup("b@x.y", "pw")  # convert 409 -> login as user-b
        assert outcome.user is not None and outcome.user.id == "user-b"

        snapshot = h.snap()
        assert h.user_texts() == ["delete CT"]
        assert snapshot["alwaysAllowedTools"] == []
        assert snapshot["isAnonymous"] is False

        h.turn("delete CT again")
        assert h.snap()["pendingApproval"] is not None  # the account is asked afresh
        assert h.ran == ["CT"]
        # The kept conversation is what the account's session is sent.
        assert engine.histories_of("user-b")[0][0] == ("user", "delete CT")
    finally:
        h.close()


def test_a_sign_in_from_signed_out_keeps_the_conversation_but_resets_consent(
    engine: FakeEngine,
) -> None:
    """None -> X. A store holding a token but no user (a host that wrote the token itself)
    reads as signed out to the controller, yet can chat."""
    engine.tokens["tok-raw"] = "user-a"

    def seed(t2v: Talk2View) -> None:
        t2v.storage.set("access_token", "tok-raw")

    h = _host(engine, before_controller=seed)
    try:
        h.turn("delete CT")
        h.approve_always()
        assert h.snap()["alwaysAllowedTools"] == [TOOL]

        h.t2v.auth.login("a@x.y", "pw")

        assert h.user_texts() == ["delete CT"]
        assert h.snap()["alwaysAllowedTools"] == []
    finally:
        h.close()


def test_the_anonymous_start_inside_a_send_keeps_that_send(engine: FakeEngine) -> None:
    """None -> anonymous, fired on the controller's own worker by the send it is serving
    (chat() -> the anonymous auto-start). That send is neither abandoned nor emptied."""
    h = _host(engine, executor=None, anonymous_auto_start=True)
    try:
        h.turn("hello")
        snapshot = h.snap()
        assert snapshot["error"] is None
        assert [(m["role"], m["content"]) for m in snapshot["messages"]] == [
            ("user", "hello"),
            ("assistant", "ok"),
        ]
        assert snapshot["isAnonymous"] is True
        assert True in h.log.values("isAnonymous")  # set by the auth event, mid-run
        assert engine.histories_of("anon-1") == [[("user", "hello")]]
    finally:
        h.close()


# ── Listener order ──────────────────────────────────────────────────────────
# The controller hears every auth change before any host listener, so a host listener can
# neither delay its reset nor change the auth state before it has looked.


def test_a_blocking_host_listener_cannot_delay_the_consent_reset(engine: FakeEngine) -> None:
    """A host listener registered before the controller blocks
    inside B's login (a blocking marshal, say) while the UI thread sends. B's tool call must
    not run on A's "always"."""
    entered = threading.Event()
    release = threading.Event()

    def blocking_listener(t2v: Talk2View) -> None:
        def listener(user: Any) -> None:
            if user is not None and user.id == "user-b":
                entered.set()
                release.wait(5.0)

        t2v.auth.on_auth_state_change(listener)

    h = _host(engine, executor=None, before_controller=blocking_listener)
    login = threading.Thread(target=lambda: h.t2v.auth.login("b@x.y", "pw"), daemon=True)
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("delete CT")
        h.approve_always()
        login.start()
        assert entered.wait(5.0)  # B is stored; the host's listener is still running
        h.turn("delete CT")  # the UI thread, meanwhile
        snapshot = h.snap()
        assert snapshot["pendingApproval"] is not None  # B is asked
        assert h.ran == ["CT"]  # and B's call did not run on A's consent
        assert h.user_texts() == ["delete CT"]  # A's turn is not B's
    finally:
        release.set()
        login.join(5.0)
        h.close()


def test_an_explicit_sign_out_clears_even_when_a_host_listener_signs_straight_in(
    engine: FakeEngine,
) -> None:
    """rr_probe_l3anon: a host listener that signs B in from inside the logout's delivery.
    Heard after it, the controller would read anon-1 -> user-b (the demo becoming an
    account) and carry the demo's conversation into B's session."""

    def relogin(t2v: Talk2View) -> None:
        def listener(user: Any) -> None:
            if user is None:
                t2v.auth.login("b@x.y", "pw")

        t2v.auth.on_auth_state_change(listener)

    h = _host(engine, before_controller=relogin)
    try:
        h.t2v.auth.start_anonymous()
        h.turn("my private demo question")
        h.t2v.auth.logout()  # an explicit sign-out; the listener then signs B in
        assert h.t2v.auth.get_user() is not None
        assert h.t2v.auth.get_user().id == "user-b"  # type: ignore[union-attr]
        assert h.user_texts() == []
        h.turn("hello from b")
        assert engine.histories_of("user-b") == [[("user", "hello from b")]]
    finally:
        h.close()


# ── Plumbing ────────────────────────────────────────────────────────────────


def test_is_anonymous_follows_auth_events_without_a_send(engine: FakeEngine) -> None:
    h = _host(engine)
    try:
        h.t2v.auth.start_anonymous()
        assert h.snap()["isAnonymous"] is True
        h.t2v.auth.login("b@x.y", "pw")
        assert h.snap()["isAnonymous"] is False
        assert h.log.values("isAnonymous") == [True, False]
    finally:
        h.close()


def test_a_sign_in_between_seeding_and_subscribing_is_not_lost(engine: FakeEngine) -> None:
    """The controller reads the user at construction and subscribes afterwards. A login in
    between reaches no listener of its own; without a resync it would take user-a for
    "signed out", and a later switch to user-b for a sign-in that keeps the conversation."""

    def login_during_subscribe(t2v: Talk2View) -> None:
        # Whichever subscription call the controller makes.
        for name in ("on_auth_state_change", "_add_listener"):
            subscribe = getattr(t2v.auth, name, None)
            if subscribe is None:
                continue

            def late(callback: Any, *args: Any, _subscribe: Any = subscribe, **kwargs: Any) -> Any:
                if t2v.auth.get_user() is None:
                    t2v.auth.login("a@x.y", "pw")
                return _subscribe(callback, *args, **kwargs)

            setattr(t2v.auth, name, late)

    h = _host(engine, before_controller=login_during_subscribe)
    try:
        h.turn("hello from a")
        h.t2v.auth.login("b@x.y", "pw")
        _assert_cleared(h.snap())
    finally:
        h.close()


def test_close_stops_following_auth(engine: FakeEngine) -> None:
    h = _host(engine)
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("hello")
        h.controller.close()
        before = h.controller.snapshot()
        h.t2v.auth.logout()
        assert h.controller.snapshot() == before
        registered = list(getattr(h.t2v.auth, "_early_listeners", [])) + h.t2v.auth._listeners
        assert h.controller._on_auth_state_change not in registered
    finally:
        h.t2v.close()


def test_a_delivery_that_lands_after_close_records_nothing(engine: FakeEngine) -> None:
    # T2VAuth copies its listener list before calling it, so a delivery already under way
    # on another thread can still reach a controller that has just closed.
    h = _host(engine)
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("hello")
        h.controller.close()
        before = h.controller.snapshot()
        h.t2v.auth.logout()
        h.controller._on_auth_state_change(None)  # the late delivery
        assert h.controller.snapshot() == before
    finally:
        h.t2v.close()


class _FlakyStorage(MemoryStorage):
    """A token store whose reads fail while ``failing`` (a locked file, say)."""

    def __init__(self) -> None:
        super().__init__()
        self.failing = False

    def get(self, key: str) -> Optional[str]:
        if self.failing:
            raise OSError("the token store is locked")
        return super().get(key)


def test_an_unreadable_token_store_resets_consent_and_keeps_the_rest(
    engine: FakeEngine,
) -> None:
    storage = _FlakyStorage()
    h = _host(engine, storage=storage)
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("delete CT")
        h.approve_always()
        storage.failing = True
        h.t2v.auth._notify_listeners(None)  # an auth event the controller cannot read
        storage.failing = False
        snapshot = h.snap()
        assert snapshot["alwaysAllowedTools"] == []  # fail closed on consent
        assert h.user_texts() == ["delete CT"]  # nothing else is guessed at
        # The identity was not taken for "signed out": a switch to B still clears.
        h.t2v.auth.login("b@x.y", "pw")
        _assert_cleared(h.snap())
    finally:
        h.close()


def test_a_raising_listener_ahead_of_the_controller_does_not_skip_its_reset(
    engine: FakeEngine,
) -> None:
    def raising_listener(t2v: Talk2View) -> None:
        t2v.auth.on_auth_state_change(lambda _user: 1 / 0)

    h = _host(engine, before_controller=raising_listener)
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("delete CT")
        h.approve_always()
        h.t2v.auth.logout()  # the listener in front raises; the controller still hears it
        h.t2v.auth.login("b@x.y", "pw")
        _assert_cleared(h.snap())
    finally:
        h.close()


def test_every_batch_replays_onto_the_first_snapshot(engine: FakeEngine) -> None:
    h = _host(engine)
    start = h.snap()
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("delete CT")
        h.approve_always()
        h.t2v.auth.logout()
        h.t2v.auth.start_anonymous()
        h.turn("hello")
        h.t2v.auth.login("b@x.y", "pw")
        assert h.log.replay(start) == h.snap()
    finally:
        h.close()
