"""End to end: real Talk2View + ChatController + T2VSkills, anonymous auto-start and three
identities against one stateful fake engine (from the final review's probe_e2e).

anon-1 (the demo) -> user-b (signup: convert 409, then login) -> signed out -> anon-2 (the
next send auto-starts it). Each engine session must hold exactly the enabled skills, an
"always allow" must never outlive its identity, the demo's conversation carries into the
account, and nothing of the account's reaches anon-2.
"""

from __future__ import annotations

from typing import List

from talk2view import ChatController, Skill, Talk2View, tool
from talk2view.types import Decision

from .controller_fakes import BatchLog
from .fake_engine import TOOL, FakeEngine


def test_skills_consent_and_conversation_across_three_identities(engine: FakeEngine) -> None:
    ran: List[str] = []

    @tool(permission=True)
    def delete_volume(name: str) -> str:
        """Delete a volume.

        Args:
            name: The volume.
        """
        ran.append(name)
        return "deleted"

    t2v = Talk2View("pk_test", base_url=engine.url(), anonymous_auto_start=True)
    t2v.tools.register_from_functions([delete_volume])
    t2v.skills.add(Skill(name="alpha", content="A"))
    t2v.skills.add(Skill(name="beta", content="B"))
    controller = ChatController(t2v)  # the default worker-thread executor
    log = BatchLog()
    controller.subscribe(log)
    start = controller.snapshot()

    def turn(text: str) -> None:
        controller.send(text)
        assert controller.wait_idle(5.0)

    def user_texts() -> List[str]:
        return [m["content"] for m in controller.snapshot()["messages"] if m["role"] == "user"]

    try:
        # anon-1: the demo, started by the first send (warm_up may share that start).
        t2v.warm_up()
        turn("hello")
        snapshot = controller.snapshot()
        assert snapshot["error"] is None
        assert snapshot["isAnonymous"] is True
        assert t2v.auth.get_user() is not None and t2v.auth.get_user().id == "anon-1"  # type: ignore[union-attr]
        assert engine.skills_of("anon-1") == ["alpha", "beta"]

        turn("delete CT")
        controller.approve(Decision("always"))
        assert controller.wait_idle(5.0)
        assert ran == ["CT"]
        assert controller.snapshot()["alwaysAllowedTools"] == [TOOL]

        t2v.skills.remove("beta")
        t2v.skills.flush()
        assert engine.skills_of("anon-1") == ["alpha"]

        # user-b: signup converts; the email has an account (409), so it signs in instead.
        outcome = t2v.auth.signup("b@x.y", "pw")
        assert outcome.user is not None and outcome.user.id == "user-b"
        snapshot = controller.snapshot()
        assert user_texts() == ["hello", "delete CT"]  # the demo carries into the account
        assert snapshot["alwaysAllowedTools"] == []  # consent does not
        assert snapshot["isAnonymous"] is False

        turn("delete CT again")
        assert controller.snapshot()["pendingApproval"] is not None  # asked afresh
        assert ran == ["CT"]
        assert engine.skills_of("user-b") == ["alpha"]
        assert engine.histories_of("user-b")[0][0] == ("user", "hello")

        # Signed out: the account's conversation and its pending card go.
        t2v.auth.logout()
        snapshot = controller.snapshot()
        assert snapshot["messages"] == []
        assert snapshot["pendingApproval"] is None
        controller.approve(Decision("once"))  # the old card: nothing to answer
        assert controller.wait_idle(5.0)
        assert ran == ["CT"]

        # anon-2: auto-started by this send, which it must not disturb.
        turn("third")
        snapshot = controller.snapshot()
        assert snapshot["error"] is None
        assert [(m["role"], m["content"]) for m in snapshot["messages"]] == [
            ("user", "third"),
            ("assistant", "ok"),
        ]
        assert snapshot["isAnonymous"] is True
        assert engine.histories_of("anon-2") == [[("user", "third")]]

        for user_id in ("anon-1", "user-b", "anon-2"):
            assert engine.skills_of(user_id) == ["alpha"], user_id
        assert log.replay(start) == controller.snapshot()
    finally:
        controller.close()
        t2v.close()
