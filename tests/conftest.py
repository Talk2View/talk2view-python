"""Fixtures shared by the tests that run against ``FakeEngine``."""

from __future__ import annotations

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
