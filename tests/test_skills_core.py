"""T2VSkills core: validation in the engine's units, debounced batch register, storage."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest
from pytest_httpserver import HTTPServer

from talk2view.client import T2VClient
from talk2view.errors import T2VError
from talk2view.skills import (
    MAX_REGISTER_BODY_BYTES,
    STORAGE_KEY,
    PlatformSkill,
    Skill,
    SkillValidationError,
    T2VSkills,
    check_body_size,
    register_payload,
)
from talk2view.state import SkillsState
from talk2view.storage import MemoryStorage
from talk2view.types import RegisterSkillsResponse


class ManualTimer:
    """A ``start_timer`` that never fires by itself: the test calls ``fire()``."""

    def __init__(self) -> None:
        self.pending: Optional[Tuple[float, Callable[[], None]]] = None
        self.started = 0
        self.cancelled = 0

    def __call__(self, delay_s: float, fn: Callable[[], None]) -> "ManualTimer":
        self.pending = (delay_s, fn)
        self.started += 1
        return self

    def cancel(self) -> None:
        if self.pending is not None:
            self.cancelled += 1
        self.pending = None

    def fire(self) -> None:
        assert self.pending is not None, "no timer is pending"
        _delay, fn = self.pending
        self.pending = None
        fn()


def _skills(
    response: Optional[Dict[str, Any]] = None,
) -> Tuple[T2VSkills, MagicMock, MemoryStorage, ManualTimer]:
    client = MagicMock()
    client.request.return_value = response or {"registered": [], "count": 0}
    storage = MemoryStorage()
    timer = ManualTimer()
    skills = T2VSkills(client, storage, start_timer=timer)
    return skills, client, storage, timer


def _registered_names(client: MagicMock) -> List[List[str]]:
    return [
        [s["name"] for s in call.kwargs["body"]["skills"]]
        for call in client.request.call_args_list
        if call.args[0] == "/v1/skills/register"
    ]


#: What both skills routes answer on a session without a skill registry (400).
SKILLS_DISABLED = {
    "error": {"type": "skills_disabled", "message": "Skills are not enabled for this session"}
}


# ── Validation (engine units) ────────────────────────────────────────────────


class TestValidation:
    def test_name_256_codepoints_rejected_255_ok(self) -> None:
        skills, _client, _storage, _timer = _skills()
        # U+1F600 is one code point but two UTF-16 units and four UTF-8 bytes: the
        # engine counts code points (Pydantic max_length on str), and so do we.
        astral = "\U0001F600"
        ok = astral * 255
        assert len(ok) == 255 and len(ok.encode("utf-16-le")) // 2 == 510
        skills.add(Skill(name=ok, content="body"))
        assert skills.get(ok) is not None
        with pytest.raises(SkillValidationError, match="256 code points"):
            skills.add(Skill(name=astral * 256, content="body"))

    def test_empty_name_and_empty_content_rejected(self) -> None:
        skills, _client, _storage, _timer = _skills()
        with pytest.raises(SkillValidationError, match="needs a name"):
            skills.add(Skill(name="", content="body"))
        with pytest.raises(SkillValidationError, match="no content"):
            skills.add(Skill(name="x", content=""))

    def test_allowed_tools_must_be_a_list_of_names(self) -> None:
        skills, _client, _storage, _timer = _skills()
        with pytest.raises(SkillValidationError, match="allowed_tools"):
            skills.add(Skill(name="x", content="b", allowed_tools=["ok", ""]))
        skills.add(Skill(name="x", content="b", allowed_tools=["set_layout"]))
        assert skills.get("x") == Skill(name="x", content="b", allowed_tools=["set_layout"])

    def test_body_over_8MB_bytes_rejected_client_side(self) -> None:  # noqa: N802
        skills, client, _storage, _timer = _skills()
        # 4 Mi code points of "é", half the cap as a char count; but json.dumps (what the
        # client sends) writes each as the 6-byte escape `é`, so the body is ~24 MiB.
        # The cap is in BYTES of that body, so a char count under it can still be over it.
        big = "é" * (4 * 1024 * 1024 + 1024)
        assert len(big) < MAX_REGISTER_BODY_BYTES
        with pytest.raises(SkillValidationError, match="bytes"):
            skills.add(Skill(name="big", content=big))
        assert skills.get_all() == []
        client.request.assert_not_called()

    def test_body_size_counts_the_whole_batch(self) -> None:
        skills, _client, _storage, _timer = _skills()
        half = "a" * (MAX_REGISTER_BODY_BYTES // 2)
        skills.add(Skill(name="one", content=half))
        with pytest.raises(SkillValidationError, match="bytes"):
            skills.add(Skill(name="two", content=half))
        # Replacing "one" is measured against the others, not against itself.
        skills.add(Skill(name="one", content=half + "b"))

    def test_re_enable_over_the_body_cap_raises_and_changes_nothing(self) -> None:
        skills, client, _storage, timer = _skills()
        half = "a" * (MAX_REGISTER_BODY_BYTES // 2)
        skills.add(Skill(name="one", content=half))
        skills.set_enabled("one", False)
        skills.add(Skill(name="two", content=half))
        before = skills.state()
        started = timer.started
        seen: List[SkillsState] = []
        skills.on_change(seen.append)
        with pytest.raises(SkillValidationError, match="bytes"):
            skills.set_enabled("one", True)
        assert skills.enabled() == [Skill(name="two", content=half)]
        assert skills.state() == before
        assert timer.started == started  # no edit scheduled
        assert seen == []  # and nothing announced
        client.request.assert_not_called()

    def test_re_enable_within_the_body_cap_works(self) -> None:
        skills, _client, _storage, timer = _skills()
        skills.add(Skill(name="a", content="1"))
        skills.set_enabled("a", False)
        started = timer.started
        skills.set_enabled("a", True)
        assert skills.enabled() == [Skill(name="a", content="1")]
        assert timer.started == started + 1

    def test_disable_never_checks_the_body_cap(self) -> None:
        # A saved list is not size-checked on load, so the enabled set can start over the
        # cap; disabling is how the host gets back under it and must never be refused.
        skills, _client, storage, _timer = _skills()
        half = "a" * (MAX_REGISTER_BODY_BYTES // 2)
        storage.set(
            STORAGE_KEY,
            json.dumps([{"name": "one", "content": half}, {"name": "two", "content": half}]),
        )
        skills.load()
        skills.set_enabled("one", False)
        assert skills.enabled() == [Skill(name="two", content=half)]

    def test_check_body_size_uses_the_wire_payload(self) -> None:
        payload = register_payload([Skill(name="n", content="c")])
        assert payload == {
            "skills": [{"name": "n", "description": "", "content": "c"}],
            "replace": True,
        }
        assert check_body_size([Skill(name="n", content="c")]) == len(
            json.dumps(payload).encode("utf-8")
        )

    def test_large_skill_warns_but_is_accepted(self, caplog: pytest.LogCaptureFixture) -> None:
        skills, _client, _storage, _timer = _skills()
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            skills.add(Skill(name="long", content="x" * 100_001))
        assert "100001 characters" in caplog.text
        assert skills.get("long") is not None


# ── Local list, debounce, register ───────────────────────────────────────────


class TestRegister:
    def test_add_is_debounced_into_one_batched_register(self) -> None:
        skills, client, _storage, timer = _skills({"registered": ["a", "b"], "count": 2})
        skills.add(Skill(name="a", content="1"))
        skills.add(Skill(name="b", content="2"))
        assert timer.started == 2 and timer.cancelled == 1  # the second add reset the timer
        client.request.assert_not_called()
        timer.fire()
        assert _registered_names(client) == [["a", "b"]]
        assert skills.state()["registered"] == ["a", "b"]
        assert skills.state()["available"] is True

    def test_register_sends_now_and_cancels_the_pending_timer(self) -> None:
        skills, client, _storage, timer = _skills({"registered": ["a"], "count": 1})
        skills.add(Skill(name="a", content="1"))
        response = skills.register()
        assert isinstance(response, RegisterSkillsResponse)
        assert response.registered == ["a"] and response.count == 1
        assert timer.pending is None
        assert _registered_names(client) == [["a"]]
        client.request.assert_called_once_with(
            "/v1/skills/register",
            method="POST",
            body={"skills": [{"name": "a", "description": "", "content": "1"}], "replace": True},
        )

    def test_register_with_skills_adds_them_as_code_skills(self) -> None:
        skills, client, _storage, _timer = _skills({"registered": ["a", "b"], "count": 2})
        skills.add(Skill(name="a", content="old"))
        skills.register([Skill(name="a", content="new"), Skill(name="b", content="2")])
        assert skills.get("a") == Skill(name="a", content="new")
        assert _registered_names(client) == [["a", "b"]]
        assert skills.state()["local"][0]["origin"] == "code"

    def test_register_with_skills_is_all_or_nothing(self) -> None:
        # Like add(): a batch that fails validation or the body cap leaves no trace.
        skills, client, _storage, timer = _skills()
        with pytest.raises(SkillValidationError, match="no content"):
            skills.register([Skill(name="a", content="1"), Skill(name="b", content="")])
        half = "a" * (MAX_REGISTER_BODY_BYTES // 2)
        with pytest.raises(SkillValidationError, match="bytes"):
            skills.register([Skill(name="one", content=half), Skill(name="two", content=half)])
        assert skills.get_all() == []
        assert timer.pending is None
        client.request.assert_not_called()

    def test_disabled_skills_are_not_sent(self) -> None:
        skills, client, _storage, _timer = _skills({"registered": ["a"], "count": 1})
        skills.add(Skill(name="a", content="1"))
        skills.add(Skill(name="b", content="2"))
        skills.set_enabled("b", False)
        skills.register()
        assert _registered_names(client) == [["a"]]
        assert [s["enabled"] for s in skills.state()["local"]] == [True, False]

    def test_set_enabled_unknown_name_raises(self) -> None:
        skills, _client, _storage, _timer = _skills()
        with pytest.raises(KeyError):
            skills.set_enabled("nope", False)

    def test_remove_returns_whether_it_was_there(self) -> None:
        skills, _client, _storage, _timer = _skills()
        skills.add(Skill(name="a", content="1"))
        assert skills.remove("a") is True
        assert skills.remove("a") is False
        assert skills.get_all() == []

    def test_on_a_merging_engine_remove_and_disable_need_a_session_reset(self) -> None:
        # No `replaced` key in the answer: an engine older than the flag, which merges.
        skills, _client, _storage, _timer = _skills({"registered": ["a", "b"], "count": 2})
        skills.add(Skill(name="a", content="1"))
        skills.add(Skill(name="b", content="2"))
        response = skills.register()
        assert response.replaced is None
        assert skills.replace_honoured() is False
        assert skills.needs_session_reset() is False
        skills.remove("a")
        assert skills.needs_session_reset() is True
        skills2, _c, _s, _t = _skills({"registered": ["b"], "count": 1})
        skills2.add(Skill(name="b", content="2"))
        skills2.register()
        skills2.set_enabled("b", False)
        assert skills2.state()["needsSessionReset"] is True

    def test_remove_of_a_never_registered_skill_needs_no_reset(self) -> None:
        skills, _client, _storage, _timer = _skills()
        skills.add(Skill(name="a", content="1"))
        skills.remove("a")
        assert skills.needs_session_reset() is False

    def test_e1_engine_turns_a_removal_into_one_debounced_replace(self) -> None:
        skills, client, _storage, timer = _skills(
            {"registered": ["a", "b"], "count": 2, "removed": [], "replaced": True}
        )
        skills.add(Skill(name="a", content="1"))
        skills.add(Skill(name="b", content="2"))
        response = skills.register()
        assert response.replaced is True
        assert skills.replace_honoured() is True
        client.request.return_value = {
            "registered": ["b"], "count": 1, "removed": ["a"], "replaced": True
        }
        skills.remove("a")
        assert skills.needs_session_reset() is False  # no recycle: the replace drops it
        assert timer.pending is not None
        timer.fire()
        assert _registered_names(client) == [["a", "b"], ["b"]]
        assert client.request.call_args.kwargs["body"]["replace"] is True
        assert skills.state()["registered"] == ["b"]
        assert skills.needs_session_reset() is False

    @pytest.mark.parametrize(
        "answer, honoured, reset",
        [({"replaced": True}, True, False), ({}, False, True)],
        ids=["replacing-engine", "merging-engine"],
    )
    def test_the_first_answer_decides_whether_a_removal_needs_a_reset(
        self, answer: Dict[str, Any], honoured: bool, reset: bool
    ) -> None:
        skills, _client, _storage, _timer = _skills(
            {"registered": ["a", "b"], "count": 2, **answer}
        )
        skills.add(Skill(name="a", content="1"))
        skills.add(Skill(name="b", content="2"))
        assert skills.replace_honoured() is None  # nothing answered yet
        skills.register()
        assert skills.replace_honoured() is honoured
        skills.set_enabled("a", False)
        assert skills.needs_session_reset() is reset

    def test_a_failed_replace_with_a_removal_pending_needs_a_reset(self) -> None:
        skills, client, _storage, timer = _skills(
            {"registered": ["a", "b"], "count": 2, "replaced": True}
        )
        skills.add(Skill(name="a", content="1"))
        skills.add(Skill(name="b", content="2"))
        skills.register()
        skills.remove("a")
        client.request.side_effect = T2VError("rate limited", "rate_limit_exceeded", 429)
        timer.fire()
        # Fail closed: the engine may still hold "a", and only a new session surely drops it.
        assert skills.needs_session_reset() is True
        client.request.side_effect = None
        client.request.return_value = {"registered": ["b"], "count": 1, "replaced": True}
        skills.flush()  # the edit stayed pending; a replace that lands clears the reset
        assert skills.needs_session_reset() is False
        assert _registered_names(client) == [["a", "b"], ["b"], ["b"]]

    def test_engine_without_replace_keeps_merging_registered_names(self) -> None:
        skills, client, _storage, timer = _skills({"registered": ["a"], "count": 1})
        skills.add(Skill(name="a", content="1"))
        skills.register()
        client.request.return_value = {"registered": ["b"], "count": 1}
        skills.add(Skill(name="b", content="2"))
        timer.fire()
        assert skills.state()["registered"] == ["a", "b"]

    def test_re_register_sends_the_enabled_set_and_clears_the_reset_flag(self) -> None:
        skills, client, _storage, _timer = _skills({"registered": ["a", "b"], "count": 2})
        skills.add(Skill(name="a", content="1"))
        skills.add(Skill(name="b", content="2"))
        skills.register()
        skills.remove("b")
        assert skills.needs_session_reset() is True
        client.request.return_value = {"registered": ["a"], "count": 1}
        response = skills.re_register()
        assert response is not None and response.registered == ["a"]
        assert skills.needs_session_reset() is False
        assert skills.state()["registered"] == ["a"]

    def test_re_register_with_nothing_enabled_sends_nothing_and_returns_none(self) -> None:
        skills, client, _storage, _timer = _skills()
        assert skills.re_register() is None
        client.request.assert_not_called()
        assert skills.state()["registered"] == []

    def test_re_register_sends_an_empty_replace_when_a_disable_is_pending(self) -> None:
        # The engine keeps one session per user and may hand the old one back on create:
        # a pending "disable the last skill" must still reach it, as an empty replace.
        skills, client, _storage, _timer = _skills(
            {"registered": ["a"], "count": 1, "replaced": True}
        )
        skills.add(Skill(name="a", content="1"))
        skills.register()
        skills.set_enabled("a", False)
        client.request.return_value = {
            "registered": [], "count": 0, "removed": ["a"], "replaced": True
        }
        response = skills.re_register()
        assert response is not None and response.removed == ["a"]
        assert client.request.call_args.kwargs["body"] == {"skills": [], "replace": True}
        assert skills.state()["registered"] == []

    def test_a_failed_re_register_keeps_the_skills_pending(self) -> None:
        skills, client, _storage, timer = _skills(
            {"registered": ["a"], "count": 1, "replaced": True}
        )
        skills.add(Skill(name="a", content="1"))
        skills.register()
        client.request.side_effect = T2VError("rate limited", "rate_limit_exceeded", 429)
        with pytest.raises(T2VError, match="rate limited"):
            skills.re_register()
        assert skills.state()["registered"] == ["a"]  # nothing cleared on a failure
        assert timer.pending is None
        client.request.side_effect = None
        skills.flush()  # still pending, so the next flush (chat() does one) retries it
        assert _registered_names(client) == [["a"], ["a"], ["a"]]
        skills.flush()  # and once it landed, nothing is pending any more
        assert len(_registered_names(client)) == 3

    # POST /v1/sessions is get-or-create: a session that did not follow this client's own
    # confirmed DELETE may be one the engine kept, holding skills nobody here knows about
    # (``registered`` is empty after a restart).

    def test_a_failed_re_register_on_a_possibly_reused_session_flags_a_reset(self) -> None:
        skills, client, _storage, _timer = _skills()
        skills.add(Skill(name="a", content="1"))
        client.request.side_effect = T2VError("rate limited", "rate_limit_exceeded", 429)
        with pytest.raises(T2VError, match="rate limited"):
            skills.re_register()  # known_fresh defaults to False
        assert skills.needs_session_reset() is True
        assert skills.state()["needsSessionReset"] is True

    def test_a_failed_re_register_on_a_known_fresh_session_flags_nothing(self) -> None:
        # A merging engine: s1 holds a and b, "b" is removed, so a reset is pending. The
        # recycle's new session holds no user skills at all, so a failed register there
        # leaves nothing unwanted live: no reset, and the edit waits for the next flush.
        skills, client, _storage, _timer = _skills({"registered": ["a", "b"], "count": 2})
        skills.add(Skill(name="a", content="1"))
        skills.add(Skill(name="b", content="2"))
        skills.register()
        skills.remove("b")
        assert skills.needs_session_reset() is True
        client.request.side_effect = T2VError("rate limited", "rate_limit_exceeded", 429)
        with pytest.raises(T2VError, match="rate limited"):
            skills.re_register(known_fresh=True)
        assert skills.needs_session_reset() is False
        assert skills.state()["registered"] == []
        client.request.side_effect = None
        client.request.return_value = {"registered": ["a"], "count": 1}
        skills.flush()  # still pending
        assert _registered_names(client)[-1] == ["a"]
        assert skills.state()["registered"] == ["a"]

    def test_an_over_cap_re_register_flags_no_reset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing was sent, and a new session would fail the same way: not a reset.
        import talk2view.skills as skills_module

        skills, client, _storage, _timer = _skills()
        skills.add(Skill(name="a", content="x" * 50))
        monkeypatch.setattr(skills_module, "MAX_REGISTER_BODY_BYTES", 40)
        assert skills.re_register() is None
        client.request.assert_not_called()
        assert skills.needs_session_reset() is False

    def test_flush_sends_a_pending_edit_now(self) -> None:
        skills, client, _storage, timer = _skills({"registered": ["a"], "count": 1})
        skills.add(Skill(name="a", content="1"))
        skills.flush()
        assert timer.pending is None
        assert _registered_names(client) == [["a"]]
        skills.flush()  # nothing pending: no second call
        assert len(_registered_names(client)) == 1

    def test_background_failure_is_logged_and_kept_dirty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        skills, client, _storage, timer = _skills()
        client.request.side_effect = T2VError("rate limited", "rate_limit_error", 429)
        skills.add(Skill(name="a", content="1"))
        with caplog.at_level(logging.ERROR, logger="talk2view.skills"):
            timer.fire()
        assert "Skills registration failed" in caplog.text
        assert skills.needs_session_reset() is False  # nothing was removed: no reset
        client.request.side_effect = None
        client.request.return_value = {"registered": ["a"], "count": 1}
        skills.flush()
        assert _registered_names(client) == [["a"], ["a"]]

    def test_an_oversized_pending_set_logs_one_warning_not_a_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A saved list is not size-checked on load, so every tick or flush of it fails the
        # same way: one warning line naming the size and the cap, not a traceback each time.
        skills, client, storage, _timer = _skills()
        half = "a" * (MAX_REGISTER_BODY_BYTES // 2)
        storage.set(
            STORAGE_KEY,
            json.dumps([{"name": "one", "content": half}, {"name": "two", "content": half}]),
        )
        skills.load()
        caplog.clear()  # load's own large-skill warnings
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            skills.flush()
        records = [r for r in caplog.records if r.name == "talk2view.skills"]
        assert [(r.levelno, r.exc_info) for r in records] == [(logging.WARNING, None)]
        message = records[0].getMessage()
        assert "bytes" in message and str(MAX_REGISTER_BODY_BYTES) in message
        client.request.assert_not_called()
        assert skills.needs_session_reset() is False

    def test_skills_disabled_nonfatal(self, httpserver: HTTPServer) -> None:
        # The engine's HTTPException handler sends the detail dict as the whole body:
        # a top-level {"error": {...}}, not {"detail": {"error": ...}}.
        httpserver.expect_request("/v1/skills/register", method="POST").respond_with_json(
            SKILLS_DISABLED, status=400
        )
        storage = MemoryStorage()
        storage.set("access_token", "tok")
        client = T2VClient("pk_test", storage, base_url=httpserver.url_for(""))
        try:
            skills = T2VSkills(client, storage, start_timer=ManualTimer())
            skills.add(Skill(name="a", content="1"))
            response = skills.register()
            assert response == RegisterSkillsResponse(registered=[], count=0)
            assert skills.state()["available"] is False
            assert skills.state()["registered"] == []
            assert skills.re_register() is None
        finally:
            client.close()

    def test_other_register_errors_propagate_and_stay_pending(self) -> None:
        skills, client, _storage, _timer = _skills()
        client.request.side_effect = T2VError("boom", "internal_error", 500)
        skills.add(Skill(name="a", content="1"))
        with pytest.raises(T2VError, match="boom"):
            skills.register()
        client.request.side_effect = None
        client.request.return_value = {"registered": ["a"], "count": 1}
        skills.flush()
        assert _registered_names(client) == [["a"], ["a"]]


# ── Storage ─────────────────────────────────────────────────────────────────


class TestStorage:
    def test_save_load_key_t2v_user_skills(self) -> None:
        skills, _client, storage, _timer = _skills()
        skills.add(Skill(name="a", description="d", content="1", allowed_tools=["t"]))
        skills.add(Skill(name="b", content="2"))
        skills.set_enabled("b", False)
        skills.save()
        raw = storage.get(STORAGE_KEY)
        assert STORAGE_KEY == "t2v_user_skills"
        assert raw is not None
        assert json.loads(raw) == [
            {
                "name": "a",
                "description": "d",
                "content": "1",
                "allowed_tools": ["t"],
                "enabled": True,
            },
            {"name": "b", "description": "", "content": "2", "enabled": False},
        ]

        fresh = T2VSkills(MagicMock(), storage, start_timer=ManualTimer())
        loaded = fresh.load()
        assert loaded == [
            Skill(name="a", description="d", content="1", allowed_tools=["t"]),
            Skill(name="b", content="2"),
        ]
        assert [s["enabled"] for s in fresh.state()["local"]] == [True, False]

    def test_load_does_not_register_until_re_register(self) -> None:
        skills, client, storage, timer = _skills({"registered": ["a"], "count": 1})
        storage.set(STORAGE_KEY, json.dumps([{"name": "a", "content": "1"}]))
        skills.load()
        assert timer.pending is None
        client.request.assert_not_called()
        skills.re_register()
        assert _registered_names(client) == [["a"]]

    def test_load_reads_the_typescript_shape_and_ignores_corrupt_data(self) -> None:
        skills, _client, storage, _timer = _skills()
        storage.set(STORAGE_KEY, "{not json")
        assert skills.load() == []
        storage.set(STORAGE_KEY, json.dumps([{"name": "ts", "description": "", "content": "c"}]))
        assert skills.load() == [Skill(name="ts", content="c")]
        storage.set(STORAGE_KEY, json.dumps([{"name": "", "content": "c"}, 5]))
        assert skills.load() == []

    @pytest.mark.parametrize(
        "flag, enabled, warns",
        [
            ({}, True, False),
            ({"enabled": True}, True, False),
            ({"enabled": False}, False, False),
            ({"enabled": "false"}, False, True),
            ({"enabled": "true"}, False, True),
            ({"enabled": 0}, False, True),
            ({"enabled": 1}, False, True),
            ({"enabled": None}, False, True),
        ],
        ids=["missing-key", "true", "false", "str-false", "str-true", "zero", "one", "null"],
    )
    def test_load_reads_enabled_fail_closed(
        self,
        flag: Dict[str, Any],
        enabled: bool,
        warns: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # A TS-saved list has no flag (enabled); only a real `true` enables a flagged one.
        skills, _client, storage, _timer = _skills()
        storage.set(STORAGE_KEY, json.dumps([{"name": "a", "content": "1", **flag}]))
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            assert skills.load() == [Skill(name="a", content="1")]
        assert [s["enabled"] for s in skills.state()["local"]] == [enabled]
        if warns:
            assert "'a'" in caplog.text and "disabled" in caplog.text
        else:
            assert caplog.text == ""

    def test_clear_forgets_everything_and_deletes_the_key(self) -> None:
        skills, _client, storage, _timer = _skills({"registered": ["a"], "count": 1})
        skills.add(Skill(name="a", content="1"))
        skills.save()
        skills.register()
        skills.clear()
        assert skills.get_all() == []
        assert storage.get(STORAGE_KEY) is None
        assert skills.needs_session_reset() is True


# ── Platform catalogue, state, listeners ─────────────────────────────────────


class TestStateAndPlatform:
    def test_list_platform_returns_none_on_404(self) -> None:
        skills, client, _storage, _timer = _skills()
        client.request.side_effect = T2VError("Not Found", "not_found", 404)
        assert skills.list_platform() is None
        assert skills.state()["platform"] is None

    def test_list_platform_skips_user_rows_and_keeps_the_first_row_per_name(self) -> None:
        skills, client, _storage, _timer = _skills()
        # The engine's shape: precedence order (user > partner > builtin); a row hidden by
        # a same-named higher-precedence skill is included with `shadowed: true`.
        client.request.return_value = {
            "skills": [
                {"name": "measure", "description": "Mine", "source": "user", "shadowed": False},
                {
                    "name": "measure",
                    "description": "Partner's",
                    "source": "partner",
                    "shadowed": True,
                },
                {"name": "general", "description": "", "source": "builtin", "shadowed": False},
                {
                    "name": "report",
                    "description": "Partner report",
                    "source": "partner",
                    "shadowed": False,
                },
                {
                    "name": "report",
                    "description": "Built-in report",
                    "source": "builtin",
                    "shadowed": True,
                },
            ]
        }
        assert skills.list_platform() == [
            PlatformSkill(name="measure", description="Partner's", source="partner"),
            PlatformSkill(name="general", source="builtin"),
            PlatformSkill(name="report", description="Partner report", source="partner"),
        ]
        client.request.assert_called_once_with("/v1/skills")
        skills.add(Skill(name="measure", content="mine"))
        skills.add(Skill(name="notes", content="only mine"))
        state = skills.state()
        assert state["platform"] == [
            {"name": "measure", "description": "Partner's", "source": "partner"},
            {"name": "general", "description": "", "source": "builtin"},
            {"name": "report", "description": "Partner report", "source": "partner"},
        ]
        assert [(s["name"], s.get("overridesPlatform")) for s in state["local"]] == [
            ("measure", True),
            ("notes", None),
        ]

    def test_list_platform_tolerates_unknown_sources_and_bad_rows(self) -> None:
        skills, client, _storage, _timer = _skills()
        client.request.return_value = {
            "skills": [
                {"name": "notes", "description": "", "source": "user", "shadowed": False},
                {"name": "org-guide", "description": "d", "source": "org", "shadowed": False},
                {"name": "legacy"},
                {"description": "no name"},
                "not a row",
            ]
        }
        assert skills.list_platform() == [
            PlatformSkill(name="org-guide", description="d", source="org"),
            PlatformSkill(name="legacy"),
        ]
        skills.add(Skill(name="notes", content="only mine"))
        assert "overridesPlatform" not in skills.state()["local"][0]

    def test_list_platform_skills_disabled_is_none(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/skills", method="GET").respond_with_json(
            SKILLS_DISABLED, status=400
        )
        storage = MemoryStorage()
        storage.set("access_token", "tok")
        client = T2VClient("pk_test", storage, base_url=httpserver.url_for(""))
        try:
            skills = T2VSkills(client, storage, start_timer=ManualTimer())
            assert skills.list_platform() is None
            assert skills.state()["platform"] is None
            assert skills.state()["available"] is False
        finally:
            client.close()

    def test_list_platform_other_errors_propagate(self) -> None:
        skills, client, _storage, _timer = _skills()
        client.request.side_effect = T2VError("nope", "internal_error", 500)
        with pytest.raises(T2VError):
            skills.list_platform()

    def test_state_shape(self) -> None:
        skills, _client, _storage, _timer = _skills()
        empty: SkillsState = skills.state()
        assert empty == {
            "local": [],
            "platform": None,
            "registered": [],
            "available": False,
            "needsSessionReset": False,
            "store": {"writable": False, "canOpenFolder": False},
        }
        skills.add(Skill(name="a", description="d", content="1"))
        assert skills.state()["local"] == [
            {
                "name": "a",
                "description": "d",
                "enabled": True,
                "origin": "code",
                "label": "code",
                "editable": False,
                "hasExtraFiles": False,
                "problems": [],
            }
        ]

    def test_on_change_receives_the_state_after_every_mutation(self) -> None:
        skills, _client, _storage, timer = _skills({"registered": ["a"], "count": 1})
        seen: List[SkillsState] = []
        off = skills.on_change(seen.append)
        skills.add(Skill(name="a", content="1"))
        timer.fire()
        skills.set_enabled("a", False)
        assert [s["registered"] for s in seen] == [[], ["a"], ["a"]]
        assert seen[-1]["needsSessionReset"] is True
        off()
        skills.remove("a")
        assert len(seen) == 3

    def test_a_raising_listener_does_not_break_the_mutation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        skills, _client, _storage, _timer = _skills()

        def bad(_state: SkillsState) -> None:
            raise RuntimeError("listener bug")

        skills.on_change(bad)
        with caplog.at_level(logging.ERROR, logger="talk2view.skills"):
            skills.add(Skill(name="a", content="1"))
        assert skills.get("a") is not None
        assert "listener raised" in caplog.text


# ── Serialized posts ────────────────────────────────────────────────────────


class _BlockingClient:
    """A register client whose ``block_on``-th POST (the first by default) blocks until
    ``release`` is set, and whose POSTs from the ``fail_from``-th on fail with a 429 (none by
    default). ``log`` records each POST's start and outcome with the names it carried."""

    def __init__(self, *, block_on: int = 1, fail_from: Optional[int] = None) -> None:
        self.release = threading.Event()
        self.blocked = threading.Event()
        self.log: List[Tuple[str, List[str]]] = []
        self._block_on = block_on
        self._fail_from = fail_from
        self._lock = threading.Lock()
        self._posts = 0

    def request(
        self, endpoint: str, method: str = "GET", body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        assert body is not None
        names = [s["name"] for s in body["skills"]]
        with self._lock:
            self._posts += 1
            post = self._posts
            self.log.append(("start", names))
        if post == self._block_on:
            self.blocked.set()
            self.release.wait(10)  # bounded; the test's joins give up first
        failed = self._fail_from is not None and post >= self._fail_from
        with self._lock:
            self.log.append(("fail" if failed else "end", names))
        if failed:
            raise T2VError("rate limited", "rate_limit_exceeded", 429)
        return {"registered": names, "count": len(names), "replaced": True}


def _join(*threads: threading.Thread) -> None:
    for thread in threads:
        thread.join(5)
    assert not any(t.is_alive() for t in threads), "a skills post never finished (deadlock?)"


def _cleanup(client: _BlockingClient, *threads: threading.Thread) -> None:
    """The threaded tests' ``finally``: unblock the client and reap every started thread
    (bounded), so a failed assertion leaves no thread running into the next test."""
    client.release.set()
    for thread in threads:
        if thread.ident is not None:  # started
            thread.join(5)


def _post_lock_free(skills: T2VSkills) -> bool:
    """Whether ANOTHER thread can take ``skills``' post lock right now (bounded wait): the
    same thread always could, the lock being re-entrant."""
    acquired: List[bool] = []

    def probe() -> None:
        got = skills._post_lock.acquire(timeout=1)
        if got:
            skills._post_lock.release()
        acquired.append(got)

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(5)
    return acquired == [True]


class TestSerializedPosts:
    def test_a_foreground_register_waits_for_the_background_post(self) -> None:
        client = _BlockingClient()
        timer = ManualTimer()
        skills = T2VSkills(client, MemoryStorage(), start_timer=timer)
        skills.add(Skill(name="a", content="1"))
        results: List[RegisterSkillsResponse] = []
        background = threading.Thread(target=timer.fire, daemon=True)  # the debounced sync
        foreground = threading.Thread(
            target=lambda: results.append(skills.register()), daemon=True
        )
        try:
            background.start()
            assert client.blocked.wait(5)
            skills.add(Skill(name="b", content="2"))
            foreground.start()
            foreground.join(0.2)
            # The second POST has not started while the first is in flight.
            assert foreground.is_alive()
            assert client.log == [("start", ["a"])]
            client.release.set()
            _join(background, foreground)
        finally:
            _cleanup(client, background, foreground)
        # One POST at a time, in snapshot order, and the last one sent is what we record.
        assert client.log == [
            ("start", ["a"]),
            ("end", ["a"]),
            ("start", ["a", "b"]),
            ("end", ["a", "b"]),
        ]
        assert results[0].registered == ["a", "b"]
        assert skills.state()["registered"] == ["a", "b"]

    def test_listeners_run_after_the_post_lock_is_released(self) -> None:
        # The controller calls flush() while holding its own (re-entrant) lock, and its
        # on_change callback takes that lock: a callback run while this object still holds
        # its post lock would deadlock against that controller thread.
        client = _BlockingClient()
        timer = ManualTimer()
        skills = T2VSkills(client, MemoryStorage(), start_timer=timer)
        controller_lock = threading.RLock()
        seen: List[List[str]] = []

        def on_change(state: SkillsState) -> None:
            with controller_lock:
                seen.append(state["registered"])

        skills.on_change(on_change)
        skills.add(Skill(name="a", content="1"))

        def controller_turn() -> None:
            with controller_lock:
                client.release.set()
                skills.flush()  # "b" is pending: needs the post lock the sync holds

        background = threading.Thread(target=timer.fire, daemon=True)
        foreground = threading.Thread(target=controller_turn, daemon=True)
        try:
            background.start()
            assert client.blocked.wait(5)
            skills.add(Skill(name="b", content="2"))
            foreground.start()
            _join(background, foreground)
        finally:
            _cleanup(client, background, foreground)
        assert skills.state()["registered"] == ["a", "b"]
        assert client.log[-2:] == [("start", ["a", "b"]), ("end", ["a", "b"])]
        assert ["a", "b"] in seen

    @pytest.mark.parametrize("fails", [False, True], ids=["ok", "failing"])
    @pytest.mark.parametrize("call", ["register", "re_register", "flush"])
    def test_every_register_path_notifies_after_releasing_the_post_lock(
        self, call: str, fails: bool
    ) -> None:
        # The rule the test above guards through the timer and flush(), for every path that
        # posts, answered or failed: a listener runs only once another thread (a waiting
        # controller thread, say) can take the post lock.
        skills, client, _storage, _timer = _skills(
            {"registered": ["a"], "count": 1, "replaced": True}
        )
        skills.add(Skill(name="a", content="1"))
        if fails:
            client.request.side_effect = T2VError("boom", "internal_error", 500)
        free: List[bool] = []
        skills.on_change(lambda _state: free.append(_post_lock_free(skills)))
        if fails and call != "flush":  # flush() never raises
            with pytest.raises(T2VError, match="boom"):
                getattr(skills, call)()
        else:
            getattr(skills, call)()
        assert client.request.call_count == 1
        assert free == [True]

    def test_flush_waits_out_an_in_flight_replace(self) -> None:
        # chat() flushes and then reads needs_session_reset() before every turn. If flush
        # returned while the timer's replace was still in flight, a replace that then failed
        # would flag the reset only after the turn had gone out: fail open.
        client = _BlockingClient(block_on=2, fail_from=2)
        timer = ManualTimer()
        skills = T2VSkills(client, MemoryStorage(), start_timer=timer)
        skills.add(Skill(name="a", content="1"))
        skills.add(Skill(name="b", content="2"))
        skills.register()  # POST 1, answered `replaced: true`: the engine holds a and b
        skills.set_enabled("a", False)  # on such an engine: no reset, just the next replace
        assert skills.needs_session_reset() is False
        background = threading.Thread(target=timer.fire, daemon=True)
        flusher = threading.Thread(target=skills.flush, daemon=True)
        try:
            background.start()
            assert client.blocked.wait(5)  # POST 2 in flight; its snapshot took the edit
            flusher.start()
            flusher.join(0.2)
            assert flusher.is_alive()  # flush waits for that POST instead of returning
            client.release.set()  # POST 2 fails with a 429
            _join(background, flusher)
        finally:
            _cleanup(client, background, flusher)
        assert skills.needs_session_reset() is True
        # And flush then sent the still-pending replace itself (it failed too), in turn.
        assert client.log[2:] == [
            ("start", ["b"]),
            ("fail", ["b"]),
            ("start", ["b"]),
            ("fail", ["b"]),
        ]
