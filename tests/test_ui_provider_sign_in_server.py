"""Provider sign-in through the real UIServer, over HTTP and SSE, with a GenericHost pumped
on its own "main" thread. Every wait is bounded."""

from __future__ import annotations

import http.client
import json
import threading
import time
from typing import Any, Dict, Iterator, List

import pytest

from talk2view import ChatController, Talk2View
from talk2view.ui import GenericHost, UIServer

from .fake_engine import follow_in_browser
from .test_ui_e2e import STUB
from .ui_engine import AccountEngine
from .ui_helpers import EventStream


class Pumped:
    """A GenericHost pumped on its own thread; ``busy`` set, the main thread stops pumping
    (a load or a modal in the host)."""

    def __init__(self) -> None:
        self.follow = True
        self.opened: List[str] = []
        self.busy = threading.Event()
        built = threading.Event()
        hosts: List[GenericHost] = []

        def opener(url: str) -> Any:
            self.opened.append(url)
            if self.follow:
                threading.Thread(target=follow_in_browser, args=(url,), daemon=True).start()
            return True

        def main() -> None:
            hosts.append(GenericHost(open_url=opener))
            built.set()
            host = hosts[0]
            while not host._stop.is_set():
                if not self.busy.is_set():
                    host.pump()
                time.sleep(0.005)

        self.thread = threading.Thread(target=main, daemon=True)
        self.thread.start()
        assert built.wait(2)
        self.host = hosts[0]

    def close(self) -> None:
        self.host.stop()
        self.thread.join(2)


class World:
    def __init__(self, engine: AccountEngine) -> None:
        engine.desktop_providers = ["apple", "google"]
        self.pumped = Pumped()
        self.t2v = Talk2View("pk_test", base_url=engine.url())
        self.controller = ChatController(self.t2v)
        self.server = UIServer(self.controller, self.pumped.host, static_dir=STUB)
        self.server.start()
        self.port = self.server.port
        self.token = self.server.url.split("#t=", 1)[1]
        self.page = EventStream(self.port, self.token)
        self.hung = False  # a deadlock: teardown would hang on the stuck locks
        assert self.page.wait_until(lambda s: s["capabilities"]["oauth"] is True)

    def post(self, body: Dict[str, Any]) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/command", body=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status

    def stop_bounded(self, limit: float = 8.0) -> None:
        stopper = threading.Thread(target=self.server.stop, daemon=True)
        stopper.start()
        stopper.join(limit)
        if stopper.is_alive():
            self.hung = True
            pytest.fail("UIServer.stop() hung")

    def close(self) -> None:
        if not self.hung:
            self.stop_bounded()
            self.controller.close()
            self.t2v.close()
        self.pumped.close()


@pytest.fixture
def world(account_engine: AccountEngine) -> Iterator[World]:
    w = World(account_engine)
    yield w
    w.close()


def test_a_host_listener_stopping_the_server_while_login_cancels_never_deadlocks(
    world: World,
) -> None:
    # The router's login is inside cancel(), waiting for the store; the store's
    # thread runs a host listener that stops the server ("close the chat panel once
    # signed in"), whose close() must not wait for the same cancel.
    w = world
    in_notify, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def holds_the_store(user: Any) -> None:
        if user is not None and user.id == "u-desktop" and not in_notify.is_set():
            in_notify.set()
            release.wait(5)

    def host_listener(user: Any) -> None:
        if user is not None and user.id == "u-desktop":
            w.server.stop()
            stopped.set()

    w.t2v.auth._add_listener(holds_the_store, early=True)
    w.t2v.auth.on_auth_state_change(host_listener)
    assert w.post({"type": "oauthStart", "provider": "apple", "id": "o1"}) == 202
    assert in_notify.wait(5)
    assert w.post({"type": "login", "email": "ana@example.com", "password": "pw",
                   "id": "l1"}) == 202
    time.sleep(0.3)  # the router is inside the token's cancel, waiting for the store
    release.set()
    if not stopped.wait(8):
        w.hung = True
        pytest.fail("the host listener's UIServer.stop() never returned")
    # The login ran after the store it waited for: the end-user's choice stays.
    assert wait_until(lambda: w.t2v.auth.is_authenticated()
                      and w.t2v.auth.get_user().id == "user-ana")  # type: ignore[union-attr]


@pytest.mark.parametrize("how", ["cancel", "stop"])
def test_a_host_busy_until_after_a_cancel_never_opens_the_browser(world: World,
                                                                  how: str) -> None:
    # The posted open runs after the attempt ended: it must not open anything.
    w = world
    w.pumped.busy.set()
    w.pumped.follow = False
    assert w.post({"type": "oauthStart", "provider": "apple", "id": "o1"}) == 202
    assert w.page.wait_until(lambda s: s["auth"]["oauthPending"] is not None)
    time.sleep(0.3)  # the sign-in waits for the host's answer
    if how == "cancel":
        assert w.post({"type": "oauthCancel", "id": "c1"}) == 202
        assert w.page.wait_until(lambda s: s["auth"]["oauthPending"] is None)
    else:
        w.stop_bounded()
    w.pumped.busy.clear()
    time.sleep(0.5)  # the main thread runs what was posted
    assert w.pumped.opened == [], "the browser opened for an attempt that had ended"


def wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True
