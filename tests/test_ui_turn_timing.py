"""Turn timings through the chat UI server: ``send`` with ``timing``, the page's ``metrics``
report, and a page (or a controller) that never reports."""

from __future__ import annotations

import http.client
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from talk2view import ChatController, Talk2View, TurnMetrics, TurnTiming
from talk2view.ui import GenericHost, UIServer
from talk2view.ui.app_state import AppState
from talk2view.ui.router import CommandRouter, UploadRegistry
from talk2view.ui.testing import FakeController
from talk2view.ui.wire import parse_command

from .ui_engine import AccountEngine
from .ui_helpers import EventStream, wait_for

STUB = Path(__file__).parent / "fixtures" / "ui_static"


class Chat:
    """A real ``Talk2View`` + ``ChatController`` behind a started ``UIServer``, with Ana
    signed in, and every turn timing it delivers."""

    def __init__(self, engine: AccountEngine, grace_s: float) -> None:
        self.t2v = Talk2View("pk_test", base_url=engine.url())
        self.t2v.metrics = TurnMetrics(page_grace_s=grace_s)
        self.timings: List[TurnTiming] = []
        self.t2v.metrics.on_turn(self.timings.append)
        self.controller = ChatController(self.t2v)
        self.server = UIServer(self.controller, GenericHost(), static_dir=STUB)
        self.token = self.server.start().split("#t=", 1)[1]
        self.page = EventStream(self.server.port, self.token)
        assert self.page.wait_until(lambda s: "auth" in s and "messages" in s)
        self.command(type="login", email="ana@example.com", password="pw", id="login")
        assert self.page.wait_until(lambda s: s["auth"]["isAuthenticated"])

    def command(self, **body: Any) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        conn.request("POST", "/command", body=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
        })
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 202

    def result(self, command_id: str) -> Dict[str, Any]:
        assert self.page.wait_until(
            lambda s: any(r["id"] == command_id for r in s["results"])
        )
        found: Dict[str, Any] = next(
            r for r in self.page.state()["results"] if r["id"] == command_id
        )
        return found

    def turn_over(self) -> str:
        assert self.page.wait_until(
            lambda s: len(s["messages"]) >= 2 and not s["isLoading"]
        )
        user_ids = [m["id"] for m in self.page.state()["messages"] if m["role"] == "user"]
        return str(user_ids[-1])

    def close(self) -> None:
        # The server closes the page's stream when it stops.
        self.server.stop()
        self.controller.close()
        self.t2v.close()


@pytest.fixture
def chats(account_engine: AccountEngine) -> Iterator[Any]:
    made: List[Chat] = []

    def make(grace_s: float = 10.0) -> Chat:
        chat = Chat(account_engine, grace_s)
        made.append(chat)
        return chat

    yield make
    for chat in made:
        chat.close()


def test_a_timed_send_waits_for_the_page_report(chats: Any) -> None:
    chat = chats()
    chat.command(type="send", text="hello", timing=True, id="s1")
    turn_id = chat.turn_over()
    time.sleep(0.1)
    assert chat.timings == []  # held for the page's report
    chat.command(
        type="metrics", messageId=turn_id, sendToPostMs=2.5, firstOpToPaintMs=12.0,
        firstOpAtMs=time.time() * 1000.0, id="m1",
    )
    assert chat.result("m1")["ok"] is True
    assert wait_for(lambda: len(chat.timings) == 1)
    timing = chat.timings[0]
    assert timing.turn_id == turn_id and timing.outcome == "done"
    assert timing.ui is not None and timing.ui.reported
    assert timing.ui.send_to_post_ms == 2.5 and timing.ui.first_op_to_paint_ms == 12.0
    assert timing.ui.queue_ms is not None and 0 <= timing.ui.queue_ms < 5000
    assert timing.ui.delivery_ms is not None


def test_an_untimed_send_is_delivered_at_once_with_the_queue_wait(chats: Any) -> None:
    chat = chats(grace_s=60.0)
    chat.command(type="send", text="hello", id="s1")
    chat.turn_over()
    assert wait_for(lambda: len(chat.timings) == 1, timeout=2.0)
    ui = chat.timings[0].ui
    assert ui is not None and not ui.reported and ui.queue_ms is not None


def test_a_report_that_never_comes_only_delays_the_timing(chats: Any) -> None:
    chat = chats(grace_s=0.2)
    chat.command(type="send", text="hello", timing=True, id="s1")
    chat.turn_over()
    assert wait_for(lambda: len(chat.timings) == 1, timeout=3.0)
    assert chat.timings[0].ui is not None and not chat.timings[0].ui.reported


def test_a_report_for_an_unknown_turn_succeeds_and_changes_nothing(chats: Any) -> None:
    chat = chats()
    chat.command(type="metrics", messageId="msg_404", sendToPostMs=None, id="m1")
    assert chat.result("m1")["ok"] is True
    assert chat.timings == []


def test_a_controller_without_metrics_takes_both_commands() -> None:
    controller = FakeController(executor=lambda job: job(), reply="ok")
    host = GenericHost()
    app = AppState(None, host)
    router = CommandRouter(controller, app, host, UploadRegistry())
    try:
        sent = router.execute(parse_command({"type": "send", "text": "hi", "timing": True}))
        assert sent["ok"] is True
        user_id = controller.snapshot()["messages"][0]["id"]
        reported = router.execute(parse_command({"type": "metrics", "messageId": user_id}))
        assert reported["ok"] is True
    finally:
        router.stop()
