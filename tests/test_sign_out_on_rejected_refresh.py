"""A refresh token the engine rejects signs the end-user out: announced, like logout().

``T2VClient`` clears the stored session when ``/v1/auth/refresh`` answers 401, on each of
its three request paths (JSON, upload, stream). The ``on_auth_state_change`` listeners then
hear ``None`` once, with no client lock held, so ``Talk2View`` drops the session and a
``ChatController`` resets consent and the conversation straight away rather than at the next
auth event.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

import pytest

from talk2view import Skill, Talk2View
from talk2view.controller import ChatController
from talk2view.errors import AuthenticationError
from talk2view.types import Decision

from .controller_fakes import inline_executor
from .fake_engine import TOOL, FakeEngine
from .test_controller_identity import Host

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _signed_in(engine: FakeEngine) -> Talk2View:
    t2v = Talk2View("pk_test", base_url=engine.url())
    t2v.auth.login("a@x.y", "pw")
    return t2v


def _record(t2v: Talk2View) -> List[Any]:
    events: List[Any] = []
    t2v.auth.on_auth_state_change(events.append)
    return events


def _list_models(t2v: Talk2View) -> None:
    t2v.list_models()


def _upload(t2v: Talk2View) -> None:
    t2v.upload_attachment(PNG, filename="scan.png")


def _stream(t2v: Talk2View) -> None:
    list(t2v.chat("hi"))


@pytest.mark.parametrize(
    "call", [_list_models, _upload, _stream], ids=["request", "upload_request", "stream"]
)
def test_a_rejected_refresh_announces_one_sign_out(
    engine: FakeEngine, call: Callable[[Talk2View], None]
) -> None:
    t2v = _signed_in(engine)
    try:
        t2v.create_session()  # so the stream path reaches the stream's own 401
        events = _record(t2v)
        engine.expire("user-a")

        with pytest.raises(AuthenticationError, match="Session expired"):
            call(t2v)

        assert events == [None]
        assert t2v.auth.is_authenticated() is False
        assert t2v.auth.get_user() is None
        assert t2v.get_session() is None  # Talk2View's own listener dropped it
    finally:
        t2v.close()


def test_nothing_is_announced_when_nobody_was_signed_in(engine: FakeEngine) -> None:
    t2v = Talk2View("pk_test", base_url=engine.url())
    try:
        events = _record(t2v)
        with pytest.raises(AuthenticationError):
            t2v.list_models()
        assert events == []
    finally:
        t2v.close()


def test_the_listeners_run_with_no_client_lock_held(engine: FakeEngine) -> None:
    t2v = _signed_in(engine)
    try:
        held: List[bool] = []
        t2v.auth.on_auth_state_change(
            lambda _user: held.append(t2v._client._refresh_lock.locked())
        )
        engine.expire("user-a")
        with pytest.raises(AuthenticationError):
            t2v.list_models()
        assert held == [False]
    finally:
        t2v.close()


def test_a_raising_listener_does_not_stop_the_others(engine: FakeEngine) -> None:
    t2v = _signed_in(engine)
    try:
        t2v.auth.on_auth_state_change(lambda _user: 1 / 0)
        events = _record(t2v)
        engine.expire("user-a")
        with pytest.raises(AuthenticationError, match="Session expired"):
            t2v.list_models()
        assert events == [None]
    finally:
        t2v.close()


def test_a_sign_out_during_a_skills_register_is_announced_after_the_post_lock(
    engine: FakeEngine,
) -> None:
    """The skills post lock is held across the register POST. The sign-out that POST's
    rejected refresh causes must not run the auth listeners (the ChatController's among
    them) under that lock: it is held back until the lock is released."""
    t2v = _signed_in(engine)
    try:
        t2v.skills.add(Skill(name="a", content="A"))
        t2v.create_session()  # registers "a"
        under_post_lock: List[Optional[bool]] = []
        t2v.auth.on_auth_state_change(
            lambda _user: under_post_lock.append(t2v.skills._post_lock._is_owned())  # type: ignore[attr-defined]
        )
        events = _record(t2v)
        engine.expire("user-a")
        t2v.skills.add(Skill(name="b", content="B"))

        t2v.skills.flush()  # never raises: the failure is logged, the edit stays pending

        assert events == [None]
        assert under_post_lock == [False]
        assert t2v.auth.is_authenticated() is False
    finally:
        t2v.close()


def test_the_controller_resets_on_a_rejected_refresh(engine: FakeEngine) -> None:
    h = Host(engine)
    try:
        h.t2v.auth.login("a@x.y", "pw")
        h.turn("delete CT")
        h.approve_always()
        assert h.snap()["alwaysAllowedTools"] == [TOOL]
        events = _record(h.t2v)
        engine.expire("user-a")

        h.turn("delete CT")  # its stream 401s, and the refresh is rejected

        assert events == [None]
        snapshot = h.snap()
        assert snapshot["alwaysAllowedTools"] == []
        assert snapshot["messages"] == []
        assert snapshot["pendingApproval"] is None
        assert snapshot["isLoading"] is False
        assert h.ran == ["CT"]  # only A's approved call ever ran
    finally:
        h.close()


def test_the_controller_hears_a_rejected_refresh_from_a_host_call(engine: FakeEngine) -> None:
    t2v = _signed_in(engine)
    controller = ChatController(t2v, executor=inline_executor)
    try:
        controller.send("hello")
        assert controller.wait_idle(5.0)
        engine.expire("user-a")
        with pytest.raises(AuthenticationError):
            t2v.list_models()  # the host's own call, not the controller's
        assert controller.snapshot()["messages"] == []
        controller.approve(Decision("once"))  # nothing pending: a no-op
    finally:
        controller.close()
        t2v.close()
