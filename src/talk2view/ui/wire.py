"""The command half of the wire contract between ``UIServer`` and the embed page.

``POST /command`` takes one JSON object ``{"id"?, "type", ...fields}``. ``parse_command``
validates it on the HTTP thread before anything is queued: an unknown ``type``, a missing
or mistyped field, or a field the command does not take is refused (400, ``bad_command``).
A valid command is queued (202) and later ends in exactly one result, recorded in the
``results`` slice:

    {"id": <the command's id or null>, "type": <its type>, "ok": true, "data"?: {...}}
    {"id": ..., "type": ..., "ok": false, "error": {"type": <ERROR_TYPES>, "message": str}}

``id`` is the page's own correlation id (at most 128 characters); the server never
interprets it.

**Lone surrogates.** ``json.loads`` turns an escaped half of a surrogate pair into a lone
surrogate, which UTF-8 cannot encode; a page can send one (a JavaScript string cut through
an emoji). In the end-user's own text (``send.text``, ``edit.text``, ``approve.feedback``,
``copy.text``) each one is replaced with U+FFFD, the replacement character, rather than
refusing what they typed. Every other string is a machine value (an id, a model, an email,
a password, a token, a link, a name), and one holding a lone surrogate is refused.

A refusal's message quotes what the page sent only as a short ``repr``, so it stays small
and a newline in it cannot forge a log line.

**Credentials.** ``login``/``signup``'s ``password`` and ``startAnonymous``'s
``captchaToken`` are carried as ``Secret``, never as plain strings, so no ``repr`` of a
command or its arguments (what a traceback that captures locals prints) holds one. The
handler that needs the value calls ``reveal()`` inline, as the argument of its auth call.

**Provider sign-in**. ``oauthStart`` takes ``provider`` ``"apple"`` or
``"google"`` (anything else is a 400) and its result means "started": the attempt then
shows in the ``auth`` slice as ``oauthPending``, and ends in the account or in
``oauthError``. It fails with ``oauth_disabled`` (``ChatFeatures.provider_sign_in`` is
off), ``oauth_unavailable`` (the engine does not offer that provider for the partner:
``capabilities.oauthProviders``) or ``busy`` (one is already waiting). ``oauthCancel`` and
``oauthDismissError`` (clears ``oauthError``) always succeed. ``login``, ``signup``,
``logout`` and ``startAnonymous`` cancel a waiting sign-in first. A cancel only stops an
attempt that has not stored its session yet: one that lands while the session is being
stored does not undo the sign-in (the account shows, and ``oauthError`` stays null);
after a ``login`` or ``logout`` the account that command chose is the one that stays.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Optional

__all__ = [
    "COMMANDS",
    "ERROR_TYPES",
    "Command",
    "CommandError",
    "CommandFailure",
    "Secret",
    "failure_result",
    "http_error",
    "parse_command",
    "success_result",
]

MAX_ID_CHARS = 128
MAX_ATTACHMENTS = 10
#: How much of a page-supplied value a refusal's message quotes, and how many field names.
MAX_ECHO_CHARS = 64
MAX_ECHO_FIELDS = 5

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")

#: ``result.error.type`` values a command can end with, besides the engine's own error
#: types (``authentication_error``, ``captcha_required``, ``sign_in_required``, ...), which
#: are passed through unchanged.
ERROR_TYPES: FrozenSet[str] = frozenset(
    {
        "busy",
        "closed",
        "not_found",
        "unknown_attachment",
        "unknown_model",
        "unknown_skill",
        "skill_too_large",
        "account_unavailable",
        "oauth_unavailable",
        "oauth_disabled",
        "dictation_unavailable",
        "clipboard_unavailable",
        "invalid_url",
        "attachments_disabled",
        "anonymous_not_allowed",
        "engine_error",
        "internal_error",
    }
)


class CommandError(ValueError):
    """The page sent a command this server does not accept (HTTP 400, ``bad_command``)."""


class CommandFailure(Exception):
    """A valid command that could not be carried out: its result's ``error``."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.type = error_type
        self.message = message


class Secret:
    """A credential from the page. ``repr`` and ``str`` are ``Secret(***)``, so no log line,
    result or traceback that prints locals can show it; ``reveal()`` returns it. Read-only,
    and equal only to itself: an ``==`` on the value would let a comparison (an assertion,
    a log of it) probe or print it. It is never pickled (the bytes would hold the value in
    plain text), and ``copy``/``deepcopy`` return the same object."""

    __slots__ = ("_value",)
    _value: str

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("a Secret is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("a Secret is read-only")

    def __reduce_ex__(self, protocol: Any) -> Any:  # what pickle calls, at every protocol
        raise TypeError("a Secret cannot be pickled")

    def __copy__(self) -> "Secret":
        return self

    def __deepcopy__(self, memo: Dict[int, Any]) -> "Secret":
        return self


@dataclass(frozen=True)
class Command:
    id: Optional[str]
    type: str
    #: Left out of ``repr``: it holds passwords, captcha tokens and the end-user's text,
    #: and traceback tools (Sentry's local variables, ``pytest -l``) print locals by repr.
    args: Dict[str, Any] = field(repr=False)


Reader = Callable[[Dict[str, Any], str], Any]


def _echo(value: Any) -> str:
    """A page-supplied value for a refusal's message: a string as the ``repr`` of at most
    ``MAX_ECHO_CHARS`` of it (escaped, so no newline, control or bidi character gets
    through), anything else as its type alone."""
    if not isinstance(value, str):
        return "<{}>".format(type(value).__name__)
    if len(value) > MAX_ECHO_CHARS:
        return repr(value[:MAX_ECHO_CHARS]) + "..."
    return repr(value)


def _machine(value: str, name: str) -> str:
    """A machine value must be encodable: a lone surrogate in it is refused."""
    if _LONE_SURROGATE.search(value):
        raise CommandError("{} is not valid Unicode text".format(name))
    return value


def _text(
    *,
    max_chars: int,
    required: bool = True,
    non_empty: bool = False,
    user: bool = False,
    secret: bool = False,
    nullable: bool = False,
) -> Reader:
    """A string field. ``user=True`` marks the end-user's own text, where a lone surrogate
    becomes U+FFFD; in any other field it is refused. ``secret=True`` hands the value on
    as a ``Secret``. ``nullable=True`` also takes an explicit ``null``."""

    def read(obj: Dict[str, Any], name: str) -> Any:
        if name not in obj:
            if required:
                raise CommandError("{} is required".format(name))
            return None
        value = obj[name]
        if value is None and nullable:
            return None
        if not isinstance(value, str):
            raise CommandError("{} must be a string".format(name))
        if len(value) > max_chars:
            raise CommandError("{} is longer than {} characters".format(name, max_chars))
        if non_empty and not value.strip():
            raise CommandError("{} must not be empty".format(name))
        if user:
            return _LONE_SURROGATE.sub("\ufffd", value)
        if secret:
            return Secret(_machine(value, name))
        return _machine(value, name)

    return read


def _choice(*values: Optional[str]) -> Reader:
    def read(obj: Dict[str, Any], name: str) -> Any:
        if name not in obj:
            raise CommandError("{} is required".format(name))
        value = obj[name]
        if value not in values or isinstance(value, bool):
            raise CommandError("{} must be one of {}".format(name, list(values)))
        return value

    return read


def _ids(obj: Dict[str, Any], name: str) -> Any:
    value = obj.get(name, [])
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise CommandError("{} must be a list of at most {} ids".format(name, MAX_ATTACHMENTS))
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_ID_CHARS:
            raise CommandError("{} must hold non-empty string ids".format(name))
        _machine(item, name)
    return list(value)


def _object(obj: Dict[str, Any], name: str) -> Any:
    value = obj.get(name)
    if value is not None and not isinstance(value, dict):
        raise CommandError("{} must be an object".format(name))
    return value


#: Every command type and its fields. Nothing else is accepted.
COMMANDS: Dict[str, Dict[str, Reader]] = {
    # Chat (the ChatController).
    "send": {"text": _text(max_chars=1_000_000, user=True), "attachmentIds": _ids},
    "cancel": {},
    "approve": {
        "toolCallId": _text(max_chars=256, non_empty=True),
        "action": _choice("once", "always", "deny"),
        "feedback": _text(max_chars=10_000, required=False, user=True),
        "updatedInput": _object,
    },
    "reload": {},
    "edit": {
        "messageId": _text(max_chars=MAX_ID_CHARS, non_empty=True),
        "text": _text(max_chars=1_000_000, non_empty=True, user=True),
    },
    "newSession": {},
    "clearError": {},
    # Account (Talk2View).
    # null: "Default", the partner's model.
    "selectModel": {"modelId": _text(max_chars=256, non_empty=True, nullable=True)},
    "login": {
        "email": _text(max_chars=320, non_empty=True),
        "password": _text(max_chars=1024, non_empty=True, secret=True),
    },
    "signup": {
        "email": _text(max_chars=320, non_empty=True),
        "password": _text(max_chars=1024, non_empty=True, secret=True),
    },
    "logout": {},
    "startAnonymous": {"captchaToken": _text(max_chars=4096, required=False, secret=True)},
    "oauthStart": {"provider": _choice("apple", "google")},
    "oauthCancel": {},
    "oauthDismissError": {},
    "warmUp": {},
    "setFontSize": {"fontSize": _choice("small", "medium", "large", None)},
    # Host.
    "copy": {"text": _text(max_chars=1_000_000, user=True)},
    "openLink": {"url": _text(max_chars=4096, non_empty=True)},
    "dictationStart": {},
    "dictationStop": {},
    # Skills.
    "skillsList": {},
    "skillEnable": {"name": _text(max_chars=255, non_empty=True)},
    "skillDisable": {"name": _text(max_chars=255, non_empty=True)},
}


def parse_command(obj: Any) -> Command:
    if not isinstance(obj, dict):
        raise CommandError("a command is a JSON object")
    kind = obj.get("type")
    if not isinstance(kind, str):
        raise CommandError("a command needs a type, as a string")
    if kind not in COMMANDS:
        raise CommandError("unknown command type {}".format(_echo(kind)))
    command_id = obj.get("id")
    if command_id is not None:
        if not isinstance(command_id, str) or not command_id or len(command_id) > MAX_ID_CHARS:
            raise CommandError("id must be a non-empty string of at most 128 characters")
        _machine(command_id, "id")
    fields = COMMANDS[kind]
    unexpected = sorted(set(obj) - {"id", "type"} - set(fields), key=str)
    if unexpected:
        named = ", ".join(_echo(name) for name in unexpected[:MAX_ECHO_FIELDS])
        if len(unexpected) > MAX_ECHO_FIELDS:
            named += " and {} more".format(len(unexpected) - MAX_ECHO_FIELDS)
        raise CommandError("{} does not take {}".format(kind, named))
    args = {name: read(obj, name) for name, read in fields.items()}
    if kind == "send" and not args["text"].strip() and not args["attachmentIds"]:
        raise CommandError("send needs text or an attachment")
    return Command(id=command_id, type=kind, args=args)


def success_result(command: Command, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"id": command.id, "type": command.type, "ok": True}
    if data is not None:
        result["data"] = data
    return result


def failure_result(command: Command, error_type: str, message: str) -> Dict[str, Any]:
    return {
        "id": command.id,
        "type": command.type,
        "ok": False,
        "error": {"type": error_type, "message": message},
    }


def http_error(error_type: str, message: str) -> Dict[str, Any]:
    """The body of every non-2xx answer: ``{"ok": false, "error": {"type", "message"}}``."""
    return {"ok": False, "error": {"type": error_type, "message": message}}
