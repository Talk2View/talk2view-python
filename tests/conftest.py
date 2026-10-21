"""Fixtures shared by the tests that run against ``FakeEngine``."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from pytest_httpserver import HTTPServer

from .fake_engine import FakeEngine
from .ui_engine import AccountEngine


@pytest.fixture(scope="session")
def fake_engine_server() -> Iterator[HTTPServer]:
    """One threaded server for every ``engine`` test: stopping one waits out its 0.5 s
    poll interval, which per test would add up."""
    server = HTTPServer(threaded=True)
    server.start()
    try:
        yield server
    finally:
        server.clear()
        server.stop()


@pytest.fixture
def engine(fake_engine_server: HTTPServer) -> Iterator[FakeEngine]:
    """A fresh fake engine — no users, no sessions — on the shared server."""
    fake = FakeEngine(fake_engine_server).start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def account_engine(fake_engine_server: HTTPServer) -> Iterator[AccountEngine]:
    """``engine`` plus /v1/config, named /v1/models and /v1/attachments (the UI tests)."""
    fake = AccountEngine(fake_engine_server).start()
    try:
        yield fake
    finally:
        if fake.config_gate is not None:
            fake.config_gate.set()
        fake.stop()


@pytest.fixture
def bundled_dir(tmp_path: Path) -> str:
    """A read-only bundled skills folder holding one ``ct-window-presets/SKILL.md``, as
    ``T2VSkills.use_store``'s ``bundled`` takes (see ``test_skills_store_integration.py``)."""
    folder = tmp_path / "bundled"
    (folder / "ct-window-presets").mkdir(parents=True, exist_ok=True)
    (folder / "ct-window-presets" / "SKILL.md").write_text(
        "---\nname: ct-window-presets\ndescription: CT presets. Use for CT windows.\n---\nB\n"
    )
    return str(folder)
