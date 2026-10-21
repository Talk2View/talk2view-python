"""ChatOptions and ToolPresentation: the integrator's options for the chat page,
checked when built, served read-only as the ``options`` and ``tools`` slices, and enforced
by the server where they fail closed."""

from __future__ import annotations

import copy
import dataclasses
import json
import pickle
from typing import Any, Callable, Dict, Iterator, List, Mapping, Tuple

import pytest

from talk2view import Attachment, ChatController, Talk2View
from talk2view.ui import (
    AppState,
    ChatFeatures,
    ChatOptions,
    ChatWelcome,
    GenericHost,
    ToolPresentation,
    UIServer,
)
from talk2view.ui import options as ui_options
from talk2view.ui import router as ui_router
from talk2view.ui.app_state import APP_KEYS, _identity
from talk2view.ui.hub import EventHub
from talk2view.ui.router import CommandRouter, UploadRegistry
from talk2view.ui.testing import FakeController
from talk2view.ui.wire import ERROR_TYPES, parse_command

from .test_ui_server import STUB, Rig, declared_only, inline, raw, status_and_json, upload_body
from .ui_engine import AccountEngine

DEFAULTS = {
    "welcome": {"heading": None, "subheading": None, "suggestions": []},
    "features": {
        "dictation": True,
        "attachments": True,
        "settings": True,
        "account": True,
        "threadList": True,
        "providerSignIn": True,
        "skills": True,
    },
    "allowAnonymous": True,
    "resetPasswordUrl": None,
    "footer": None,
    "appName": None,
}
NO_ATTACHMENTS = ChatOptions(features=ChatFeatures(attachments=False))


@pytest.fixture
def t2v(account_engine: AccountEngine) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=account_engine.url())
    yield client
    client.close()


def routed(t2v: Talk2View, options: ChatOptions) -> Tuple[FakeController, AppState, Any, Any]:
    controller = FakeController(executor=inline, reply="ok")
    host = GenericHost()
    app = AppState(t2v, host, options=options)
    app.attach(EventHub())
    uploads = UploadRegistry()
    return controller, app, uploads, CommandRouter(controller, app, host, uploads)


def run(router: CommandRouter, **body: Any) -> Dict[str, Any]:
    return router.execute(parse_command(dict(body, id="c")))


class LyingText(str):
    """A ``str`` whose own methods tell another story than its characters do."""

    def __iter__(self) -> Iterator[str]:
        return iter("https://example.com/")

    def lstrip(self, chars: Any = None) -> str:
        return "https://example.com/"

    def strip(self, chars: Any = None) -> str:
        return "fine"


class LyingMap(Mapping[str, ToolPresentation]):
    """Says it holds one tool, and yields ``count``."""

    def __init__(self, count: int) -> None:
        self._count = count

    def __getitem__(self, name: str) -> ToolPresentation:
        return ToolPresentation()

    def __iter__(self) -> Iterator[str]:
        return iter(["t{}".format(i) for i in range(self._count)])

    def __len__(self) -> int:
        return 1


class ShiftingList(List[Any]):
    """Yields its items, then swaps its first for a number: a second pass reads ``5``."""

    def __iter__(self) -> Iterator[Any]:
        items = list(super().__iter__())
        yield from items
        self[0] = 5


# ── The options themselves ──


def test_the_defaults_are_the_typescript_chats() -> None:
    options = ChatOptions()
    assert options.to_wire() == DEFAULTS
    assert options.tools_to_wire() == {}


def test_app_name_is_trimmed() -> None:
    assert ChatOptions(app_name="  Talk2Slicer ").to_wire()["appName"] == "Talk2Slicer"


def test_every_option_reaches_the_wire_in_ts_casing() -> None:
    options = ChatOptions(
        welcome=ChatWelcome(
            heading="Ask about your scene", subheading="Load data by talking.",
            suggestions=["Load MRHead", "Measure"],
        ),
        features=ChatFeatures(dictation=False, attachments=False, thread_list=False),
        allow_anonymous=False,
        reset_password_url="https://example.com/reset?from=slicer",
        footer="Answers can be wrong.",
        app_name="Talk2Slicer",
        tools={
            "load_file": ToolPresentation(activity="Loading a file", destructive=True),
            "threshold_segment": ToolPresentation(destructive=True, warning="Replaces it."),
            "get_scene_context": ToolPresentation(activity="Reading the scene"),
        },
    )
    assert options.to_wire() == {
        "welcome": {"heading": "Ask about your scene", "subheading": "Load data by talking.",
                    "suggestions": ["Load MRHead", "Measure"]},
        "features": {
            "dictation": False,
            "attachments": False,
            "settings": True,
            "account": True,
            "threadList": False,
            "providerSignIn": True,
            "skills": True,
        },
        "allowAnonymous": False,
        "resetPasswordUrl": "https://example.com/reset?from=slicer",
        "footer": "Answers can be wrong.",
        "appName": "Talk2Slicer",
    }
    assert options.tools_to_wire() == {
        "load_file": {"activity": "Loading a file", "destructive": True, "warning": None},
        "threshold_segment": {"activity": None, "destructive": True, "warning": "Replaces it."},
        "get_scene_context": {"activity": "Reading the scene", "destructive": False,
                              "warning": None},
    }
    json.dumps([options.to_wire(), options.tools_to_wire()], allow_nan=False)


@pytest.mark.parametrize(
    "build, error",
    [
        (lambda: ChatWelcome(heading=""), ValueError),
        (lambda: ChatWelcome(heading="x" * 201), ValueError),
        (lambda: ChatWelcome(heading=5), TypeError),
        (lambda: ChatWelcome(suggestions="Load MRHead"), TypeError),
        (lambda: ChatWelcome(suggestions=["ok", "  "]), ValueError),
        (lambda: ChatWelcome(suggestions=["s"] * 13), ValueError),
        (lambda: ChatWelcome(suggestions=["\ud800"]), ValueError),
        (lambda: ChatWelcome(subheading=""), ValueError),
        (lambda: ChatWelcome(subheading="x" * (ui_options.MAX_TEXT_CHARS + 1)), ValueError),
        (lambda: ChatWelcome(subheading=5), TypeError),
        (lambda: ChatFeatures(attachments=1), TypeError),
        (lambda: ChatFeatures(thread_list=None), TypeError),
        (lambda: ToolPresentation(activity=""), ValueError),
        (lambda: ToolPresentation(destructive="yes"), TypeError),
        (lambda: ToolPresentation(warning="Careful"), ValueError),  # not destructive
        (lambda: ChatOptions(allow_anonymous=0), TypeError),
        (lambda: ChatOptions(footer="x" * 1001), ValueError),
        (lambda: ChatOptions(app_name=""), ValueError),
        (lambda: ChatOptions(app_name="   "), ValueError),
        (lambda: ChatOptions(app_name="x" * 65), ValueError),
        (lambda: ChatOptions(app_name="Talk\nSlicer"), ValueError),
        (lambda: ChatOptions(app_name="Talk\x07Slicer"), ValueError),
        (lambda: ChatOptions(app_name="\ud800"), ValueError),
        (lambda: ChatOptions(app_name=5), TypeError),
        (lambda: ChatOptions(welcome={"heading": "Hi"}), TypeError),
        (lambda: ChatOptions(features={"attachments": False}), TypeError),
        (lambda: ChatOptions(tools=[("load_file", ToolPresentation())]), TypeError),
        (lambda: ChatOptions(tools={"load file": ToolPresentation()}), ValueError),
        (lambda: ChatOptions(tools={"9lives": ToolPresentation()}), ValueError),
        (lambda: ChatOptions(tools={"t" * 129: ToolPresentation()}), ValueError),
        (lambda: ChatOptions(tools={"load_file\n": ToolPresentation()}), ValueError),
        (lambda: ChatOptions(tools={3: ToolPresentation()}), TypeError),
        (lambda: ChatOptions(tools={"load_file": {"destructive": True}}), TypeError),
        (lambda: ChatOptions(tools={"t{}".format(i): ToolPresentation() for i in range(129)}),
         ValueError),
    ],
)
def test_a_bad_option_is_refused_when_it_is_built(
    build: Callable[[], Any], error: type
) -> None:
    with pytest.raises(error):
        build()


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "https://example.com@evil.example/reset",
        "https://",
        "https://exa mple.com/reset",
        "https://example.com/\u202ereset",
        "https://[not-ipv6]/reset",
        "",
    ],
)
def test_the_reset_password_link_follows_the_open_link_rule(url: str) -> None:
    with pytest.raises(ValueError):
        ChatOptions(reset_password_url=url)
    assert not ui_options._openable(url)


def test_open_link_and_the_reset_link_share_one_rule() -> None:
    assert ui_router._openable is ui_options._openable


def test_a_str_subclass_is_checked_and_kept_as_the_text_it_holds() -> None:
    # Its iteration and lstrip() say https; its characters say javascript.
    with pytest.raises(ValueError):
        ChatOptions(reset_password_url=LyingText("javascript:alert(1)"))
    with pytest.raises(ValueError):
        ChatWelcome(heading=LyingText("   "))  # its strip() says "fine"
    options = ChatOptions(
        welcome=ChatWelcome(heading=LyingText("Hi"), suggestions=[LyingText("Load MRHead")]),
        reset_password_url=LyingText("https://example.com/reset"),
        footer=LyingText("Terms apply."),
        tools={LyingText("load_file"): ToolPresentation(
            activity=LyingText("Loading"), destructive=True, warning=LyingText("Careful"))},
    )
    presentation = options.tools["load_file"]
    kept = [
        options.welcome.heading, *options.welcome.suggestions, options.reset_password_url,
        options.footer, *options.tools, presentation.activity, presentation.warning,
    ]
    assert [type(value) for value in kept] == [str] * 7


def test_the_tools_are_counted_by_what_the_mapping_yields_not_by_its_len() -> None:
    with pytest.raises(ValueError, match="at most {}".format(ui_options.MAX_TOOLS)):
        ChatOptions(tools=LyingMap(ui_options.MAX_TOOLS + 1))
    assert list(ChatOptions(tools=LyingMap(3)).tools) == ["t0", "t1", "t2"]


def test_the_suggestions_kept_are_the_ones_checked() -> None:
    welcome = ChatWelcome(suggestions=ShiftingList(["Load MRHead"]))
    assert welcome.suggestions == ("Load MRHead",)
    assert type(welcome.suggestions[0]) is str


def test_the_tools_mapping_is_copied_and_read_only() -> None:
    tools = {"load_file": ToolPresentation(destructive=True)}
    options = ChatOptions(tools=tools)
    tools["delete_everything"] = ToolPresentation(destructive=True)
    assert list(options.tools) == ["load_file"]
    with pytest.raises(TypeError):
        options.tools["x"] = ToolPresentation()  # type: ignore[index]


def test_the_options_copy_pickle_and_hash_like_any_frozen_value() -> None:
    options = ChatOptions(
        welcome=ChatWelcome(heading="Hi", suggestions=["Load MRHead"]),
        features=ChatFeatures(attachments=False),
        allow_anonymous=False,
        reset_password_url="https://example.com/reset",
        footer="Terms apply.",
        tools={
            "load_file": ToolPresentation(activity="Loading", destructive=True, warning="Careful"),
            "get_scene_context": ToolPresentation(),
        },
    )
    copies = [copy.copy(options), copy.deepcopy(options)] + [
        pickle.loads(pickle.dumps(options, protocol))  # noqa: S301 — bytes this test made
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1)
    ]
    for copied in copies:
        assert copied == options and hash(copied) == hash(options)
        assert copied.to_wire() == options.to_wire()
        assert copied.tools_to_wire() == options.tools_to_wire()
        with pytest.raises(TypeError):
            copied.tools["x"] = ToolPresentation()  # type: ignore[index]
    # Equal options hash equal whatever order the tools were given in.
    reordered = dataclasses.replace(options, tools=dict(reversed(list(options.tools.items()))))
    assert reordered == options and hash(reordered) == hash(options)
    assert {options: "kept"}[reordered] == "kept"
    assert dataclasses.asdict(options)["welcome"] == {
        "heading": "Hi", "subheading": None, "suggestions": ("Load MRHead",)
    }


# ── AppState: the two slices, and the guest rule ──


def test_app_state_serves_both_slices(t2v: Talk2View) -> None:
    options = ChatOptions(footer="Terms apply.", tools={"load_file": ToolPresentation(
        destructive=True)})
    hub = EventHub()
    app = AppState(t2v, GenericHost(), options=options)
    app.attach(hub)
    try:
        state = hub.snapshot()["state"]
        assert state["options"] == dict(DEFAULTS, footer="Terms apply.")
        assert state["tools"] == {
            "load_file": {"activity": None, "destructive": True, "warning": None}
        }
        assert {"options", "tools"} <= set(APP_KEYS)
        assert app.options is options
    finally:
        app.detach()


def test_app_state_defaults_its_options_and_refuses_anything_else() -> None:
    assert AppState(None, GenericHost()).options == ChatOptions()
    with pytest.raises(TypeError):
        AppState(None, GenericHost(), options={"allow_anonymous": False})  # type: ignore[arg-type]


def test_no_guests_needs_a_talk2view_without_auto_start(account_engine: AccountEngine) -> None:
    auto = Talk2View("pk_test", base_url=account_engine.url(), anonymous_auto_start=True)
    plain = Talk2View("pk_test", base_url=account_engine.url())
    try:
        assert auto.anonymous_auto_start is True and plain.anonymous_auto_start is False
        with pytest.raises(ValueError, match="anonymous_auto_start"):
            AppState(auto, GenericHost(), options=ChatOptions(allow_anonymous=False))
        AppState(plain, GenericHost(), options=ChatOptions(allow_anonymous=False))
        AppState(auto, GenericHost(), options=ChatOptions())
    finally:
        auto.close()
        plain.close()


# ── The router enforces the two that fail closed ──


def test_attachments_off_refuses_a_send_that_names_one(t2v: Talk2View) -> None:
    t2v.auth.login("ana@example.com", "pw")
    controller, app, uploads, router = routed(t2v, NO_ATTACHMENTS)
    try:
        attachment = Attachment(id="a1", filename="x.png", mime_type="image/png", size_bytes=3)
        uploads.add(attachment, owner=_identity(t2v))
        result = run(router, type="send", text="look", attachmentIds=["a1"])
        assert result["error"] == {
            "type": "attachments_disabled",
            "message": "Attachments are turned off in this app.",
        }
        assert controller.calls == []
        assert run(router, type="send", text="just text")["ok"] is True
    finally:
        router.stop()
        app.detach()


def test_attachments_off_refuses_an_id_before_looking_it_up(t2v: Talk2View) -> None:
    # Never uploaded: the refusal is attachments_disabled, not unknown_attachment, so the
    # page learns the option before anything about the registry.
    controller, app, _uploads, router = routed(t2v, NO_ATTACHMENTS)
    try:
        result = run(router, type="send", text="look", attachmentIds=["never-uploaded"])
        assert result["error"]["type"] == "attachments_disabled"
        assert controller.calls == []
    finally:
        router.stop()
        app.detach()


def test_no_guests_refuses_start_anonymous(t2v: Talk2View, account_engine: AccountEngine) -> None:
    _controller, app, _uploads, router = routed(t2v, ChatOptions(allow_anonymous=False))
    try:
        assert run(router, type="startAnonymous")["error"] == {
            "type": "anonymous_not_allowed",
            "message": "Guest chat is turned off in this app. Sign in to continue.",
        }
        assert not t2v.auth.is_authenticated()
        assert [p for p, _ in account_engine.auth_bodies] == []
    finally:
        router.stop()
        app.detach()


def test_by_default_a_guest_may_start(t2v: Talk2View) -> None:
    _controller, app, _uploads, router = routed(t2v, ChatOptions())
    try:
        assert run(router, type="startAnonymous")["ok"] is True
        assert t2v.auth.is_anonymous()
    finally:
        router.stop()
        app.detach()


def test_the_new_error_types_are_on_the_wire() -> None:
    assert {"attachments_disabled", "anonymous_not_allowed"} <= ERROR_TYPES


# ── The server ──


def test_the_server_serves_the_options_and_tools_slices() -> None:
    options = ChatOptions(
        welcome=ChatWelcome(heading="Hi"), tools={"load_file": ToolPresentation(destructive=True)}
    )
    controller, host = FakeController(executor=inline), GenericHost()
    rig = Rig(UIServer(controller, host, static_dir=STUB, options=options), controller, host)
    try:
        state = rig.server._hub.snapshot()["state"]
        assert state["options"]["welcome"] == {
            "heading": "Hi", "subheading": None, "suggestions": [],
        }
        assert state["tools"]["load_file"]["destructive"] is True
    finally:
        rig.server.stop()


def test_options_belong_to_the_app_state_a_host_gives(t2v: Talk2View) -> None:
    app = AppState(t2v, GenericHost())
    with pytest.raises(ValueError, match="AppState"):
        UIServer(FakeController(), GenericHost(), app=app, static_dir=STUB, options=ChatOptions())


def test_no_guests_refuses_a_controller_whose_talk2view_would_start_one(
    account_engine: AccountEngine,
) -> None:
    auto = Talk2View("pk_test", base_url=account_engine.url(), anonymous_auto_start=True)
    controller = ChatController(auto)
    host = GenericHost()
    no_guests = ChatOptions(allow_anonymous=False)
    try:
        # The app has no Talk2View of its own, so only the server sees the controller's: a
        # send would otherwise start a guest session behind a page that says "sign in".
        with pytest.raises(ValueError, match="anonymous_auto_start"):
            UIServer(controller, host, app=AppState(None, host, options=no_guests),
                     static_dir=STUB)
        with pytest.raises(ValueError, match="anonymous_auto_start"):
            UIServer(controller, host, static_dir=STUB, options=no_guests)
        assert account_engine.auth_bodies == []
        assert not auto.auth.is_authenticated()
    finally:
        controller.close()
        auto.close()


def test_the_app_and_the_controller_share_one_talk2view(account_engine: AccountEngine) -> None:
    one = Talk2View("pk_test", base_url=account_engine.url())
    other = Talk2View("pk_test", base_url=account_engine.url())
    controller = ChatController(one)
    host = GenericHost()
    try:
        with pytest.raises(ValueError, match="one Talk2View"):
            UIServer(controller, host, app=AppState(other, host), static_dir=STUB)
        server = UIServer(controller, host, app=AppState(one, host), static_dir=STUB)
        server.stop()
    finally:
        controller.close()
        one.close()
        other.close()


def test_a_controller_without_a_talk2view_may_serve_an_app_with_one(t2v: Talk2View) -> None:
    controller, host = FakeController(executor=inline), GenericHost()
    app = AppState(t2v, host, options=ChatOptions(allow_anonymous=False))
    rig = Rig(UIServer(controller, host, app=app, static_dir=STUB), controller, host)
    try:
        assert rig.server._hub.snapshot()["state"]["options"]["allowAnonymous"] is False
    finally:
        rig.server.stop()


def test_attachments_off_refuses_an_upload_before_reading_it() -> None:
    controller, host = FakeController(executor=inline), GenericHost()
    rig = Rig(UIServer(controller, host, static_dir=STUB, options=NO_ATTACHMENTS), controller, host)
    try:
        body, content_type = upload_body()
        # Headers that declare a body that never comes: only a refusal that reads nothing
        # can answer at all.
        reply = declared_only(rig, "/upload", content_type, len(body))
        status, payload = status_and_json(reply)
        assert status == 403 and payload["error"]["type"] == "attachments_disabled"
        assert b"Connection: close" in reply
    finally:
        rig.server.stop()


@pytest.mark.parametrize(
    "framing", ["Transfer-Encoding: chunked\r\n", ""], ids=["chunked", "no-content-length"]
)
def test_attachments_off_refuses_an_upload_before_the_framing_checks(framing: str) -> None:
    """After the gate, the refusal comes first: framing that would be 411 is still 403."""
    controller, host = FakeController(executor=inline), GenericHost()
    rig = Rig(UIServer(controller, host, static_dir=STUB, options=NO_ATTACHMENTS), controller, host)
    try:
        reply = raw(rig.port, (
            "POST /upload HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n"
            "{}\r\n".format(rig.port, rig.token, framing)
        ).encode())
        status, payload = status_and_json(reply)
        assert status == 403 and payload["error"]["type"] == "attachments_disabled"
    finally:
        rig.server.stop()
