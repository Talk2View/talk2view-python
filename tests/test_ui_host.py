"""talk2view.ui.host: Theme validation, GenericHost dispatching and adapter surface."""

from __future__ import annotations

import subprocess
import sys
import threading
from typing import List

import pytest

from talk2view import MemoryStorage, Talk2View
from talk2view.dispatch import Dispatcher
from talk2view.ui import GenericHost, HostAdapter, Theme


def test_the_core_package_never_imports_the_ui() -> None:
    probe = "import sys, talk2view; print(sorted(m for m in sys.modules if 'talk2view.ui' in m))"
    result = subprocess.run(  # noqa: S603 — fixed interpreter and argument, no shell
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


def test_theme_accepts_modes_and_t2v_tokens() -> None:
    theme = Theme(mode="dark", tokens={"--t2v-bg": "#021C25", "--t2v-accent": "#26C8B8"})
    assert theme.to_wire() == {
        "mode": "dark",
        "tokens": {"--t2v-bg": "#021C25", "--t2v-accent": "#26C8B8"},
    }
    assert Theme(mode="light").to_wire() == {"mode": "light", "tokens": {}}


@pytest.mark.parametrize(
    "mode, tokens",
    [
        ("sepia", {}),
        ("light", {"color": "red"}),
        ("light", {"--other-bg": "red"}),
        ("light", {"--t2v-BG": "red"}),
        ("light", {"--t2v-bg": "x" * 257}),
        ("light", {"--t2v-bg": "red\nblue"}),
        ("light", {"--t2v-bg": 3}),
    ],
)
def test_theme_refuses_anything_outside_its_namespace(mode: str, tokens: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError):
        Theme(mode=mode, tokens=tokens)  # type: ignore[arg-type]


def test_generic_host_runs_posts_on_its_own_thread_only() -> None:
    host = GenericHost()
    seen: List[int] = []
    worker = threading.Thread(target=lambda: host.post(lambda: seen.append(threading.get_ident())))
    worker.start()
    worker.join()
    assert host.is_main_thread()
    assert host.pump() == 1
    assert seen == [threading.get_ident()]
    errors: List[BaseException] = []

    def pump_elsewhere() -> None:
        try:
            host.pump()
        except RuntimeError as exc:
            errors.append(exc)

    other = threading.Thread(target=pump_elsewhere)
    other.start()
    other.join()
    assert len(errors) == 1


def test_run_forever_pumps_until_stop() -> None:
    host = GenericHost()
    ran: List[str] = []

    def producer() -> None:
        host.post(lambda: ran.append("a"))
        host.post(host.stop)

    threading.Thread(target=producer).start()
    host.run_forever(poll_s=0.005)
    assert ran == ["a"]


def test_run_forever_refuses_a_foreign_thread() -> None:
    host = GenericHost()
    errors: List[BaseException] = []

    def run() -> None:
        try:
            host.run_forever()
        except RuntimeError as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(5)
    assert len(errors) == 1


def test_show_and_open_external_use_the_injected_opener() -> None:
    opened: List[str] = []
    storage = MemoryStorage()
    host = GenericHost(open_url=opened.append, storage=storage, theme=Theme(mode="dark"))
    host.show("http://127.0.0.1:1/#t=x")
    host.open_external("https://example.com/")
    assert opened == ["http://127.0.0.1:1/#t=x", "https://example.com/"]
    assert host.theme().mode == "dark"
    assert host.storage() is storage
    assert host.audio() is None
    assert host.supports_clipboard is False
    with pytest.raises(NotImplementedError):
        host.clipboard_set("x")


def test_generic_host_is_a_host_adapter_and_a_dispatcher() -> None:
    host = GenericHost()
    adapter: HostAdapter = host
    dispatcher: Dispatcher = host
    assert adapter.name == "generic" and dispatcher.is_main_thread()
    t2v = Talk2View("pk_test", dispatcher=host)
    try:
        assert t2v.tools.dispatcher is host
    finally:
        t2v.close()
