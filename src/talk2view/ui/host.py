"""Host adapters: what the UI server needs from the application it runs inside.

A ``HostAdapter`` is also a ``Dispatcher`` (``post`` + ``is_main_thread``), so one object
can serve as ``Talk2View(dispatcher=host)`` and as the UI server's host. ``GenericHost``
is the adapter for a plain Python process: the system browser shows the page and the
thread that built it drains a ``QueueDispatcher`` with ``pump()`` or ``run_forever()``.
Hosts with a toolkit (3D Slicer, FreeCAD, Blender) ship their own adapter; the core never
imports a UI toolkit.
"""

from __future__ import annotations

import re
import threading
import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Literal, Mapping, Optional, Protocol

from ..dispatch import QueueDispatcher
from ..storage import TokenStorage

__all__ = ["GenericHost", "HostAdapter", "Theme"]

#: A theme token is a CSS custom property the page may set; nothing else gets through.
_TOKEN_NAME = re.compile(r"^--t2v-[a-z0-9-]{1,64}$")
_MAX_TOKEN_VALUE = 256


@dataclass(frozen=True)
class Theme:
    """The host's look, pushed to the page (``theme`` slice).

    ``tokens`` maps ``--t2v-*`` CSS custom properties to values, e.g.
    ``{"--t2v-bg": "#F2F0EC"}``. Anything else is refused here (``ValueError``), so a host
    can never hand the page a property outside the chat's own namespace.
    """

    mode: Literal["light", "dark"]
    tokens: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in ("light", "dark"):
            raise ValueError("Theme.mode must be 'light' or 'dark', not {!r}".format(self.mode))
        for name, value in self.tokens.items():
            if not isinstance(name, str) or not _TOKEN_NAME.match(name):
                raise ValueError("Theme token names must match --t2v-[a-z0-9-]+: {!r}".format(name))
            if not isinstance(value, str) or len(value) > _MAX_TOKEN_VALUE:
                raise ValueError("Theme token {} needs a string of at most {} chars".format(
                    name, _MAX_TOKEN_VALUE))
            if any(ord(ch) < 0x20 for ch in value):
                raise ValueError("Theme token {} contains a control character".format(name))

    def to_wire(self) -> Dict[str, Any]:
        return {"mode": self.mode, "tokens": dict(self.tokens)}


class HostAdapter(Protocol):
    """What ``UIServer`` needs from its host. Also satisfies ``Dispatcher``.

    ``post`` must never block (the UI server calls it from its router thread, and the chat
    controller from inside its delivery window). ``show``, ``open_external`` and
    ``clipboard_set`` are only ever called through ``post``, i.e. on the host's main
    thread. A host whose ``clipboard_set`` works sets ``supports_clipboard = True`` (read
    with ``getattr``; absent means False, so the page never offers a copy that cannot work).
    """

    name: str

    def post(self, fn: Callable[[], None]) -> None: ...

    def is_main_thread(self) -> bool: ...

    def show(self, url: str) -> None: ...

    def open_external(self, url: str) -> Any:
        """Open ``url`` in the system browser. Return ``False`` when no browser could be
        opened; anything else counts as opened."""

    def clipboard_set(self, text: str) -> None: ...

    def theme(self) -> Theme: ...

    def storage(self) -> Optional[TokenStorage]: ...

    def audio(self) -> Any: ...  # reserved: an AudioCapture, or None


class GenericHost:
    """A host for a plain Python process: the system browser and a pumped queue.

    Build it on the thread that will pump it (the "main" thread); pass it to
    ``Talk2View(dispatcher=host)`` too, so tool handlers run on that thread. Then either
    call ``pump()`` from your own loop or hand the thread over with ``run_forever()``.
    ``stop()`` (any thread) ends ``run_forever``.
    """

    name = "generic"
    #: The standard library has no clipboard: the page does not offer "copy" here.
    supports_clipboard = False

    def __init__(
        self,
        *,
        theme: Optional[Theme] = None,
        storage: Optional[TokenStorage] = None,
        open_url: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._dispatcher = QueueDispatcher()  # owned by the constructing thread
        self._theme = theme if theme is not None else Theme(mode="light")
        self._storage = storage
        self._open_url: Callable[[str], Any] = open_url if open_url is not None else webbrowser.open
        self._stop = threading.Event()

    # ── Dispatcher ──

    def post(self, fn: Callable[[], None]) -> None:
        self._dispatcher.post(fn)

    def is_main_thread(self) -> bool:
        return self._dispatcher.is_main_thread()

    def pump(self, max_items: Optional[int] = None) -> int:
        """Run what other threads posted. Only the constructing thread may call it."""
        return self._dispatcher.pump(max_items)

    def run_forever(self, *, poll_s: float = 0.02) -> None:
        """Pump every ``poll_s`` until ``stop()`` or Ctrl+C. Owner thread only."""
        if not self.is_main_thread():
            raise RuntimeError("GenericHost.run_forever() must run on the thread that built it")
        try:
            while not self._stop.is_set():
                self.pump()
                self._stop.wait(poll_s)
        except KeyboardInterrupt:
            pass

    def stop(self) -> None:
        self._stop.set()

    # ── HostAdapter ──

    def show(self, url: str) -> None:
        self._open_url(url)

    def open_external(self, url: str) -> Any:
        return self._open_url(url)  # webbrowser.open's False: no browser was opened

    def clipboard_set(self, text: str) -> None:
        raise NotImplementedError("GenericHost has no clipboard")

    def theme(self) -> Theme:
        return self._theme

    def storage(self) -> Optional[TokenStorage]:
        return self._storage

    def audio(self) -> Any:
        return None
