"""talk2view.ui.wire: every command the page may send, and everything it may not."""

from __future__ import annotations

import copy
import dataclasses
import pickle
from typing import Any, Dict

import pytest

from talk2view.ui.wire import (
    COMMANDS,
    ERROR_TYPES,
    Command,
    CommandError,
    Secret,
    failure_result,
    http_error,
    parse_command,
    success_result,
)

VALID: Dict[str, Dict[str, Any]] = {
    "send": {"text": "hi", "attachmentIds": ["a1"]},
    "cancel": {},
    "approve": {"toolCallId": "tc-1", "action": "always", "feedback": "ok",
                "updatedInput": {"name": "MRHead"}},
    "reload": {},
    "edit": {"messageId": "m-1", "text": "again"},
    "newSession": {},
    "clearError": {},
    "selectModel": {"modelId": "m-fast"},
    "login": {"email": "a@b.c", "password": "pw"},
    "signup": {"email": "a@b.c", "password": "pw"},
    "logout": {},
    "startAnonymous": {"captchaToken": "turnstile"},
    "oauthStart": {"provider": "google"},
    "oauthCancel": {},
    "oauthDismissError": {},
    "warmUp": {},
    "setFontSize": {"fontSize": "large"},
    "copy": {"text": "x"},
    "openLink": {"url": "https://example.com"},
    "dictationStart": {},
    "dictationStop": {},
    "skillsList": {},
    "skillEnable": {"name": "measure"},
    "skillDisable": {"name": "measure"},
}


def test_the_command_set_is_the_spec_list_plus_five() -> None:
    spec = {
        "send", "cancel", "approve", "reload", "edit", "newSession", "clearError",
        "selectModel", "login", "signup", "logout", "startAnonymous", "oauthStart", "copy",
        "dictationStart", "dictationStop", "skillsList", "skillEnable", "skillDisable",
    }
    assert set(COMMANDS) == spec | {
        "openLink", "setFontSize", "warmUp", "oauthCancel", "oauthDismissError",
    }
    assert set(VALID) == set(COMMANDS)


@pytest.mark.parametrize("kind", sorted(COMMANDS))
def test_every_command_parses_with_its_fields(kind: str) -> None:
    command = parse_command(dict(VALID[kind], type=kind, id="c-1"))
    assert command.type == kind and command.id == "c-1"
    assert set(command.args) == set(COMMANDS[kind])


def test_optional_fields_default_to_none_or_empty() -> None:
    assert parse_command({"type": "send", "text": "hi"}).args == {"text": "hi", "attachmentIds": []}
    assert parse_command({"type": "approve", "toolCallId": "t", "action": "deny"}).args == {
        "toolCallId": "t", "action": "deny", "feedback": None, "updatedInput": None,
    }
    assert parse_command({"type": "startAnonymous"}).args == {"captchaToken": None}
    assert parse_command({"type": "setFontSize", "fontSize": None}).args == {"fontSize": None}
    assert parse_command({"type": "cancel"}).id is None


@pytest.mark.parametrize(
    "body",
    [
        [],
        "send",
        {"text": "hi"},
        {"type": "explode"},
        {"type": 3},
        {"type": "cancel", "id": 7},
        {"type": "cancel", "id": ""},
        {"type": "cancel", "id": "x" * 129},
        {"type": "cancel", "extra": 1},
        {"type": "selectModel", "id": "m-fast"},  # the model is modelId: id is the envelope's
        {"type": "send"},
        {"type": "send", "text": 5},
        {"type": "send", "text": "   "},
        {"type": "send", "text": "hi", "attachmentIds": "a1"},
        {"type": "send", "text": "hi", "attachmentIds": [1]},
        {"type": "send", "text": "hi", "attachmentIds": ["a"] * 11},
        {"type": "approve", "toolCallId": "t", "action": "yes"},
        {"type": "approve", "toolCallId": "t", "action": True},
        {"type": "approve", "toolCallId": "", "action": "once"},
        {"type": "approve", "toolCallId": "t", "action": "once", "updatedInput": []},
        {"type": "edit", "messageId": "m", "text": ""},
        {"type": "login", "email": "a@b.c"},
        {"type": "login", "email": "a@b.c", "password": ""},
        {"type": "setFontSize", "fontSize": "huge"},
        {"type": "setFontSize"},
        {"type": "skillEnable", "name": "x" * 256},
        {"type": "openLink", "url": ""},
    ],
)
def test_anything_else_is_a_bad_command(body: Any) -> None:
    with pytest.raises(CommandError):
        parse_command(body)


def test_send_may_carry_only_an_attachment() -> None:
    assert parse_command({"type": "send", "text": "", "attachmentIds": ["a1"]}).args["text"] == ""


def test_a_command_repr_carries_no_argument_values() -> None:
    # Traceback tools (Sentry's local variables, pytest -l, rich) print a frame's locals
    # with repr: a command's arguments hold passwords, captcha tokens and the user's text.
    for body in (
        {"type": "login", "email": "a@b.c", "password": "hunter2-secret"},
        {"type": "startAnonymous", "captchaToken": "turnstile-secret"},
        {"type": "send", "text": "my-private-question"},
    ):
        command = parse_command(dict(body, id="c-1"))
        shown = repr(command)
        assert "secret" not in shown and "private" not in shown, shown
        assert "c-1" in shown and body["type"] in shown


def test_credentials_are_carried_as_secrets() -> None:
    for kind, name in (("login", "password"), ("signup", "password")):
        command = parse_command({"type": kind, "email": "a@b.c", "password": "hunter2-secret"})
        assert "hunter2-secret" not in repr(command.args)
        assert command.args["email"] == "a@b.c"  # an email is not a credential
        secret = command.args[name]
        assert isinstance(secret, Secret)
        assert secret.reveal() == "hunter2-secret"
    token = parse_command({"type": "startAnonymous", "captchaToken": "turnstile-secret"})
    assert isinstance(token.args["captchaToken"], Secret)
    assert token.args["captchaToken"].reveal() == "turnstile-secret"
    assert "turnstile-secret" not in repr(token.args)
    assert parse_command({"type": "startAnonymous"}).args["captchaToken"] is None


def test_a_secret_never_shows_its_value() -> None:
    secret = Secret("hunter2-secret")
    for shown in (repr(secret), str(secret), "{}".format(secret), "%s %r" % (secret, secret),
                  repr([secret]), repr({"password": secret})):
        assert "hunter2-secret" not in shown, shown
    assert str(secret) == repr(secret) == "Secret(***)"
    # Identity equality only: no comparison can print or probe the value.
    assert secret != Secret("hunter2-secret") and secret == secret
    assert not hasattr(secret, "__dict__")
    with pytest.raises(AttributeError):
        secret._value = "changed"  # type: ignore[misc]
    assert secret.reveal() == "hunter2-secret"


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_a_secret_refuses_pickling_without_showing_its_value(protocol: int) -> None:
    # Pickled bytes (a cache, a queue to another process, a crash dump) would hold the
    # plaintext: a Secret refuses at every protocol, and so does anything that holds one.
    secret = Secret("hunter2-secret")
    command = parse_command({"type": "login", "email": "a@b.c", "password": "hunter2-secret"})
    for value in (secret, command, command.args):
        with pytest.raises(TypeError) as info:
            pickle.dumps(value, protocol)
        assert str(info.value) == "a Secret cannot be pickled"
        assert "hunter2-secret" not in repr(info.value)


def test_a_secret_copies_as_itself() -> None:
    secret = Secret("hunter2-secret")
    assert copy.copy(secret) is secret
    assert copy.deepcopy(secret) is secret
    command = parse_command({"type": "login", "email": "a@b.c", "password": "hunter2-secret"})
    copied = copy.deepcopy(command.args)
    assert copied is not command.args and copied["password"] is command.args["password"]
    assert dataclasses.asdict(command)["args"]["password"] is command.args["password"]


# ``json.loads`` leaves half of a surrogate pair as a lone surrogate, which UTF-8 cannot
# encode. A page can send one (a JavaScript string cut through an emoji).
LONE = "\ud83d"


@pytest.mark.parametrize(
    "body",
    [
        {"type": "cancel", "id": "c" + LONE},
        {"type": "selectModel", "modelId": LONE},
        {"type": "send", "text": "hi", "attachmentIds": ["a1", LONE]},
        {"type": "approve", "toolCallId": "t" + LONE, "action": "once"},
        {"type": "edit", "messageId": "m" + LONE, "text": "x"},
        {"type": "login", "email": "a@b.c", "password": "pw" + LONE},
        {"type": "signup", "email": LONE + "@b.c", "password": "pw"},
        {"type": "startAnonymous", "captchaToken": LONE},
        {"type": "oauthStart", "provider": LONE},
        {"type": "openLink", "url": "https://example.com/" + LONE},
        {"type": "skillEnable", "name": LONE},
    ],
)
def test_a_machine_value_must_be_encodable(body: Dict[str, Any]) -> None:
    with pytest.raises(CommandError):
        parse_command(body)


def test_user_text_keeps_a_lone_surrogate_as_a_replacement_character() -> None:
    assert parse_command({"type": "send", "text": "hi " + LONE}).args["text"] == "hi \ufffd"
    assert parse_command({"type": "send", "text": LONE}).args["text"] == "\ufffd"
    edited = parse_command({"type": "edit", "messageId": "m", "text": LONE + "x"})
    assert edited.args["text"] == "\ufffdx"
    denied = parse_command(
        {"type": "approve", "toolCallId": "t", "action": "deny", "feedback": "no" + LONE}
    )
    assert denied.args["feedback"] == "no\ufffd"
    assert parse_command({"type": "copy", "text": LONE}).args["text"] == "\ufffd"
    # A whole emoji (one code point once json.loads has joined the pair) is untouched.
    assert parse_command({"type": "send", "text": "\U0001f600"}).args["text"] == "\U0001f600"


def test_an_unknown_type_is_echoed_short_and_escaped() -> None:
    for kind, bound in (("x" * 100_000, 200), ("send\nFAKE LOG LINE", 200), ([0] * 100_000, 80)):
        with pytest.raises(CommandError) as info:
            parse_command({"type": kind})
        message = str(info.value)
        assert len(message) < bound, message[:300]
        assert "\n" not in message


def test_unexpected_field_names_are_echoed_short_and_escaped() -> None:
    with pytest.raises(CommandError) as info:
        parse_command({"type": "cancel", "k\ninjected log line": 1, "\u202eevil": 2})
    message = str(info.value)
    assert "\n" not in message and "\u202e" not in message
    many = {"k{}-".format(i) + "x" * 1000: 1 for i in range(1000)}
    with pytest.raises(CommandError) as info:
        parse_command(dict(many, type="cancel"))
    assert len(str(info.value)) < 1000


def test_result_and_error_shapes() -> None:
    command = Command(id="c-9", type="signup", args={})
    assert success_result(command) == {"id": "c-9", "type": "signup", "ok": True}
    assert success_result(command, {"confirmationRequired": True}) == {
        "id": "c-9", "type": "signup", "ok": True, "data": {"confirmationRequired": True},
    }
    assert failure_result(command, "busy", "later") == {
        "id": "c-9", "type": "signup", "ok": False, "error": {"type": "busy", "message": "later"},
    }
    assert http_error("bad_command", "no") == {
        "ok": False, "error": {"type": "bad_command", "message": "no"},
    }
    assert "busy" in ERROR_TYPES and "internal_error" in ERROR_TYPES
