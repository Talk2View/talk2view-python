"""Dictation for the chat page: the TS dictation adapter (``assistant-ui/dictation.ts``)
run on the server side, over the host's ``AudioCapture``.

``start()`` opens the microphone and, when nobody is signed in and guests are allowed,
starts the guest session and warms its key on threads of their own while the end-user
talks (TS ``listen()``: the two are kept apart; a short clip must not wait for the mint).
``stop()`` closes the microphone and hands the clip to a ``t2v-dictation`` thread, which
transcribes it with the end-user's voice model and language (``preferences``), else the
partner's ``default_stt_model``, else ``DEFAULT_DICTATION_MODEL``, and records the
transcript in the ``dictation`` slice for the page to append once. ``cancel()`` drops the
clip. Every phase and failure is in the slice; nothing here raises to the page but a
``busy`` start. A clip still running after ``MAX_CLIP_SECONDS`` is stopped by a timer, as
if the end-user had pressed stop, so nothing is lost to an open mic; the page keeps
showing "listening" the whole time. That transcript's ``reason`` is ``"max_duration"``
(else ``None``), so the page can show its own brief notice for the auto-stop case — TS
has no such cap, so there is nothing to match there, only a rule this SDK adds.

Threads: ``start``/``stop``/``cancel`` run on the router thread, the capture records on
the host library's thread, the request on ``t2v-dictation``, and the auto-stop timer fires
``stop()`` from its own thread (never the router's). ``_lock`` is a leaf: it is never held
across the capture, the request, or a call into ``Talk2View``. ``start()`` and ``stop()``
each re-check ``_closed``/the generation after their host call returns, because
``capture.start()``/``capture.stop()`` run with the lock released and a concurrent
``close()`` (from ``UIServer.stop()``, a different thread) cannot see a recording that
has not been marked ``listening`` yet, or one already past it: the re-check is what cancels
a capture ``close()`` missed, and what drops a clip already off the wire before it can be
uploaded to a server that has stopped.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Dict, Optional

from ..audio import (
    DEFAULT_DICTATION_MODEL,
    MAX_CLIP_BYTES,
    MAX_CLIP_SECONDS,
    AudioCapture,
    CaptureError,
)
from ..errors import T2VError
from .hub import EventHub
from .wire import CommandFailure

if TYPE_CHECKING:
    from .. import Talk2View

__all__ = ["Dictation"]

logger = logging.getLogger("talk2view.ui")

IDLE, LISTENING, TRANSCRIBING = "idle", "listening", "transcribing"
_NOT_SIGNED_IN = "Sign in, or send a message first, to dictate."
_TOO_LARGE = "The recording is larger than the engine allows (max 25 MB)."


class Dictation:
    def __init__(
        self, capture: AudioCapture, t2v: Optional["Talk2View"], *, allow_anonymous: bool
    ) -> None:
        self._capture = capture
        self._t2v = t2v
        self._allow_anonymous = allow_anonymous
        self._lock = threading.Lock()
        self._hub: Optional[EventHub] = None
        self._phase = IDLE
        self._transcript: Optional[Dict[str, Any]] = None
        self._error: Optional[Dict[str, str]] = None
        self._seq = 0
        self._generation = 0  # +1 per start/cancel/close: a transcription from before is dropped
        self._closed = False
        self._timer: Optional[threading.Timer] = None  # the MAX_CLIP_SECONDS auto-stop

    def attach(self, hub: EventHub) -> None:
        with self._lock:
            self._hub = hub
            self._publish_locked()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._generation += 1
            recording = self._phase == LISTENING
            self._phase = IDLE
            timer, self._timer = self._timer, None
            self._hub = None
        if timer is not None:
            timer.cancel()
        if recording:
            self._capture.cancel()

    # ── Commands (router thread) ──

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise CommandFailure("closed", "This chat has stopped.")
            if self._phase != IDLE:
                raise CommandFailure("busy", "A dictation is already in progress.")
            self._generation += 1
            generation = self._generation
            self._transcript = None
            self._error = None
        try:
            self._capture.start()
        except CaptureError as exc:
            self._fail(exc.type, exc.message, generation)
            return
        except Exception as exc:
            logger.exception("the host's audio capture failed to start")
            self._fail(
                "capture_failed",
                "The microphone could not be started ({}).".format(type(exc).__name__),
                generation,
            )
            return
        # capture.start() ran with the lock released: a close() (a different thread; start/
        # stop/cancel are the router's alone) may have landed while it was still recording,
        # and could not see it as "listening" yet to cancel it. Catch that here, or this
        # capture is never stopped.
        timer: Optional[threading.Timer] = None
        with self._lock:
            stale = self._closed or generation != self._generation
            if not stale:
                self._phase = LISTENING
                timer = threading.Timer(MAX_CLIP_SECONDS, self._auto_stop, args=(generation,))
                timer.daemon = True
                self._timer = timer
                self._publish_locked()
        if stale or timer is None:  # timer is always set when stale is False
            self._capture.cancel()
            return
        timer.start()
        # A signed-out visitor's first action can be the mic: start the guest and mint the
        # key now, while they talk. Off this thread, and never waited on.
        t2v = self._t2v
        if t2v is not None:
            threading.Thread(
                target=self._warm, args=(t2v,), name="t2v-dictation-warm", daemon=True
            ).start()

    def _auto_stop(self, generation: int) -> None:
        """``MAX_CLIP_SECONDS`` elapsed: stop the clip as if the end-user had pressed stop,
        so nothing recorded is lost outright. Runs on the ``Timer``'s own thread; a
        ``stop``/``cancel``/``close`` that got there first already moved the phase or the
        generation, so this is then a no-op."""
        with self._lock:
            if self._closed or generation != self._generation or self._phase != LISTENING:
                return
        self.stop(auto=True)

    def stop(self, *, auto: bool = False) -> None:
        with self._lock:
            if self._phase != LISTENING:
                return
            self._phase = TRANSCRIBING
            generation = self._generation
            timer, self._timer = self._timer, None
            self._publish_locked()
        if timer is not None:
            timer.cancel()
        try:
            clip = self._capture.stop()
        except CaptureError as exc:
            self._fail(exc.type, exc.message, generation)
            return
        except Exception as exc:
            logger.exception("the host's audio capture failed to stop")
            self._fail(
                "capture_failed",
                "The recording could not be read ({}).".format(type(exc).__name__),
                generation,
            )
            return
        # capture.stop() also ran with the lock released: a close() that landed meanwhile
        # already told the page nothing more is coming, so the clip is dropped here rather
        # than uploaded to a server that has stopped.
        with self._lock:
            stale = self._closed or generation != self._generation
        if stale:
            return
        if len(clip) > MAX_CLIP_BYTES:
            self._fail("file_too_large", _TOO_LARGE, generation)
            return
        threading.Thread(
            target=self._transcribe,
            args=(clip, generation, auto),
            name="t2v-dictation",
            daemon=True,
        ).start()

    def cancel(self) -> None:
        with self._lock:
            if self._phase == IDLE:
                return
            recording = self._phase == LISTENING
            self._generation += 1
            self._phase = IDLE
            timer, self._timer = self._timer, None
            self._publish_locked()
        if timer is not None:
            timer.cancel()
        if recording:
            self._capture.cancel()

    # ── Threads of their own ──

    def _warm(self, t2v: "Talk2View") -> None:
        try:
            if not t2v.auth.is_authenticated():
                if not self._allow_anonymous:
                    return
                t2v.ensure_session()
            t2v.warm_up()
        except Exception:
            logger.debug("dictation's early session start or warm-up failed", exc_info=True)

    def _transcribe(self, clip: bytes, generation: int, auto: bool = False) -> None:
        t2v = self._t2v
        try:
            if t2v is None or not t2v.auth.is_authenticated():
                if t2v is not None and self._allow_anonymous and t2v.ensure_session():
                    pass
                else:
                    raise T2VError(_NOT_SIGNED_IN, type="sign_in_required")
            prefs = t2v.preferences.get()
            model = prefs.stt_model or self._partner_default(t2v) or DEFAULT_DICTATION_MODEL
            result = t2v.voice.transcribe(
                clip, model=model, language=prefs.stt_language or None, filename="recording.wav"
            )
            text = result.text.strip()
        except T2VError as exc:
            self._fail(exc.type or "transcription_failed", str(exc), generation)
            return
        except Exception as exc:
            logger.exception("transcription failed")
            self._fail(
                "transcription_failed",
                "The recording could not be transcribed ({}).".format(type(exc).__name__),
                generation,
            )
            return
        with self._lock:
            if generation != self._generation or self._closed:
                return  # cancelled, or a newer dictation started
            self._phase = IDLE
            if text:
                self._seq += 1
                # "reason" tells the page why the clip stopped (a "Stopped at 10
                # minutes" notice for the auto-stop case, TS has no cap so this is Python-
                # only) without giving the page its own duplicate MAX_CLIP_SECONDS timer.
                self._transcript = {
                    "seq": self._seq,
                    "text": text,
                    "reason": "max_duration" if auto else None,
                }
            self._publish_locked()

    @staticmethod
    def _partner_default(t2v: "Talk2View") -> Optional[str]:
        try:
            return t2v.get_config().default_stt_model or None
        except Exception:
            return None

    # ── The slice ──

    def _fail(self, error_type: str, message: str, generation: Optional[int] = None) -> None:
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            self._phase = IDLE
            self._transcript = None
            self._error = {"type": error_type, "message": message}
            self._publish_locked()

    def _publish_locked(self) -> None:
        if self._hub is not None:
            self._hub.set_slice("dictation", {
                "phase": self._phase,
                "transcript": dict(self._transcript) if self._transcript is not None else None,
                "error": dict(self._error) if self._error is not None else None,
            })
