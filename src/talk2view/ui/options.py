"""Integrator options for the chat page: what the TypeScript SDK's ``<Talk2ViewChat>`` takes
as props (``chat/provider.tsx:30-83``), for a host that serves the page with ``UIServer``::

    options = ChatOptions(
        welcome=ChatWelcome(heading="Ask about your scene", suggestions=["Load MRHead"]),
        features=ChatFeatures(attachments=False),
        reset_password_url="https://example.com/reset",
        footer="Answers can be wrong. Check them before you act.",
        tools={"load_file": ToolPresentation(activity="Loading a file", destructive=True)},
    )
    server = UIServer(controller, host, options=options)

They reach the page read-only, as two app slices (``AppState``):

- ``options``: ``{"welcome": {"heading": str | null, "suggestions": [str]}, "features":
  {"dictation", "attachments", "settings", "account", "threadList", "providerSignIn":
  bool},
  "allowAnonymous": bool, "resetPasswordUrl": str | null, "footer": str | null}``;
- ``tools``: ``{<tool name>: {"activity": str | null, "destructive": bool,
  "warning": str | null}}``, TS ``describeToolActivity``, ``isToolDestructive`` and
  ``destructiveWarning`` given once per tool instead of computed per call.

What TS takes as a function or a React node is plain text here, and the page shows it as
text, never as HTML. TS ``fontFamily`` and ``className`` (dark mode) are the host's
``Theme``; ``systemPrompt`` and ``tools`` are ``ChatController`` and ``t2v.tools``.

Everything is checked when it is built: a value of the wrong type raises ``TypeError``, and
an empty or over-long string, a lone surrogate, too many entries, a tool name the engine
would refuse, a ``warning`` on a tool that is not ``destructive``, or a reset-password link
that ``openLink`` would refuse raises ``ValueError``. Each string is first copied to an exact
``str``, and the suggestions and the tools to a plain list and dict, in one pass: what is
checked is what is kept, whatever a subclass's own methods or ``len()`` say. The page never
meets a bad option.

The server enforces the two that fail closed. With ``features.attachments`` off,
``POST /upload`` answers 403 ``attachments_disabled`` and a ``send`` that names attachments
ends in ``attachments_disabled``. That stops new files only: it does not strip the chat's
history, so ``edit`` and ``reload`` re-send the attachments a message there already carries
(one the host sent itself, or one sent before this server over the same controller).
With ``allow_anonymous`` off, ``startAnonymous`` ends in
``anonymous_not_allowed``, and ``AppState`` and ``UIServer`` refuse a ``Talk2View`` built
with ``anonymous_auto_start=True``, the app's or the controller's, which would start a
guest session behind the page's sign-in gate. With ``features.provider_sign_in`` off,
``oauthStart`` ends in ``oauth_disabled``. The other flags only shape the page.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .wire import _LONE_SURROGATE

__all__ = ["ChatFeatures", "ChatOptions", "ChatWelcome", "ToolPresentation"]

MAX_HEADING_CHARS = 200
MAX_ACTIVITY_CHARS = 200
#: A suggestion, the footer, a warning.
MAX_TEXT_CHARS = 1000
MAX_SUGGESTIONS = 12
MAX_TOOLS = 128
MAX_URL_CHARS = 4096
#: The engine's limit on a client tool's name: 1..128 characters.
_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,127}")

#: Unicode categories no link may contain: controls (C0, C1, DEL), format characters (bidi
#: overrides and isolates, zero-width characters), and every kind of space and separator.
_UNSAFE_IN_LINKS = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp"})


def _openable(url: str) -> bool:
    """An ``http``/``https`` link with a host and nothing that makes it read as another: no
    user part (``https://google.com@evil.com`` opens ``evil.com``), a bracketed host only
    when it is an IPv6 address, a port only when it is one, and no character from
    ``_UNSAFE_IN_LINKS``. ``openLink`` and ``reset_password_url`` share this rule."""
    if any(unicodedata.category(ch) in _UNSAFE_IN_LINKS for ch in url):
        return False
    try:
        parts = urlsplit(url)
        host, _port = parts.hostname, parts.port  # .port raises ValueError for a bad port
        if parts.scheme not in ("http", "https") or not host or "@" in parts.netloc:
            return False
        if parts.netloc.startswith("["):
            ipaddress.IPv6Address(host)
    except ValueError:  # e.g. an unclosed bracket, or a bracketed host that is not IPv6
        return False
    return True


def _text(name: str, value: Any, max_chars: int) -> str:
    """``value`` copied to an exact ``str`` (as ``EventHub.set_slice`` copies keys), so no
    method of a subclass runs on it again, then checked: not blank, at most ``max_chars``
    code points, and text UTF-8 can carry."""
    if not isinstance(value, str):
        raise TypeError("{} must be a string, not {}".format(name, type(value).__name__))
    text = str.__str__(value)
    if not text.strip():
        raise ValueError("{} must not be empty".format(name))
    if len(text) > max_chars:
        raise ValueError("{} is longer than {} characters".format(name, max_chars))
    if _LONE_SURROGATE.search(text):
        raise ValueError("{} is not valid Unicode text".format(name))
    return text


def _optional_text(name: str, value: Any, max_chars: int) -> Optional[str]:
    return None if value is None else _text(name, value, max_chars)


def _flag(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise TypeError("{} must be True or False, not {}".format(name, type(value).__name__))


@dataclass(frozen=True)
class ChatWelcome:
    """The opening screen: a heading, and the openers offered under the composer on an
    empty chat (TS ``Talk2ViewChatWelcome``). A list of suggestions is kept as a tuple."""

    heading: Optional[str] = None
    suggestions: Sequence[str] = ()

    def __post_init__(self) -> None:
        heading = _optional_text("welcome.heading", self.heading, MAX_HEADING_CHARS)
        object.__setattr__(self, "heading", heading)
        if not isinstance(self.suggestions, (list, tuple)):
            raise TypeError("welcome.suggestions must be a list of strings")
        given = list(self.suggestions)  # one pass: what is checked is what is kept
        if len(given) > MAX_SUGGESTIONS:
            raise ValueError("welcome.suggestions holds at most {}".format(MAX_SUGGESTIONS))
        suggestions = tuple(
            _text("welcome.suggestions[{}]".format(index), suggestion, MAX_TEXT_CHARS)
            for index, suggestion in enumerate(given)
        )
        object.__setattr__(self, "suggestions", suggestions)

    def to_wire(self) -> Dict[str, Any]:
        return {"heading": self.heading, "suggestions": list(self.suggestions)}


@dataclass(frozen=True)
class ChatFeatures:
    """The page's built-in views, all on by default (TS ``Talk2ViewChatFeatures``). The
    server enforces ``attachments`` for new files, not for the attachments already in the
    chat's history (see the module docstring); the rest only shape the page. ``dictation``
    also needs ``capabilities.audio``, false until hosts record audio. ``provider_sign_in``
    is enforced too: off, ``oauthStart`` ends in ``oauth_disabled``."""

    dictation: bool = True
    attachments: bool = True
    settings: bool = True
    account: bool = True
    thread_list: bool = True
    #: "Continue with Apple" / "Continue with Google". They show only while the engine
    #: also offers desktop sign-in for the partner (``capabilities.oauth``); off, the
    #: server refuses ``oauthStart`` with ``oauth_disabled``.
    provider_sign_in: bool = True

    def __post_init__(self) -> None:
        for flag in dataclasses.fields(self):
            _flag("features." + flag.name, getattr(self, flag.name))

    def to_wire(self) -> Dict[str, Any]:
        return {
            "dictation": self.dictation,
            "attachments": self.attachments,
            "settings": self.settings,
            "account": self.account,
            "threadList": self.thread_list,
            "providerSignIn": self.provider_sign_in,
        }


@dataclass(frozen=True)
class ToolPresentation:
    """How the page shows one tool's calls. ``activity`` is what the running row says
    (TS ``describeToolActivity``; the page's own default is "Working…"). ``destructive``
    puts a warning above the approval buttons (TS ``isToolDestructive``), and ``warning``
    replaces the page's default warning (TS ``destructiveWarning``), so it needs
    ``destructive``."""

    activity: Optional[str] = None
    destructive: bool = False
    warning: Optional[str] = None

    def __post_init__(self) -> None:
        activity = _optional_text("activity", self.activity, MAX_ACTIVITY_CHARS)
        object.__setattr__(self, "activity", activity)
        _flag("destructive", self.destructive)
        object.__setattr__(self, "warning", _optional_text("warning", self.warning, MAX_TEXT_CHARS))
        if self.warning is not None and not self.destructive:
            raise ValueError("a warning only shows for a destructive tool")

    def to_wire(self) -> Dict[str, Any]:
        return {"activity": self.activity, "destructive": self.destructive, "warning": self.warning}


class _ToolMap(Mapping[str, ToolPresentation]):
    """``ChatOptions.tools``: a read-only copy of the integrator's mapping. Unlike a
    ``MappingProxyType`` it copies, pickles and hashes, so the options holding it do too.
    Equal maps hash equal whatever order their tools were given in."""

    def __init__(self, tools: Mapping[str, ToolPresentation]) -> None:
        self._tools: Dict[str, ToolPresentation] = dict(tools)

    def __getitem__(self, name: str) -> ToolPresentation:
        return self._tools[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __hash__(self) -> int:
        return hash(frozenset(self._tools.items()))

    def __repr__(self) -> str:
        return repr(self._tools)


@dataclass(frozen=True)
class ChatOptions:
    """The integrator's options for the chat page (TS ``Talk2ViewChatProps``); see the
    module docstring. ``tools`` maps a tool name to its ``ToolPresentation``, and is a
    read-only copy: changing the mapping afterwards changes nothing. The options copy,
    deep-copy, pickle and hash like any frozen value."""

    welcome: ChatWelcome = field(default_factory=ChatWelcome)
    features: ChatFeatures = field(default_factory=ChatFeatures)
    #: Signed-out visitors may chat as guests (TS default true). False: sign in first.
    allow_anonymous: bool = True
    #: Where the page's "Forgot it?" link goes (through ``openLink``). None: no link.
    reset_password_url: Optional[str] = None
    #: A short line under the composer, e.g. terms.
    footer: Optional[str] = None
    tools: Mapping[str, ToolPresentation] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.welcome, ChatWelcome):
            raise TypeError("welcome must be a ChatWelcome")
        if not isinstance(self.features, ChatFeatures):
            raise TypeError("features must be a ChatFeatures")
        _flag("allow_anonymous", self.allow_anonymous)
        url = _optional_text("reset_password_url", self.reset_password_url, MAX_URL_CHARS)
        if url is not None and not _openable(url):
            raise ValueError("reset_password_url must be an http or https link openLink opens")
        object.__setattr__(self, "reset_password_url", url)
        object.__setattr__(self, "footer", _optional_text("footer", self.footer, MAX_TEXT_CHARS))
        if not isinstance(self.tools, Mapping):
            raise TypeError("tools must map tool names to ToolPresentation")
        given = dict(self.tools)  # one pass: counted by what it yields, not by its len()
        if len(given) > MAX_TOOLS:
            raise ValueError("tools holds at most {} tools".format(MAX_TOOLS))
        copied: Dict[str, ToolPresentation] = {}
        for key, presentation in given.items():
            if not isinstance(key, str):
                raise TypeError("a tool name must be a string, not {}".format(type(key).__name__))
            name = str.__str__(key)
            if not _TOOL_NAME.fullmatch(name):
                raise ValueError("{!r} is not a tool name the engine accepts".format(name[:64]))
            if not isinstance(presentation, ToolPresentation):
                raise TypeError("tools[{!r}] must be a ToolPresentation".format(name))
            copied[name] = presentation
        object.__setattr__(self, "tools", _ToolMap(copied))

    def to_wire(self) -> Dict[str, Any]:
        """The ``options`` slice."""
        return {
            "welcome": self.welcome.to_wire(),
            "features": self.features.to_wire(),
            "allowAnonymous": self.allow_anonymous,
            "resetPasswordUrl": self.reset_password_url,
            "footer": self.footer,
        }

    def tools_to_wire(self) -> Dict[str, Any]:
        """The ``tools`` slice."""
        return {name: presentation.to_wire() for name, presentation in self.tools.items()}
