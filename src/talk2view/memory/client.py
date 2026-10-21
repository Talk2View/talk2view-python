"""The device's side of on-device memory (spec §7.4, §10.2): ``T2VMemory`` sends each turn's
memory field, applies the engine's ``memory`` events, answers ``search_memory`` from the
store, records each round in the store's log, and asks the engine to extract and to
consolidate.

Nothing here holds up a chat. The engine's memory calls run on one background thread
(``worker.py``; ``inline=True`` runs them in the caller, for tests and the eval), their
bodies are built by ``bodies.py``, and every failure is silent: one log line naming the
status or the exception's class, never memory text, and a later trigger tries again once
the failure's cooldown has run.
"""

from __future__ import annotations

import functools
import json
import logging
import threading
import uuid
import weakref
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from ..client import T2VClient
from ..errors import T2VError
from ..types import ChatEvent
from . import bodies, days
from .models import IDLE_EXTRACT_S, RESULT_LIMIT_MAX, AppliedBatch
from .ops import date_problem
from .search import result_json
from .store import MemoryStore, _timestamp, _utc, utc_now
from .worker import Cooldown, Ticker, Worker

logger = logging.getLogger("talk2view.memory")

# One engine memory call can take about 120 s: a 60 s model call, then one retry when the
# model's reply can't be parsed. The calls run in the background, so wait longer than that.
CALL_TIMEOUT_S = 180.0
# search_memory's arguments (spec §8.4).
SEARCH_LIMIT = 8
QUERY_MIN = 2
QUERY_MAX = 200

#: search_memory's answer when nothing is found, or nothing may be.
NO_RESULTS = result_json([])

_MODES = ("conversation", "import")

# The clients whose partner has memory off: the engine answered 403 memory_disabled. No
# extraction or consolidation goes out through them again in this process.
_partner_memory_off: "weakref.WeakSet[T2VClient]" = weakref.WeakSet()
_partner_memory_off_lock = threading.Lock()


class _Turn:
    """One turn as ``observe_turn`` follows it, across an approval's pause."""

    def __init__(self, user_text: Optional[str], conversation_id: str, live: bool) -> None:
        self.user_text = user_text  # None: the rest of a turn this memory never saw begin
        self.conversation_id = conversation_id
        self.live = live  # memory was on for it: its memory events apply
        self.reply: List[str] = []
        # Whether its round goes in the log when it is done: not after an error, nor once
        # it applied a forget or a remove (R67).
        self.keep = live and user_text is not None


class T2VMemory:
    """One user's on-device memory in use (spec §10.2): what ``Talk2View(memory_path=...)``
    makes for each signed-in user as ``t2v.memory``, over that user's ``MemoryStore``.

    Each turn ``Talk2View.chat`` calls ``run_due`` (it only queues work), sends
    ``turn_payload`` as the turn's ``memory`` field, answers ``search_memory`` with
    ``handle_search`` and passes the turn's events through ``observe_turn``, which applies
    each ``memory`` event and records the round when the turn is done. ``last_applied`` is
    what the latest ``memory`` event applied, and ``undo`` takes it back.

    ``conversation_id`` is the conversation in progress (32 hex characters, the shape the
    engine accepts); ``new_conversation`` starts another. ``timezone`` is the user's, an
    IANA name, the one the turn's ``Capabilities.timezone`` sends (R64); None means the
    system's zone.

    The engine calls (``extract``, ``consolidate``, ``import_text``) wait up to
    CALL_TIMEOUT_S, so ``run_due`` and ``new_conversation`` queue them on one background
    daemon thread and return at once, unless ``inline=True`` (tests and the eval: they run
    in the caller, in order). A call that fails returns False and logs one line, with the
    status or the exception's class and never memory text. The automatic calls then cool
    down (R81, ``worker.Cooldown``): none goes out for 60 s, twice as long after each
    further failure in a row, at most an hour and never less than the engine's
    ``Retry-After``; a call that succeeds ends the cooldown. Each queued call checks it
    when it starts (R86). It runs on ``clock``, or on the ``now`` given to ``run_due``. A
    guest (``is_anonymous()`` true) makes no call: the engine refuses them to guests. After
    a 403 ``memory_disabled`` none goes out through this client again in this process.

    Thread-safe. ``close`` stops it and closes the store.
    """

    def __init__(
        self,
        client: T2VClient,
        store: MemoryStore,
        *,
        clock: Callable[[], datetime] = utc_now,
        timezone: Optional[str] = None,
        inline: bool = False,
        is_anonymous: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.store = store
        self.timezone = timezone
        self.conversation_id = uuid.uuid4().hex
        self._client = client
        self._clock = clock
        self._is_anonymous = is_anonymous
        # Held for every use of the store, so close() never closes it under a read or a
        # write, and for the cooldown. Never held across an engine call.
        self._lock = threading.RLock()
        self._closed = False
        self._incognito = False
        self._launched = False  # run_due has run: its first run extracts every conversation
        self._import_pending = False  # an import wants its consolidation
        # The revision a consolidation left: until memory changes, another would only repeat
        # it (one that left entries over the caps, say).
        self._settled_revision: Optional[int] = None
        self._parked: Optional[_Turn] = None  # the turn an approval paused
        self._last_applied: Optional[AppliedBatch] = None
        self._cooldown = Cooldown()
        self._worker = Worker(inline=inline)
        self._ticker: Optional[Ticker] = None
        self._ticker_lock = threading.Lock()

    # ── state ──

    @property
    def enabled(self) -> bool:
        """Whether the engine's memory calls (extraction, consolidation, imports) still go
        out: False once closed, and once the engine has said the partner has memory off
        (403 ``memory_disabled``), for the rest of the process."""
        if self._closed:
            return False
        with _partner_memory_off_lock:
            return self._client not in _partner_memory_off

    @property
    def incognito(self) -> bool:
        """Whether the current conversation is incognito: its turns send only ``{"status":
        "incognito"}``, apply no memory events, record no rounds and find nothing when the
        agent searches. Set it before the conversation's first message (spec §10.1), or use
        ``new_conversation(incognito=True)``; set later, it deletes the rounds recorded so
        far, as the store's ``set_incognito`` does."""
        return self._incognito

    @incognito.setter
    def incognito(self, value: bool) -> None:
        with self._lock:
            self._mark_incognito(bool(value))

    @property
    def last_applied(self) -> Optional[AppliedBatch]:
        """What the turn's latest ``memory`` event applied, as one undoable batch (spec
        §10.1: the "Memory updated" chip, with its undo). It is set before that event is
        passed on, so a host that receives a ``memory`` event from ``chat()`` finds the
        event's batch here; ``undo(last_applied.id)`` takes it back within 30 s. None at the
        start of each turn, and after an event the memory didn't apply (paused, incognito,
        no ops, or the store failed). A batch whose ops were all dropped has an empty
        ``applied``."""
        return self._last_applied

    def undo(self, batch_id: str) -> bool:
        """Take back a batch the memory applied, such as ``last_applied``, through the
        store's ``undo``: True when undone. False, never raising, once closed, after the
        30 s window, for a batch undone already or overtaken by a later change, and when the
        store fails (logged)."""
        with self._lock:
            if self._closed:
                return False
            try:
                return self.store.undo(batch_id)
            except Exception as error:
                logger.warning("Memory: undoing a memory change failed (%s)", _name(error))
                return False

    def new_conversation(self, *, incognito: bool = False) -> str:
        """End the conversation in progress and start another, incognito or not; returns its
        id. The new one is switched to and marked in one step, so no turn ever finds it
        unmarked. The one that ended is queued for extraction (spec §7.4), never waited
        for."""
        with self._lock:
            previous = self.conversation_id
            self.conversation_id = current = uuid.uuid4().hex
            self._parked = None
            self._incognito = False
            if incognito:
                self._mark_incognito(True)
        self._queue_extraction(previous)
        return current

    def _mark_incognito(self, value: bool) -> None:
        """Under the lock: make the current conversation incognito, or not, here and in the
        store. When the store fails (logged), the flag holds all the same: an incognito
        conversation sends and keeps nothing either way."""
        self._incognito = value
        if self._closed:
            return
        try:
            self.store.set_incognito(self.conversation_id, value)
        except Exception as error:
            logger.warning("Memory: marking the chat incognito failed (%s)", _name(error))

    def close(self) -> None:
        """Stop: no more engine calls, events, rounds or searches, and the store is closed.
        It never waits for a call in flight: that call's result is dropped, and its
        conversation is extracted again at the next launch."""
        with self._ticker_lock:  # as start_background reads it: no timer starts after this
            if self._closed:
                return
            self._closed = True
        self.stop_background()
        with self._lock:
            self._worker.stop()
            self.store.close()

    # ── each turn ──

    def turn_payload(self, latest_user_message: str) -> Optional[Dict[str, Any]]:
        """The turn's ``memory`` field (spec §8.1): the store's ``turn_payload`` for the
        current conversation, in ``timezone``. Only ``{"status": "paused"}`` while memory is
        paused, and only ``{"status": "incognito"}`` in an incognito conversation. None when
        closed, or when the store can't be read (logged): the turn then goes without it."""
        with self._lock:
            if self._closed:
                return None
            try:
                if self.store.paused:
                    return {"status": "paused"}
                if self._incognito:
                    return {"status": "incognito"}
                field = self.store.turn_payload(
                    latest_user_message,
                    exclude_conversation=self.conversation_id,
                    timezone=self.timezone,
                )
            except Exception as error:
                logger.warning("Memory: reading the turn's memory failed (%s)", _name(error))
                return None
        if field["status"] != "on":
            return {"status": field["status"]}
        return field

    def handle_search(self, args: Dict[str, Any]) -> str:
        """Answer a ``search_memory`` call (spec §8.4) from the store, as its
        ``search_result_json``, leaving out the current conversation, which the agent has in
        context already. A null argument counts as left out (A7): ``limit`` is 8 by default
        and kept within 1 to 20 (R40), and ``query`` is cut to 200 code points. A malformed
        argument gets a short ``{"error": ...}`` result that never quotes memory; nothing
        raises. Paused, incognito or closed, nothing is found (NO_RESULTS)."""
        problem, query, from_date, to_date, limit = _search_arguments(args)
        if problem is not None:
            return json.dumps({"error": problem})
        with self._lock:
            try:
                if self._closed or self._incognito or self.store.paused:
                    return NO_RESULTS
                return self.store.search_result_json(
                    query,
                    from_date=from_date,
                    to_date=to_date,
                    limit=limit,
                    exclude_conversation=self.conversation_id,
                    timezone=self.timezone,
                )
            except Exception as error:
                logger.warning("Memory: a memory search failed (%s)", _name(error))
                return json.dumps({"error": "the memory search failed"})

    def on_event(self, event: ChatEvent) -> Optional[AppliedBatch]:
        """Apply a ``memory`` event's ops (spec §7.3) to the store as one undoable batch, and
        return it. None for any other event, for an event with no ops (R62: no empty batch),
        while memory is paused or the conversation incognito, when closed, or when the store
        fails (logged)."""
        if event.type != "memory" or not event.memory_ops:
            return None
        with self._lock:
            try:
                if self._closed or self._incognito or self.store.paused:
                    return None
                batch = self.store.apply_ops(event.memory_ops, source="said")
            except Exception as error:
                logger.warning("Memory: applying a memory event failed (%s)", _name(error))
                return None
        # Only counts and the rules that dropped ops: the records carry memory text.
        logger.debug(
            "Memory: a memory event applied %d ops and dropped %d%s",
            len(batch.applied),
            len(batch.dropped),
            _reasons(batch.dropped),
        )
        return batch

    def observe_turn(self, user_text: str, events: Iterator[ChatEvent]) -> Iterator[ChatEvent]:
        """Pass a turn's events through, following them. Each ``memory`` event is applied
        (``on_event``), and its batch made ``last_applied``, before it is passed on; the
        reply's ``text`` is gathered, and on ``done`` the round is recorded in the store's
        log, once. Never for a turn that met an ``error`` or was stopped, and never for one
        that applied a ``forget`` or a ``remove`` (R67): its own words name what was
        forgotten. A paused or incognito turn applies and records nothing. A turn an
        ``approval_required`` pauses goes on in ``observe_approval``."""
        with self._lock:
            turn = _Turn(user_text, self.conversation_id, self._live())
            self._parked = None
            self._last_applied = None
        yield from self._follow(turn, events)

    def observe_approval(self, events: Iterator[ChatEvent]) -> Iterator[ChatEvent]:
        """Pass through the events of ``Talk2View.respond_to_approval``, the rest of the turn
        an approval paused, following them as ``observe_turn`` does, so that turn's memory
        events apply and its round is recorded when it is done. With no paused turn to go
        on with, the events are applied and no round is recorded."""
        with self._lock:
            turn = self._parked or _Turn(None, self.conversation_id, self._live())
            self._parked = None
        yield from self._follow(turn, events)

    def _live(self) -> bool:
        """Under the lock: whether memory is on for a turn starting now."""
        try:
            return not (self._closed or self._incognito or self.store.paused)
        except Exception as error:
            logger.warning("Memory: reading the pause switch failed (%s)", _name(error))
            return False

    def _follow(self, turn: _Turn, events: Iterator[ChatEvent]) -> Iterator[ChatEvent]:
        for event in events:
            if event.type == "memory":
                batch = self.on_event(event) if turn.live else None
                with self._lock:
                    self._last_applied = batch  # R85: there when the host gets the event
                if batch is not None and any(
                    record.get("op") in ("forget", "remove") for record in batch.applied
                ):
                    turn.keep = False
            elif event.type == "text" and event.content:
                turn.reply.append(event.content)
            elif event.type == "error":
                turn.keep = False
            elif event.type == "done":
                self._record(turn)
            elif event.type == "approval_required":
                with self._lock:
                    self._parked = turn
            yield event

    def _record(self, turn: _Turn) -> None:
        """Record a finished turn's round, unless it must leave none (see observe_turn)."""
        if not turn.keep or turn.user_text is None:
            return
        with self._lock:
            try:
                if self._closed or self.store.paused:
                    return
                self.store.record_round(
                    turn.conversation_id, turn.user_text, "".join(turn.reply), at=self._clock()
                )
            except Exception as error:
                logger.warning("Memory: recording the round failed (%s)", _name(error))

    # ── triggers (spec §7.4) ──

    def run_due(self, now: Optional[datetime] = None) -> List[str]:
        """Queue what is due, and return its keys: ``"extract:<id>"`` for each conversation to
        extract, then ``"consolidate"``. Inline, they have run by the time it returns.
        Otherwise it only queues them and never waits; it never raises either.

        - The store's purge, always: hidden entries and rounds past the undo window go.
        - Extraction of each conversation with rounds extraction hasn't read that has been
          idle IDLE_EXTRACT_S (10 minutes) by ``now`` (the clock's by default); at the first
          check, at launch, of every one, idle or not. Never one queued or running already.
        - Consolidation, when the store says it is due (``needs_consolidation``) or an import
          wants one, unless memory hasn't changed since the last one.

        Nothing but the purge while memory is paused, for a guest, once the partner has
        memory off, or while a failed call's cooldown runs at ``now`` (R81). Each job it
        queues checks the cooldown again when it starts, and makes no call while it runs:
        the conversation stays due (R86). A call that fails starts the cooldown at ``now``
        when it was given, so a simulated clock never mixes with the real one."""
        try:
            return self._run_due(now)
        except Exception as error:
            logger.warning("Memory: checking what is due failed (%s)", _name(error))
            return []

    def _run_due(self, now: Optional[datetime]) -> List[str]:
        if self._closed:
            return []
        self._worker.run("purge", self._purge)
        if not self._may_call():
            return []
        moment = _utc(now if now is not None else self._clock())
        cutoff = _timestamp(moment - timedelta(seconds=IDLE_EXTRACT_S))
        with self._lock:
            if self._closed or self.store.paused or self._cooldown.cooling(moment):
                return []
            launch, self._launched = not self._launched, True
            idle = [
                record.id
                for record in self.store.unextracted()
                if launch or record.last_active_at <= cutoff
            ]
        due = [key for key in (self._queue_extract(cid, now) for cid in idle) if key is not None]
        with self._lock:
            consolidate = not self._closed and self._consolidation_due()
        job = functools.partial(self._consolidate_job, now)
        if consolidate and self._worker.run("consolidate", job):
            due.append("consolidate")
        return due

    def _consolidation_due(self) -> bool:
        """Under the lock: whether to consolidate now (see run_due)."""
        if self._import_pending:
            return True
        return self.store.revision() != self._settled_revision and self.store.needs_consolidation()

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait for the background thread to run everything queued: True once it has, False
        when ``timeout`` seconds pass first. For tests and shutdown; inline, nothing waits."""
        return self._worker.wait_idle(timeout)

    def start_background(self, interval_s: float = 60.0) -> None:
        """Call ``run_due`` every ``interval_s`` seconds on a daemon thread until
        ``stop_background`` or ``close``, for a host with no loop of its own to call it from.
        Off by default: ``Talk2View.chat`` calls ``run_due`` before every turn. Calling it
        while it runs, or once closed, does nothing."""
        if interval_s <= 0:
            raise ValueError("interval_s must be more than 0")
        with self._ticker_lock:
            if not self._closed and self._ticker is None:
                self._ticker = Ticker(self.run_due, interval_s)

    def stop_background(self) -> None:
        """Stop ``start_background``'s calls. It never waits: a ``run_due`` in progress
        finishes, and the thread ends."""
        with self._ticker_lock:
            ticker, self._ticker = self._ticker, None
        if ticker is not None:
            ticker.stop()

    def _queue_extraction(self, conversation_id: str) -> None:
        """Queue the extraction of the conversation that just ended (new_conversation)."""
        try:
            with self._lock:
                if self._closed or self.store.paused:
                    return
                record = self.store.conversation(conversation_id)
            if record is not None and not record.incognito and self._may_call():
                self._queue_extract(conversation_id, None)
        except Exception as error:
            logger.warning("Memory: queueing an extraction failed (%s)", _name(error))

    def _queue_extract(self, conversation_id: str, since: Optional[datetime]) -> Optional[str]:
        """Queue the conversation's extraction (``_extract_job``): its key, or None when one
        is queued or running already."""
        key = "extract:" + conversation_id
        job = functools.partial(self._extract_job, conversation_id, since)
        return key if self._worker.run(key, job) else None

    def _extract_job(self, conversation_id: str, since: Optional[datetime]) -> None:
        """An extraction run_due or new_conversation queued. It makes no call while a failed
        call's cooldown runs at ``since``, run_due's ``now``, or else now, and the
        conversation stays due (R86): a queue of them meeting a failing engine makes one
        call, not one each."""
        if not self._cooling(since):
            self._extract_safely(conversation_id, "conversation", since)

    def _consolidate_job(self, since: Optional[datetime] = None) -> None:
        """A consolidation run_due or an import queued: none while the cooldown runs, as for
        ``_extract_job``, and consolidation stays due."""
        if not self._cooling(since):
            self._consolidate_safely(since)

    def _cooling(self, since: Optional[datetime]) -> bool:
        with self._lock:
            return self._cooldown.cooling(self._moment(since))

    def _moment(self, since: Optional[datetime]) -> datetime:
        """``since``, a trigger's time, or else the clock's time now."""
        return _utc(since if since is not None else self._clock())

    def _purge(self) -> None:
        with self._lock:
            if not self._closed:
                self.store.purge()

    def _may_call(self) -> bool:
        """Whether an engine memory call may go out: open, the partner's memory not off, and
        the user no guest. A guest check that fails says no."""
        if not self.enabled:
            return False
        try:
            return self._is_anonymous is None or not self._is_anonymous()
        except Exception:
            return False

    def _today(self, timezone: Optional[str]) -> str:
        """The user's today in ``timezone``: a call passes the zone it began with, so its
        today, its ``timezone`` field and the days its facts expire on agree."""
        return days.today(_utc(self._clock()), timezone).isoformat()

    # ── engine calls ──

    def extract(self, conversation_id: str, *, mode: str = "conversation") -> bool:
        """One POST /v1/memory/extract (spec §8.5) for the conversation's rounds extraction
        hasn't read (``bodies.extract_body``), and its result applied: the facts, the entry
        ops as one batch, with source "inferred" ("imported" in import mode), and the
        summary; then the extracted point moves on. True when applied, or when there was
        nothing new from the user to send. False when the call failed (logged) or is
        refused (a guest, the partner's memory off, memory paused), when the conversation
        is unknown or incognito, or while another extraction of it is queued or running.
        It goes out during a failed call's cooldown too, and its outcome updates it.

        A result that arrives once the conversation has turned incognito is applied
        without its entry ops (the store drops the rest), and one that arrives once memory
        is paused or closed is dropped: a later trigger extracts again."""
        if mode not in _MODES:
            raise ValueError("mode must be 'conversation' or 'import'")
        key = "extract:" + conversation_id
        if not self._worker.claim(key):
            return False
        try:
            return self._extract_safely(conversation_id, mode)
        finally:
            self._worker.release(key)

    def _extract_safely(
        self, conversation_id: str, mode: str, since: Optional[datetime] = None
    ) -> bool:
        try:
            return self._extract(conversation_id, mode, since)
        except Exception as error:
            logger.warning("Memory: extraction failed (%s)", _name(error))
            return False

    def _extract(self, conversation_id: str, mode: str, since: Optional[datetime]) -> bool:
        if not self._may_call():
            return False
        timezone = self.timezone  # the request's, all through
        with self._lock:
            if self._closed or self.store.paused:
                return False
            record = self.store.conversation(conversation_id)
            if record is None or record.incognito:
                return False
            body, through = bodies.extract_body(
                self.store, record, mode=mode, today=self._today(timezone), timezone=timezone
            )
            if body is None:
                if through > record.extracted_through:
                    self.store.mark_extracted(record.id, through)
                return True
        data = self._call("extraction", "/v1/memory/extract", body, _extraction_answer, since)
        return data is not None and self._apply_extraction(data, record.id, through, mode)

    def _apply_extraction(
        self, data: Dict[str, Any], conversation_id: Optional[str], through: int, mode: str
    ) -> bool:
        """Apply an extract answer (its shape checked by ``_extraction_answer``) to the
        conversation, or with ``conversation_id`` None (an import) to none."""
        summary, facts, ops = data.get("summary"), data["facts"], data["entry_ops"]
        source = "imported" if mode == "import" else "inferred"
        with self._lock:
            if self._closed or self.store.paused:
                return False
            keep_ops = True
            if conversation_id is not None:
                record = self.store.conversation(conversation_id)
                if record is None:  # deleted while the call ran
                    return False
                keep_ops = not record.incognito  # apply_ops isn't keyed by conversation
            recorded = self.store.add_facts(facts, conversation_id=conversation_id, source=source)
            batch = self.store.apply_ops(ops, source=source) if ops and keep_ops else None
            if conversation_id is not None:
                if isinstance(summary, dict):
                    title, text = summary.get("title"), summary.get("text")
                    if isinstance(title, str) and isinstance(text, str):
                        self.store.set_summary(conversation_id, title, text)
                self.store.mark_extracted(conversation_id, through)
        logger.debug(
            "Memory: extraction recorded %d facts and applied %d entry ops, dropping %d%s",
            len(recorded),
            len(batch.applied) if batch is not None else 0,
            len(batch.dropped) if batch is not None else len(ops),
            _reasons(batch.dropped) if batch is not None else "",
        )
        return True

    def consolidate(self) -> bool:
        """One POST /v1/memory/consolidate (spec §8.6) built from the store's
        ``consolidation_snapshot`` (``bodies.consolidate_body``), and its result applied
        through ``apply_consolidation`` only: all of it, or nothing when memory changed
        while the call ran, which leaves consolidation due (logged with counts). True when
        applied. False when the call failed (logged) or is refused (a guest, the partner's
        memory off, memory paused), when the result wasn't applied, or while another
        consolidation is queued or running. It goes out during a failed call's cooldown
        too, and its outcome updates it."""
        if not self._worker.claim("consolidate"):
            return False
        try:
            return self._consolidate_safely()
        finally:
            self._worker.release("consolidate")

    def _consolidate_safely(self, since: Optional[datetime] = None) -> bool:
        try:
            return self._consolidate(since)
        except Exception as error:
            logger.warning("Memory: consolidation failed (%s)", _name(error))
            return False

    def _consolidate(self, since: Optional[datetime]) -> bool:
        if not self._may_call():
            return False
        timezone = self.timezone  # the request's: its today, and the days facts expire on
        with self._lock:
            if self._closed or self.store.paused:
                return False
            snapshot = self.store.consolidation_snapshot()
        body = bodies.consolidate_body(snapshot, today=self._today(timezone), timezone=timezone)
        data = self._call(
            "consolidation", "/v1/memory/consolidate", body, _consolidation_answer, since
        )
        if data is None:
            return False
        entries = data["entries"]
        merges, expiries = data.get("merge_facts", []), data.get("expire_facts", [])
        with self._lock:
            if self._closed or self.store.paused:
                return False
            applied = self.store.apply_consolidation(
                entries, merges, expiries, snapshot=snapshot, timezone=timezone
            )
            if applied:
                self._import_pending = False
                self._settled_revision = self.store.revision()
        if not applied:
            logger.info(
                "Memory: memory changed during consolidation, so its result (%d entries) was"
                " not applied; consolidation stays due",
                len(entries),
            )
            return False
        logger.debug(
            "Memory: consolidation applied %d entries, %d fact merges and %d expiries",
            len(entries),
            len(merges),
            len(expiries),
        )
        return True

    def import_text(self, text: str) -> bool:
        """Import memory the user exported from another assistant (spec §9 rule 10): one
        POST /v1/memory/extract in import mode (``bodies.import_body``), with the text as
        the user's one message and the store's "Include sensitive topics" switch. Its facts
        and entry ops are applied with source "imported", tied to no conversation, and a
        consolidation follows (queued, unless inline). True when applied. False when the
        call failed (logged) or is refused (a guest, the partner's memory off, memory
        paused), and for text that is blank or longer than bodies.EXTRACT_CHARS_MAX
        (200,000) code points. It goes out during a failed call's cooldown too, and its
        outcome updates it."""
        try:
            return self._import(text)
        except Exception as error:
            logger.warning("Memory: import failed (%s)", _name(error))
            return False

    def _import(self, text: str) -> bool:
        if not text.strip():
            return False
        if len(text) > bodies.EXTRACT_CHARS_MAX:
            logger.warning(
                "Memory: import failed (%d code points, over the %d one call takes)",
                len(text),
                bodies.EXTRACT_CHARS_MAX,
            )
            return False
        if not self._may_call():
            return False
        timezone = self.timezone  # the request's, all through
        with self._lock:
            if self._closed or self.store.paused:
                return False
            body = bodies.import_body(
                self.store,
                uuid.uuid4().hex,
                text,
                now=_timestamp(self._clock()),
                today=self._today(timezone),
                timezone=timezone,
            )
        data = self._call("import", "/v1/memory/extract", body, _extraction_answer, None)
        if data is None or not self._apply_extraction(data, None, 0, "import"):
            return False
        with self._lock:
            self._import_pending = True
        self._worker.run("consolidate", self._consolidate_job)
        return True

    def _call(
        self,
        what: str,
        endpoint: str,
        body: Dict[str, Any],
        well_formed: Callable[[Dict[str, Any]], bool],
        since: Optional[datetime],
    ) -> Optional[Dict[str, Any]]:
        """POST ``body`` to an engine memory route: its JSON answer, or None.

        Once closed, None at once and nothing sent (R82): the body may hold the memory of a
        user who has just signed out. A failure (an error, a timeout, or an answer that is
        no JSON object or that ``well_formed`` refuses) logs one line with the status and
        the error type, or the exception's class, never its message, which could quote the
        request, and counts against the cooldown at ``since``, run_due's ``now``, or else
        now; a 403 ``memory_disabled`` turns memory calls off instead. A success ends the
        cooldown. A call the close cut off changes nothing and logs nothing."""
        if self._closed:
            return None
        cools, retry_after = True, None
        try:
            data = self._client.request(endpoint, method="POST", body=body, timeout=CALL_TIMEOUT_S)
        except T2VError as error:
            if error.type == "memory_disabled":
                cools = False
                with _partner_memory_off_lock:
                    _partner_memory_off.add(self._client)
            problem, retry_after = _failure(error), error.retry_after
        except Exception as error:
            problem = _name(error)
        else:
            if isinstance(data, dict) and well_formed(data):
                with self._lock:
                    if not self._closed:
                        self._cooldown.recovered()
                return data
            problem = "malformed answer"
        with self._lock:
            if self._closed:
                return None
            if cools:
                self._cooldown.failed(self._moment(since), retry_after)
        logger.warning("Memory: %s failed (%s)", what, problem)
        return None


def _extraction_answer(data: Dict[str, Any]) -> bool:
    """Whether an extract answer can be applied: its facts and its entry ops are lists."""
    return isinstance(data.get("facts"), list) and isinstance(data.get("entry_ops"), list)


def _consolidation_answer(data: Dict[str, Any]) -> bool:
    """Whether a consolidate answer can be applied: its entries are a list, and so are its
    fact merges and expiries when it has them."""
    return isinstance(data.get("entries"), list) and all(
        isinstance(data.get(key, []), list) for key in ("merge_facts", "expire_facts")
    )


def _search_arguments(
    args: Any,
) -> Tuple[Optional[str], str, Optional[str], Optional[str], int]:
    """``search_memory``'s arguments, checked (see handle_search): ``(problem, query,
    from_date, to_date, limit)``, ``problem`` a short message when one is malformed."""
    if not isinstance(args, dict):
        return "the arguments must be an object", "", None, None, SEARCH_LIMIT
    query = args.get("query")
    if not isinstance(query, str):
        return "query must be text", "", None, None, SEARCH_LIMIT
    query = query[:QUERY_MAX]
    if len(query) < QUERY_MIN:
        return "query must be at least 2 characters", "", None, None, SEARCH_LIMIT
    from_date, to_date = args.get("from_date"), args.get("to_date")
    for name, value in (("from_date", from_date), ("to_date", to_date)):
        if date_problem(value):
            return name + " must be a YYYY-MM-DD date", "", None, None, SEARCH_LIMIT
    limit = args.get("limit")
    if limit is None:
        limit = SEARCH_LIMIT
    elif isinstance(limit, bool) or not isinstance(limit, int):
        return "limit must be a whole number", "", None, None, SEARCH_LIMIT
    return None, query, from_date, to_date, max(1, min(limit, RESULT_LIMIT_MAX))


def _name(error: BaseException) -> str:
    """An exception, for the log: its class, never its message."""
    return type(error).__name__


def _failure(error: T2VError) -> str:
    """A failed call, for the log: its status and the engine's error type."""
    if error.status_code is None:
        return error.type or _name(error)
    return "HTTP {} {}".format(error.status_code, error.type or "error")


def _reasons(dropped: List[str]) -> str:
    """The rules that dropped ops, for the log: rule names only."""
    return " (" + ", ".join(sorted(set(dropped))) + ")" if dropped else ""


__all__ = ["NO_RESULTS", "T2VMemory"]
