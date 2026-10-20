"""talk2view.ui.router: one worker, every command to exactly one result, fail closed."""

from __future__ import annotations

import logging
import threading
import traceback
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Set

import pytest

from talk2view import Attachment, Decision, Skill, Talk2View
from talk2view.skills import MAX_REGISTER_BODY_BYTES
from talk2view.ui import GenericHost
from talk2view.ui.app_state import _SIGNED_OUT, _UNREADABLE, AppState, _identity
from talk2view.ui.hub import EventHub
from talk2view.ui.router import CommandRouter, UploadRegistry
from talk2view.ui.testing import FakeController
from talk2view.ui.wire import CommandFailure, parse_command

from .ui_engine import AccountEngine
from .ui_helpers import wait_for

#: An upload owner, as ``app_state._identity`` reads a signed-in token store.
ANA = (True, "user-ana")


def inline(job: Callable[[], None]) -> None:
    job()


class RecordingHost(GenericHost):
    supports_clipboard = True

    def __init__(self) -> None:
        self.opened: List[str] = []
        super().__init__(open_url=self.opened.append)
        self.copied: List[str] = []

    def clipboard_set(self, text: str) -> None:
        self.copied.append(text)


class Rig:
    def __init__(self, t2v: Optional[Talk2View], host: Optional[GenericHost] = None) -> None:
        self.controller = FakeController(executor=inline, reply="ok")
        self.host = host or RecordingHost()
        self.hub = EventHub()
        self.app = AppState(t2v, self.host)
        self.app.attach(self.hub)
        self.uploads = UploadRegistry()
        self.skills_listed = 0
        self.router = CommandRouter(
            self.controller, self.app, self.host, self.uploads, on_skills_list=self._listed
        )

    def _listed(self) -> None:
        self.skills_listed += 1

    def run(self, **body: Any) -> Dict[str, Any]:
        return self.router.execute(parse_command(dict(body, id="c")))

    def close(self) -> None:
        self.router.stop()
        self.app.detach()


@pytest.fixture
def t2v(account_engine: AccountEngine) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=account_engine.url())
    yield client
    client.close()


@pytest.fixture
def rig(t2v: Talk2View) -> Iterator[Rig]:
    made = Rig(t2v)
    yield made
    made.close()


@pytest.fixture
def bare() -> Iterator[Rig]:
    made = Rig(None)
    yield made
    made.close()


def ok(result: Dict[str, Any]) -> bool:
    return result["ok"] is True and "error" not in result


def error_type(result: Dict[str, Any]) -> str:
    assert result["ok"] is False, result
    value: str = result["error"]["type"]
    return value


# ── Chat commands ──


def test_send_cancel_reload_edit_new_session_clear_error(rig: Rig) -> None:
    assert rig.run(type="send", text="hello") == {"id": "c", "type": "send", "ok": True}
    assert ok(rig.run(type="reload"))  # the turn is sent again under a new message id
    user_id = rig.controller.snapshot()["messages"][0]["id"]
    assert ok(rig.run(type="edit", messageId=user_id, text="edited"))
    assert error_type(rig.run(type="edit", messageId="nope", text="x")) == "not_found"
    assert ok(rig.run(type="clearError"))
    assert ok(rig.run(type="cancel"))
    assert ok(rig.run(type="newSession"))
    assert [name for name, _ in rig.controller.calls] == [
        "send", "retry", "edit", "edit", "clear_error", "cancel", "clear",
    ]


def test_send_resolves_uploaded_attachment_ids_or_refuses(rig: Rig, t2v: Talk2View) -> None:
    assert ok(rig.run(type="login", email="ana@example.com", password="pw"))
    attachment = Attachment(id="a1", filename="x.png", mime_type="image/png", size_bytes=3)
    rig.uploads.add(attachment, owner=_identity(t2v))
    assert error_type(rig.run(type="send", text="hi", attachmentIds=["a1", "zz"])) == (
        "unknown_attachment"
    )
    assert rig.controller.calls == []  # nothing sent without the attachment the user chose
    assert ok(rig.run(type="send", text="", attachmentIds=["a1"]))
    assert rig.controller.calls == [("send", ("", [attachment]))]


def test_send_passes_each_attachment_once(rig: Rig, t2v: Talk2View) -> None:
    assert ok(rig.run(type="login", email="ana@example.com", password="pw"))
    first = Attachment(id="a1", filename="x.png", mime_type="image/png", size_bytes=3)
    second = Attachment(id="a2", filename="y.png", mime_type="image/png", size_bytes=4)
    rig.uploads.add(first, owner=_identity(t2v))
    rig.uploads.add(second, owner=_identity(t2v))
    assert ok(rig.run(type="send", text="hi", attachmentIds=["a2", "a1", "a2", "a1"]))
    assert rig.controller.calls == [("send", ("hi", [second, first]))]


def test_send_resolves_ids_for_whoever_is_signed_in_when_it_runs(
    rig: Rig, t2v: Talk2View, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ok(rig.run(type="login", email="ana@example.com", password="pw"))
    attachment = Attachment(id="a1", filename="x.png", mime_type="image/png", size_bytes=3)
    rig.uploads.add(attachment, owner=_identity(t2v))
    assert ok(rig.run(type="login", email="bob@example.com", password="pw"))
    assert error_type(rig.run(type="send", text="hi", attachmentIds=["a1"])) == (
        "unknown_attachment"
    )
    assert ok(rig.run(type="logout"))
    assert error_type(rig.run(type="send", text="hi", attachmentIds=["a1"])) == (
        "unknown_attachment"
    )
    assert ok(rig.run(type="login", email="ana@example.com", password="pw"))

    def unreadable() -> Any:
        raise RuntimeError("the token store cannot be read")

    monkeypatch.setattr(t2v.auth, "get_user", unreadable)
    assert error_type(rig.run(type="send", text="hi", attachmentIds=["a1"])) == (
        "unknown_attachment"
    )
    assert rig.controller.calls == []
    monkeypatch.undo()
    assert ok(rig.run(type="send", text="hi", attachmentIds=["a1"]))


#: A page string that must never reach a result's message raw: a newline (it could forge a
#: log line), a bidi override, and more than MAX_ECHO_CHARS.
UNSAFE_ID = "x\ny\u202ez" + "w" * 100


def assert_echoed(message: str) -> None:
    assert "\n" not in message and "\u202e" not in message
    assert "'x\\ny\\u202ez" in message and "w" * 100 not in message and "..." in message


def test_unknown_attachment_and_model_echo_the_page_string_escaped(
    rig: Rig, t2v: Talk2View
) -> None:
    result = rig.run(type="send", text="hi", attachmentIds=[UNSAFE_ID])
    assert error_type(result) == "unknown_attachment"
    assert_echoed(result["error"]["message"])
    result = rig.run(type="selectModel", modelId=UNSAFE_ID)
    assert error_type(result) == "unknown_model"
    assert_echoed(result["error"]["message"])
    assert rig.controller.calls == []


def test_approve_passes_the_decision_and_the_card_id(rig: Rig) -> None:
    assert ok(rig.run(type="send", text="/approve go"))
    pending = rig.controller.snapshot()["pendingApproval"]
    assert pending is not None
    assert error_type(rig.run(type="send", text="again")) == "busy"
    result = rig.run(type="approve", toolCallId=pending["toolCallId"], action="always",
                     feedback="fine", updatedInput={"name": "CT"})
    assert ok(result)
    assert rig.controller.calls[-1] == (
        "approve",
        (Decision("always", feedback="fine", updated_input={"name": "CT"}), pending["toolCallId"]),
    )


def test_a_closed_controller_is_reported_not_raised(rig: Rig) -> None:
    rig.controller.close()
    assert error_type(rig.run(type="send", text="x")) == "closed"


def test_an_unexpected_exception_is_internal_error_and_logged(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.controller.raise_on["cancel"] = RuntimeError("boom with a secret")
    with caplog.at_level(logging.ERROR, logger="talk2view.ui"):
        result = rig.run(type="cancel")
    assert result["error"] == {"type": "internal_error", "message": "Something went wrong."}
    assert "command cancel failed" in caplog.text


# ── Account commands ──


def test_login_logout_and_signup_confirmation(
    rig: Rig, t2v: Talk2View, account_engine: AccountEngine
) -> None:
    assert ok(rig.run(type="login", email="ana@example.com", password="pw"))
    assert t2v.auth.is_authenticated()
    assert ok(rig.run(type="logout"))
    assert not t2v.auth.is_authenticated()
    # A plain signup on the fake engine answers 401: the engine's type is passed through.
    assert error_type(rig.run(type="signup", email="new@example.com", password="pw")) == (
        "authentication_error"
    )
    # From a guest session signup converts it. A 409 (the email has an account) signs in.
    assert ok(rig.run(type="startAnonymous"))
    assert rig.run(type="signup", email="ana@example.com", password="pw") == {
        "id": "c", "type": "signup", "ok": True, "data": {"confirmationRequired": False},
    }
    assert t2v.auth.is_authenticated() and not t2v.auth.is_anonymous()
    # Accepted: the confirmation link is emailed and the session stays a guest's meanwhile.
    assert ok(rig.run(type="logout"))
    assert ok(rig.run(type="startAnonymous"))
    account_engine.convert_status = 200
    assert rig.run(type="signup", email="new@example.com", password="pw") == {
        "id": "c", "type": "signup", "ok": True, "data": {"confirmationRequired": True},
    }
    assert t2v.auth.is_anonymous()


def test_start_anonymous_and_warm_up(rig: Rig, t2v: Talk2View) -> None:
    assert ok(rig.run(type="startAnonymous"))
    assert t2v.auth.is_anonymous()
    assert ok(rig.run(type="warmUp"))


def test_start_anonymous_sends_the_captcha_and_passes_a_refusal_through(
    rig: Rig, t2v: Talk2View, account_engine: AccountEngine
) -> None:
    account_engine.anonymous_refusal = (403, "captcha_required")
    assert error_type(rig.run(type="startAnonymous")) == "captcha_required"
    assert not t2v.auth.is_authenticated()
    account_engine.anonymous_refusal = None
    assert ok(rig.run(type="startAnonymous", captchaToken="turnstile-xyz"))
    assert auth_bodies(account_engine, "/v1/auth/anonymous") == [
        {}, {"captcha_token": "turnstile-xyz"},
    ]


def auth_bodies(engine: AccountEngine, path: str) -> List[Dict[str, Any]]:
    return [body for at, body in engine.auth_bodies if at == path]


def test_credentials_reach_the_engine_revealed(
    rig: Rig, t2v: Talk2View, account_engine: AccountEngine
) -> None:
    assert ok(rig.run(type="login", email="ana@example.com", password="hunter2-secret"))
    assert t2v.auth.is_authenticated()
    assert auth_bodies(account_engine, "/v1/auth/login") == [
        {"email": "ana@example.com", "password": "hunter2-secret"},
    ]
    assert ok(rig.run(type="logout"))
    assert ok(rig.run(type="startAnonymous"))
    assert ok(rig.run(type="signup", email="ana@example.com", password="convert-secret"))
    (convert,) = auth_bodies(account_engine, "/v1/auth/convert")
    assert {key: convert[key] for key in ("email", "password")} == {
        "email": "ana@example.com", "password": "convert-secret",
    }


def test_select_model_and_font_size(rig: Rig, t2v: Talk2View) -> None:
    assert error_type(rig.run(type="selectModel", modelId="m-deep")) == "unknown_model"
    rig.run(type="login", email="ana@example.com", password="pw")
    assert wait_for(lambda: rig.hub.snapshot()["state"]["models"]["list"] != [])
    assert ok(rig.run(type="selectModel", modelId="m-deep"))
    assert t2v.preferences.get().model == "m-deep"
    assert ok(rig.run(type="setFontSize", fontSize="small"))
    assert t2v.preferences.get().font_size == "small"
    assert ok(rig.run(type="setFontSize", fontSize=None))
    assert t2v.preferences.get().font_size is None


def test_oauth_and_dictation_are_unavailable(rig: Rig) -> None:
    assert error_type(rig.run(type="oauthStart", provider="google")) == "oauth_unavailable"
    assert error_type(rig.run(type="dictationStart")) == "dictation_unavailable"
    assert error_type(rig.run(type="dictationStop")) == "dictation_unavailable"


def test_account_commands_without_a_talk2view(bare: Rig) -> None:
    for body in (
        {"type": "login", "email": "a@b.c", "password": "pw"},
        {"type": "logout"},
        {"type": "startAnonymous"},
        {"type": "warmUp"},
        {"type": "setFontSize", "fontSize": "small"},
        {"type": "skillsList"},
        {"type": "skillEnable", "name": "x"},
    ):
        assert error_type(bare.run(**body)) == "account_unavailable", body
    assert error_type(bare.run(type="selectModel", modelId="m")) == "unknown_model"


def test_passwords_never_reach_a_result_or_the_log(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        result = rig.run(type="signup", email="new@example.com", password="hunter2-secret")
    assert "hunter2-secret" not in repr(result)
    assert "hunter2-secret" not in caplog.text
    # Nor the command itself, which is a local in every frame of the router's tracebacks,
    # nor its arguments (the handlers' ``args``).
    command = parse_command({"type": "login", "email": "a@b.c", "password": "hunter2-secret"})
    assert "hunter2-secret" not in repr(command)
    assert "hunter2-secret" not in repr(command.args)
    assert str(command.args["password"]) == "Secret(***)"


def test_a_traceback_with_locals_from_inside_login_holds_no_password(
    rig: Rig, t2v: Talk2View, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A builtin raises without a Python frame of its own, so the traceback ends in the
    # router's _login, after the password has been revealed into the call: exactly what a
    # tool that captures every frame's locals (Sentry, rich, pytest -l) would record.
    monkeypatch.setattr(t2v.auth, "login", divmod)
    with caplog.at_level(logging.ERROR, logger="talk2view.ui"):
        result = rig.run(type="login", email="a@b.c", password="hunter2-secret")
    assert result["error"]["type"] == "internal_error"
    (record,) = [r for r in caplog.records if r.exc_info]
    assert record.exc_info is not None
    formatted = "".join(
        traceback.TracebackException(*record.exc_info, capture_locals=True).format()
    )
    assert "in _login" in formatted and "Secret(***)" in formatted  # the locals are there
    assert "hunter2-secret" not in formatted


# ── Host commands ──


def test_copy_and_open_link_are_posted_to_the_host(rig: Rig) -> None:
    host = rig.host
    assert isinstance(host, RecordingHost)
    assert ok(rig.run(type="copy", text="the answer"))
    assert ok(rig.run(type="openLink", url="https://slicer.org/docs?q=1"))
    assert host.copied == [] and host.opened == []  # posted, not run on this thread
    host.pump()
    assert host.copied == ["the answer"]
    assert host.opened == ["https://slicer.org/docs?q=1"]


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,x",
        "https://",
        "http://exa mple.com",
        "http://example.com/\n",
        "ftp://example.com",
        "http://[::1",  # urlsplit raises: still a refusal, not an internal error
        # A user part makes a link show one site and open another.
        "https://google.com@evil.com",
        "http://user:pw@example.com",
        "http://@",
        # No host.
        "http://:80",
        "https://?q=1",
        # A bracketed host that is not an IPv6 address, or a port that is not one.
        "http://[v1.x]",
        "http://example.com:99999",
        "http://example.com:http",
        # Characters that change how the link reads: bidi and format controls, C1
        # controls, other spaces, line and paragraph separators.
        "https://example.com/\u202eevil",
        "https://example.com/\u2066x",
        "https://exa\u200bmple.com",
        "https://example.com/\x85",
        "https://example.com/\x9b",
        "https://example.com/\u00a0",
        "https://example.com/\u2028",
    ],
)
def test_open_link_refuses_anything_but_http_and_https(rig: Rig, url: str) -> None:
    assert error_type(rig.run(type="openLink", url=url)) == "invalid_url"


@pytest.mark.parametrize(
    "url",
    [
        "HTTP://EXAMPLE.COM",
        "https://exämple.com/ü?q=1#frag",
        "https://xn--exmple-cua.com",
        "http://127.0.0.1:5173/docs",
        "https://[::1]:8080/",
        "https://example.com/%E2%80%AEencoded-is-fine",
    ],
)
def test_open_link_accepts_ordinary_links(rig: Rig, url: str) -> None:
    host = rig.host
    assert isinstance(host, RecordingHost)
    assert ok(rig.run(type="openLink", url=url))
    host.pump()
    assert host.opened == [url]


def test_copy_needs_a_host_with_a_clipboard(t2v: Talk2View) -> None:
    plain = Rig(t2v, GenericHost())
    try:
        assert error_type(plain.run(type="copy", text="x")) == "clipboard_unavailable"
    finally:
        plain.close()


# ── Skills ──


def test_skills_list_rescans_then_reads_the_catalogue(rig: Rig, t2v: Talk2View) -> None:
    rig.run(type="login", email="ana@example.com", password="pw")
    assert ok(rig.run(type="skillsList"))  # the fake engine has no GET /v1/skills: None
    assert rig.skills_listed == 1


def test_skill_enable_and_disable(rig: Rig, t2v: Talk2View) -> None:
    t2v.skills.add(Skill(name="measure", description="d", content="body"))
    assert ok(rig.run(type="skillDisable", name="measure"))
    assert t2v.skills.state()["local"][0]["enabled"] is False
    assert ok(rig.run(type="skillEnable", name="measure"))
    assert t2v.skills.state()["local"][0]["enabled"] is True
    assert error_type(rig.run(type="skillEnable", name="ghost")) == "unknown_skill"


def test_enabling_a_skill_over_the_register_cap_is_skill_too_large(
    rig: Rig, t2v: Talk2View
) -> None:
    half = "a" * (MAX_REGISTER_BODY_BYTES // 2)
    t2v.skills.add(Skill(name="one", description="d", content=half))
    assert ok(rig.run(type="skillDisable", name="one"))
    t2v.skills.add(Skill(name="two", description="d", content=half))
    assert error_type(rig.run(type="skillEnable", name="one")) == "skill_too_large"
    enabled = {s["name"]: s["enabled"] for s in t2v.skills.state()["local"]}
    assert enabled == {"one": False, "two": True}  # the refusal changed nothing


# ── The worker ──


def test_the_worker_runs_commands_in_order_on_its_own_thread_and_records_results(
    rig: Rig,
) -> None:
    threads: List[str] = []
    original = rig.controller.cancel

    def cancel() -> None:
        threads.append(threading.current_thread().name)
        original()

    rig.controller.cancel = cancel  # type: ignore[method-assign]
    rig.router.start()
    for i in range(5):
        assert rig.router.submit(parse_command({"type": "cancel", "id": str(i)})) is None
    assert wait_for(lambda: len(rig.hub.snapshot()["state"]["results"]) == 5)
    assert [r["id"] for r in rig.hub.snapshot()["state"]["results"]] == ["0", "1", "2", "3", "4"]
    assert set(threads) == {"t2v-ui-router"}


def test_a_full_queue_refuses_and_a_stopped_router_refuses(t2v: Talk2View) -> None:
    rig = Rig(t2v)
    rig.router = CommandRouter(rig.controller, rig.app, rig.host, rig.uploads, max_queued=2)
    gate = threading.Event()
    rig.router.submit_job(gate.wait)  # before start: the worker will sit on it
    rig.router.start()
    try:
        cancel = parse_command({"type": "cancel"})
        assert rig.router.submit(cancel) is None and rig.router.submit(cancel) is None
        assert rig.router.submit(cancel) == "queue_full"
    finally:
        gate.set()
        rig.close()
    assert rig.router.submit(parse_command({"type": "cancel"})) == "stopped"


def test_a_failing_job_does_not_kill_the_worker(rig: Rig) -> None:
    rig.router.start()

    def boom() -> None:
        raise RuntimeError("job bug")

    rig.router.submit_job(boom)
    assert rig.router.submit(parse_command({"type": "cancel", "id": "after"})) is None
    assert wait_for(lambda: rig.hub.snapshot()["state"]["results"] != [])


def router_threads() -> Set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name == "t2v-ui-router" and t.is_alive()}


def test_a_router_starts_once_and_stops_idempotently(rig: Rig) -> None:
    before = router_threads()
    rig.router.start()
    with pytest.raises(RuntimeError):
        rig.router.start()
    worker = router_threads() - before
    assert len(worker) == 1
    rig.router.stop()
    rig.router.stop()
    assert wait_for(lambda: not any(t.is_alive() for t in worker))
    assert rig.router.submit(parse_command({"type": "cancel"})) == "stopped"
    with pytest.raises(RuntimeError):
        rig.router.start()


def test_a_router_not_started_yet_refuses_as_stopped(rig: Rig) -> None:
    assert rig.router.submit(parse_command({"type": "cancel"})) == "stopped"
    assert rig.controller.calls == []


def test_a_router_stopped_before_it_started_accepts_nothing(rig: Rig) -> None:
    rig.router.stop()
    assert rig.router.submit(parse_command({"type": "cancel"})) == "stopped"
    with pytest.raises(RuntimeError):
        rig.router.start()
    assert rig.router.submit(parse_command({"type": "cancel"})) == "stopped"


def test_a_base_exception_from_host_code_fails_the_command_and_stops_the_router(
    t2v: Talk2View, caplog: pytest.LogCaptureFixture
) -> None:
    def interrupted() -> None:
        raise KeyboardInterrupt

    rig = Rig(t2v)
    rig.router = CommandRouter(
        rig.controller, rig.app, rig.host, rig.uploads, on_skills_list=interrupted
    )
    gate = threading.Event()
    rig.router.submit_job(gate.wait)  # holds the worker until both commands are queued
    before = router_threads()
    rig.router.start()
    worker = router_threads() - before
    try:
        with caplog.at_level(logging.ERROR, logger="talk2view.ui"):
            assert rig.router.submit(parse_command({"type": "skillsList", "id": "k"})) is None
            assert rig.router.submit(parse_command({"type": "cancel", "id": "behind"})) is None
            gate.set()
            assert wait_for(lambda: len(rig.hub.snapshot()["state"]["results"]) == 2)
            assert wait_for(lambda: not any(t.is_alive() for t in worker))
        failed, behind = rig.hub.snapshot()["state"]["results"]
        assert failed == {
            "id": "k", "type": "skillsList", "ok": False,
            "error": {"type": "internal_error", "message": "Something went wrong."},
        }
        assert behind["id"] == "behind" and behind["error"]["type"] == "closed"
        assert rig.controller.calls == []  # the command behind it was never run
        assert rig.router.submit(parse_command({"type": "cancel"})) == "stopped"  # not full
        assert "KeyboardInterrupt" in caplog.text
    finally:
        gate.set()
        rig.close()


def test_a_base_exception_from_a_job_stops_the_router(rig: Rig) -> None:
    def exits() -> None:
        raise SystemExit(3)

    gate = threading.Event()
    rig.router.submit_job(gate.wait)
    rig.router.submit_job(exits)
    rig.router.start()
    try:
        assert rig.router.submit(parse_command({"type": "cancel", "id": "behind"})) is None
    finally:
        gate.set()
    assert wait_for(lambda: rig.hub.snapshot()["state"]["results"] != [])
    assert rig.hub.snapshot()["state"]["results"][0]["error"]["type"] == "closed"
    assert rig.router.submit(parse_command({"type": "cancel"})) == "stopped"


def test_upload_registry_is_bounded_and_ordered() -> None:
    registry = UploadRegistry(capacity=2)
    first, second, third = (
        Attachment(id=n, filename=n, mime_type="image/png", size_bytes=1) for n in "abc"
    )
    registry.add(first, owner=ANA)
    registry.add(second, owner=ANA)
    registry.add(third, owner=ANA)
    assert registry.resolve(["c", "b"], owner=ANA) == [third, second]
    with pytest.raises(CommandFailure) as info:
        registry.resolve(["a"], owner=ANA)
    assert info.value.type == "unknown_attachment"
    assert registry.resolve([], owner=ANA) == []


def test_upload_registry_holds_only_real_attachments() -> None:
    # The controller starts a run before it reads an attachment's fields: anything but an
    # Attachment reaching send() could leave it busy for good, so it never gets in.
    registry = UploadRegistry()
    for fake in (
        SimpleNamespace(id="a1"),  # has an id, so the registry could file it
        {"id": "a1", "filename": "x.png", "mime_type": "image/png", "size_bytes": 3},
    ):
        with pytest.raises(TypeError):
            registry.add(fake, owner=ANA)  # type: ignore[arg-type]
    with pytest.raises(CommandFailure):
        registry.resolve(["a1"], owner=ANA)


def refusal(registry: UploadRegistry, ids: List[str], owner: Any) -> CommandFailure:
    with pytest.raises(CommandFailure) as info:
        registry.resolve(ids, owner=owner)
    return info.value


def test_upload_registry_gives_each_attachment_to_its_owner_only() -> None:
    registry = UploadRegistry()
    mine = Attachment(id="a1", filename="x.png", mime_type="image/png", size_bytes=3)
    registry.add(mine, owner=ANA)
    assert registry.resolve(["a1"], owner=(True, "user-ana")) == [mine]  # equal, not same
    missing = refusal(registry, ["a2"], ANA)
    # Another identity, nobody, or an identity that cannot be read: refused exactly as an
    # id that was never uploaded, so the page learns nothing of anyone else's.
    for owner in ((True, "user-bob"), (True, None), _SIGNED_OUT, _UNREADABLE):
        failure = refusal(registry, ["a1"], owner)
        assert (failure.type, failure.message) == (
            "unknown_attachment", missing.message.replace("a2", "a1")
        )
    # No signed-in user owns anything: not an unreadable store, not nobody, and not a token
    # whose stored user is missing or garbled ((True, None), which two identities could
    # share). Even filed under such an owner by mistake, it resolves for nobody.
    for nobody in (_UNREADABLE, _SIGNED_OUT, (True, None), (True, "")):
        registry.add(mine, owner=nobody)
        assert refusal(registry, ["a1"], nobody).type == "unknown_attachment", nobody
