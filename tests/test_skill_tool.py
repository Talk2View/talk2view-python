"""save_skill: the assistant proposes, the end-user saves."""

from talk2view.skill_store import FolderSkillStore
from talk2view.skill_tool import SAVE_SKILL, SAVE_SKILL_SCHEMA, save_skill_handler
from talk2view.tools import T2VTools
from talk2view.types import ClientToolSchema

from .test_skills_store_integration import skills


class NoopClient:
    """A ``T2VTools`` needs a client for its own register calls; execute_tool_call and
    friends never touch it."""

    def request(self, path, method="GET", body=None):
        return {"registered": [], "count": 0}


def handler(tmp_path):
    s = skills()
    s.use_store(FolderSkillStore(tmp_path))
    return s, save_skill_handler(s)


def test_schema_is_small_and_complete():
    assert SAVE_SKILL_SCHEMA.name == SAVE_SKILL
    props = SAVE_SKILL_SCHEMA.parameters["properties"]
    assert set(props) == {"name", "description", "instructions", "replaces"}
    assert SAVE_SKILL_SCHEMA.parameters["required"] == ["name", "description", "instructions"]
    words = len(
        (SAVE_SKILL_SCHEMA.description + " ".join(p["description"] for p in props.values())).split()
    )
    assert words <= 70  # ~150 tokens: it rides on every request
    assert "consistent terms" in props["instructions"]["description"]


# The handler is always called the production way: keyword arguments, exactly how
# `T2VTools.execute_tool_call` invokes any registered handler (`handler(**clean_args)`).


def test_save_skill_writes_and_says_when_it_applies(tmp_path):
    s, run = handler(tmp_path)
    out = run(name="report-in-ml", description="Reports volumes in mL. Use when measuring.",
              instructions="Report segment volumes in mL, one decimal.")
    assert "report-in-ml" in out and "next message" in out
    assert (tmp_path / "report-in-ml" / "SKILL.md").is_file()


def test_save_skill_invalid_writes_nothing_and_explains(tmp_path):
    _s, run = handler(tmp_path)
    out = run(name="Report_ML", description="", instructions="x")
    assert out.startswith("Not saved.")
    assert "name:" in out and "Report_ML" in out and "description:" in out
    assert list(tmp_path.iterdir()) == []


def test_save_skill_update_uses_replaces(tmp_path):
    _s, run = handler(tmp_path)
    args = {"name": "report-in-ml", "description": "Reports mL. Use when measuring.",
            "instructions": "One decimal."}
    run(**args)
    run(**{**args, "instructions": "Two decimals.", "replaces": "report-in-ml"})
    assert "Two decimals." in (tmp_path / "report-in-ml" / "SKILL.md").read_text()


def test_save_skill_taken_name_without_replaces_explains(tmp_path):
    _s, run = handler(tmp_path)
    args = {"name": "report-in-ml", "description": "Reports mL. Use when measuring.",
            "instructions": "x"}
    run(**args)
    out = run(**{**args, "instructions": "y"})
    assert out.startswith("Not saved.") and "replaces" in out


def test_save_skill_ignores_an_unknown_extra_keyword(tmp_path):
    """A stray extra key a model sent -- even one literally named ``args`` -- must never
    raise: it is simply not one of the fields the handler reads."""
    s, run = handler(tmp_path)
    out = run(name="report-in-ml", description="Reports mL. Use when measuring.",
              instructions="One decimal.", args="oops", another="also ignored")
    assert "report-in-ml" in out and "next message" in out


def test_save_skill_without_a_writable_store_explains():
    """No ``use_store()`` call: ``save_doc`` raises ``SkillValidationError``, not a store
    error -- the handler must still report it instead of letting it propagate."""
    s = skills()
    run = save_skill_handler(s)
    out = run(name="report-in-ml", description="Reports mL. Use when measuring.",
              instructions="x")
    assert out == "Not saved: this app keeps no skills of its own."


def test_builtin_rides_with_the_host_schemas():
    sent = []

    class Client:
        def request(self, path, method="GET", body=None):
            sent.append([t["name"] for t in body["tools"]])
            return {"registered": [], "count": 0}

    tools = T2VTools(Client())
    tools.add_builtin(SAVE_SKILL_SCHEMA, lambda **kwargs: "ok", permission=True)
    tools.register([ClientToolSchema(name="load_sample_data", description="d")])
    tools.re_register()
    assert sent == [["load_sample_data", "save_skill"], ["load_sample_data", "save_skill"]]
    assert (
        tools.validate_args("save_skill", {"name": "a"}) == "Missing required argument: description"
    )


def test_builtin_added_after_register_still_appears_on_re_register():
    """A host may call ``register_from_functions``/``register`` BEFORE ``UIServer`` has a
    chance to ``add_builtin`` -- the 3D Slicer extension's own order. The next ``re_register()`` (a
    new session) must still carry the builtin, even though the register call it rode in
    on did not."""
    sent = []

    class Client:
        def request(self, path, method="GET", body=None):
            sent.append([t["name"] for t in body["tools"]])
            return {"registered": [], "count": 0}

    tools = T2VTools(Client())
    tools.register([ClientToolSchema(name="load_sample_data", description="d")])
    tools.add_builtin(SAVE_SKILL_SCHEMA, lambda **kwargs: "ok", permission=True)
    tools.re_register()
    assert sent == [["load_sample_data"], ["load_sample_data", "save_skill"]]


# ── save_skill end to end ────────────────────────────────────────────────────────────
#
# End-to-end: save_skill wired the real way (add_builtin) and driven through
# `execute_tool_call`, the same path a live tool call from the engine takes.


def _wired_tools(tmp_path):
    s = skills()
    s.use_store(FolderSkillStore(tmp_path))
    tools = T2VTools(NoopClient())
    tools.add_builtin(SAVE_SKILL_SCHEMA, save_skill_handler(s), permission=True)
    return tools


def test_execute_tool_call_saves_and_reports_success(tmp_path):
    tools = _wired_tools(tmp_path)

    result, is_error = tools.execute_tool_call(
        SAVE_SKILL,
        {
            "name": "report-in-ml",
            "description": "Reports volumes in mL. Use when measuring.",
            "instructions": "Report segment volumes in mL, one decimal.",
        },
    )

    assert is_error is False
    assert "report-in-ml" in result and "next message" in result
    assert (tmp_path / "report-in-ml" / "SKILL.md").is_file()


def test_execute_tool_call_invalid_writes_nothing(tmp_path):
    tools = _wired_tools(tmp_path)

    result, is_error = tools.execute_tool_call(
        SAVE_SKILL, {"name": "Bad Name", "description": "", "instructions": "x"}
    )

    # Reported to the model as text, like any other save_skill failure -- not a tool
    # error: the model can read it and retry.
    assert is_error is False
    assert result.startswith("Not saved.")
    assert list(tmp_path.iterdir()) == []


def test_execute_tool_call_ignores_an_extra_args_key(tmp_path):
    """The exact failure mode a dual-mode ``run(args=None, **kwargs)`` handler would hit:
    a call argument literally named ``args``. Kept as a regression test now that the
    handler is pure ``**kwargs``."""
    tools = _wired_tools(tmp_path)

    result, is_error = tools.execute_tool_call(
        SAVE_SKILL,
        {
            "name": "report-in-ml",
            "description": "Reports mL. Use when measuring.",
            "instructions": "One decimal.",
            "args": "oops",
        },
    )

    assert is_error is False
    assert "report-in-ml" in result and "next message" in result


# ── The result rides the step (result_on_step) ───────────────────────────────────────
#
# The page's settled line ("Saved skill 'x'" / "Not saved") reads the handler's own
# sentence from the call's step. T2VSession puts it on the approval_result event's
# `content`, only for a builtin installed with result_on_step=True; the reducer then
# records it as the step's `result` (tests/test_controller_approvals.py).


def _approve(tools, tool_name, arguments, decision=None):
    from unittest.mock import MagicMock

    from talk2view.sessions import T2VSession
    from talk2view.types import ChatCompletionChunk, ChatCompletionChunkChoice, ChatEvent, Decision

    client = MagicMock()
    client.stream_request.side_effect = [
        iter([ChatCompletionChunk(id="s",
                                  choices=[ChatCompletionChunkChoice(finish_reason="stop")])])
    ]
    session = T2VSession(session_id="s", thread_id="t", model="m", client=client, tools=tools)
    approval = ChatEvent(type="approval_required", kind="client", tool_name=tool_name,
                         tool_call_id="tc1", arguments=arguments)
    events = list(session.respond_to_approval(approval, decision or Decision("once")))
    return next(e for e in events if e.type == "approval_result")


def _result_tools(tmp_path):
    s = skills()
    s.use_store(FolderSkillStore(tmp_path))
    tools = T2VTools(NoopClient())
    tools.add_builtin(SAVE_SKILL_SCHEMA, save_skill_handler(s), permission=True,
                      allow_always=False, result_on_step=True)
    return tools


ARGS = {"name": "report-in-ml", "description": "Reports volumes in mL. Use when measuring.",
        "instructions": "One decimal."}


def test_result_on_step_is_an_opt_in_a_host_tool_never_has():
    tools = T2VTools(NoopClient())
    tools.handle("delete_volume", lambda **kw: "deleted", permission=True)
    tools.add_builtin(SAVE_SKILL_SCHEMA, lambda **kw: "ok", permission=True, result_on_step=True)
    assert tools.records_result(SAVE_SKILL) is True
    assert tools.records_result("delete_volume") is False
    # Re-installed without it: off again.
    tools.add_builtin(SAVE_SKILL_SCHEMA, lambda **kw: "ok", permission=True)
    assert tools.records_result(SAVE_SKILL) is False


def test_a_host_that_owns_the_name_keeps_no_result_on_its_step():
    tools = T2VTools(NoopClient())
    tools.handle(SAVE_SKILL, lambda **kw: "host's own", permission=True)
    tools.add_builtin(SAVE_SKILL_SCHEMA, lambda **kw: "ok", permission=True, result_on_step=True)
    assert tools.records_result(SAVE_SKILL) is False


def test_an_approved_save_carries_the_saved_sentence(tmp_path):
    event = _approve(_result_tools(tmp_path), SAVE_SKILL, ARGS)
    assert event.decision == "once"
    assert event.content == "Saved skill 'report-in-ml'. It applies from the user's next message."


def test_the_edited_arguments_are_what_is_saved_and_reported(tmp_path):
    from talk2view.types import Decision

    edited = dict(ARGS, name="bad-name")
    event = _approve(_result_tools(tmp_path), SAVE_SKILL, ARGS,
                     Decision("once", updated_input=edited))
    assert event.content is not None and event.content.startswith("Saved skill 'bad-name'")
    assert (tmp_path / "bad-name" / "SKILL.md").is_file()


def test_a_clash_carries_the_not_saved_sentence(tmp_path):
    tools = _result_tools(tmp_path)
    _approve(tools, SAVE_SKILL, ARGS)
    event = _approve(tools, SAVE_SKILL, ARGS)
    assert event.content is not None and event.content.startswith("Not saved.")
    assert "report-in-ml" in event.content


def test_a_denied_save_carries_nothing(tmp_path):
    from talk2view.types import Decision

    event = _approve(_result_tools(tmp_path), SAVE_SKILL, ARGS, Decision("deny", feedback="no"))
    assert event.decision == "deny" and event.content is None
    assert list(tmp_path.iterdir()) == []


def test_the_recorded_result_is_capped(tmp_path):
    from talk2view.tools import STEP_RESULT_MAX

    tools = T2VTools(NoopClient())
    tools.add_builtin(SAVE_SKILL_SCHEMA, lambda **kw: "x" * 5000, permission=True,
                      result_on_step=True)
    event = _approve(tools, SAVE_SKILL, ARGS)
    assert event.content == "x" * STEP_RESULT_MAX


def test_a_host_permission_tool_carries_no_result(tmp_path):
    tools = _result_tools(tmp_path)
    tools.handle("delete_volume", lambda **kw: "deleted CT", permission=True)
    event = _approve(tools, "delete_volume", {"name": "CT"})
    assert event.decision == "once" and event.content is None
