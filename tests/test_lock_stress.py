"""Opt-in stress probe of the cross-class lock graph (from the final review's probe_lock_stress).

A real ``Talk2View`` with a real ``T2VSkills`` (a real ``threading.Timer`` debounce) and a
real ``ChatController`` on its worker-thread executor. Actor threads mutate, flush, register
and list skills; send, cancel, clear and snapshot the controller; and sign in and out, so the
controller's auth listener runs against everything else. A subscriber on the emitting thread
reaches back into the skills (``skills.state()`` and ``skills.add()`` from inside a delivery).
That covers every edge the controller adds: its ``_lock`` -> the skills ``_lock`` (the slice
is read live under it), its ``_emit_lock`` -> ``_lock``, and the auth listener taking both.

A round fails on a deadlock (an actor still alive after its bounded join), an actor raising
anything but ``ControllerBusyError`` from ``send``, a controller that never goes idle, a
skills slice that is not ``skills.state()`` at the end, or batches that do not replay onto
the first snapshot. Not part of the default run (timing-dependent; ~0.6 s a round)::

    T2V_STRESS=1 uv run pytest tests/test_lock_stress.py -v
"""

from __future__ import annotations

import faulthandler
import os
import random
import sys
import threading
import time
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import MagicMock

import pytest

from talk2view import Talk2View
from talk2view.cancel import CancelToken
from talk2view.controller import ChatController, ControllerBusyError
from talk2view.errors import T2VError
from talk2view.skills import Skill, T2VSkills
from talk2view.types import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    TokenResponse,
    User,
)

from .controller_fakes import BatchLog

pytestmark = pytest.mark.stress

ROUNDS = 10
ROUND_SECONDS = 0.5
JOIN_SECONDS = 10.0


def _chunk(content: Optional[str] = None, finish: Optional[str] = None) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="t",
        model="m",
        thread_id="th",
        choices=[
            ChatCompletionChunkChoice(
                delta=ChatCompletionChunkDelta(content=content), finish_reason=finish
            )
        ],
    )


def one_round(seed: int) -> List[str]:
    rng = random.Random(seed)  # noqa: S311 — deterministic per seed, not cryptography
    rng_lock = threading.Lock()

    def chance() -> float:
        with rng_lock:
            return rng.random()

    problems: List[str] = []
    t2v = Talk2View("pk_test_123", base_url="http://localhost:9999")
    t2v._client.close()
    client = MagicMock()
    t2v._client = client
    t2v.tools._client = client
    t2v.skills = T2VSkills(client, t2v.data_storage, debounce_s=0.002)
    engine_lock = threading.Lock()
    sessions = [0]
    merging = [False]

    def request(endpoint: str, method: str = "GET", **kwargs: Any) -> Dict[str, Any]:
        time.sleep(chance() * 0.002)
        with engine_lock:
            if endpoint == "/v1/sessions":
                sessions[0] += 1
                return {"session_id": "s{}".format(sessions[0]), "thread_id": "th", "model": "m"}
            if endpoint == "/v1/skills/register":
                if chance() < 0.15:
                    raise T2VError("rate limited", "rate_limit_exceeded", 429)
                names = [s["name"] for s in kwargs["body"]["skills"]]
                answer: Dict[str, Any] = {"registered": names, "count": len(names)}
                if not merging[0]:
                    answer["replaced"] = True
                return answer
            if endpoint == "/v1/skills":
                return {"skills": [{"name": "p1", "source": "partner", "shadowed": False}]}
        return {}

    def stream(
        endpoint: str, body: Dict[str, Any], cancel: Optional[CancelToken] = None
    ) -> Iterator[ChatCompletionChunk]:
        def gen() -> Iterator[ChatCompletionChunk]:
            for _ in range(int(chance() * 4)):
                time.sleep(chance() * 0.002)
                yield _chunk("x")
            yield _chunk(finish="stop")

        return gen()

    client.request.side_effect = request
    client.stream_request.side_effect = stream
    controller = ChatController(t2v)
    log = BatchLog()
    controller.subscribe(log)
    start = controller.snapshot()

    def reach_back(_batch: Any) -> None:
        # On the emitting thread, inside the delivery window.
        if chance() < 0.05:
            t2v.skills.state()
        if chance() < 0.01:
            t2v.skills.add(Skill(name="cb{}".format(int(chance() * 3)), content="c"))

    controller.subscribe(reach_back)
    stop = threading.Event()

    def guarded(name: str, body: Any) -> Any:
        def run() -> None:
            while not stop.is_set():
                try:
                    body()
                except ControllerBusyError:
                    pass
                except Exception as exc:  # noqa: BLE001 — every other exception is a finding
                    problems.append("{}: {!r}".format(name, exc))
                time.sleep(chance() * 0.002)

        return threading.Thread(target=run, name=name, daemon=True)

    def skills_step() -> None:
        r = chance()
        name = "k{}".format(int(chance() * 6))
        if r < 0.3:
            t2v.skills.add(Skill(name=name, content="c" * (1 + int(chance() * 50))))
        elif r < 0.5:
            t2v.skills.remove(name)
        elif r < 0.65:
            try:
                t2v.skills.set_enabled(name, chance() < 0.5)
            except KeyError:
                pass
        elif r < 0.75:
            t2v.skills.flush()
        elif r < 0.8:
            try:
                t2v.skills.register()
            except T2VError:
                pass
        elif r < 0.85:
            t2v.skills.list_platform()
        elif r < 0.9:
            t2v.skills.save()
            t2v.skills.load()
        else:
            merging[0] = chance() < 0.3

    def chat_step() -> None:
        r = chance()
        if r < 0.5:
            controller.send("hi")
        elif r < 0.65:
            controller.cancel()
        elif r < 0.75:
            controller.clear()
        else:
            controller.snapshot()

    def auth_step() -> None:
        r = chance()
        if r < 0.4:
            n = int(chance() * 3)
            anonymous = chance() < 0.5
            user = User(id="{}-{}".format("anon" if anonymous else "user", n), email="")
            t2v.auth._store_tokens(
                TokenResponse(
                    access_token="tok", refresh_token="ref", user=user, is_anonymous=anonymous
                )
            )
            t2v.auth._notify_listeners(user)
        elif r < 0.6:
            t2v.auth.logout()  # the closed client's POST fails; logout clears and notifies
        time.sleep(chance() * 0.01)

    actors = [guarded("skills-{}".format(k), skills_step) for k in range(3)]
    actors.append(guarded("chat", chat_step))
    actors.append(guarded("auth", auth_step))
    for actor in actors:
        actor.start()
    time.sleep(ROUND_SECONDS)
    stop.set()
    for actor in actors:
        actor.join(JOIN_SECONDS)
        if actor.is_alive():
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            return problems + ["DEADLOCK: {} still alive".format(actor.name)]
    if not controller.wait_idle(JOIN_SECONDS):
        problems.append("controller never idle")
    t2v.skills.flush()
    time.sleep(0.05)
    controller.wait_idle(JOIN_SECONDS)
    live = t2v.skills.state()
    snapshot = controller.snapshot()
    if snapshot["skills"] != live:
        problems.append("slice != skills.state():\n  {}\n  {}".format(snapshot["skills"], live))
    if log.replay(start) != snapshot:
        problems.append("the batches do not replay onto the first snapshot")
    controller.close()
    t2v.close()
    return problems


@pytest.mark.skipif(os.environ.get("T2V_STRESS") != "1", reason="set T2V_STRESS=1 to run")
@pytest.mark.parametrize("seed", range(ROUNDS))
def test_the_lock_graph_holds_under_random_concurrent_use(seed: int) -> None:
    assert one_round(seed) == []
