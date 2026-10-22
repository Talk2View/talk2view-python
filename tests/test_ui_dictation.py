"""talk2view.ui.dictation: the TS dictation adapter's flow, server side."""

from __future__ import annotations

import struct
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

import pytest

from talk2view import Talk2View
from talk2view.audio import CaptureError, pcm16_to_wav
from talk2view.ui import dictation as dictation_module
from talk2view.ui.dictation import Dictation
from talk2view.ui.hub import EventHub
from talk2view.ui.wire import CommandFailure

from .ui_engine import AccountEngine
from .ui_helpers import wait_for

TONE = pcm16_to_wav(struct.pack("<8h", 0, 3000, -3000, 3000, -3000, 3000, -3000, 0), 16000)


class FakeCapture:
    def __init__(self, clip: bytes = TONE, fail_start: Optional[CaptureError] = None) -> None:
        self.clip = clip
        self.fail_start = fail_start
        self.calls: List[str] = []
        self.threads: List[str] = []

    def start(self) -> None:
        self.calls.append("start")
        self.threads.append(threading.current_thread().name)
        if self.fail_start is not None:
            raise self.fail_start

    def stop(self) -> bytes:
        self.calls.append("stop")
        return self.clip

    def cancel(self) -> None:
        self.calls.append("cancel")

    def level(self) -> float:
        return 0.5


class GatedCapture:
    """A fake ``AudioCapture`` whose ``start()``/``stop()`` block until the test releases
    them, so a ``close()`` (a different thread: ``UIServer.stop()``) can land while the
    host call is still in flight, and the race ``close()`` alone cannot see is exercised
    directly rather than by luck."""

    def __init__(self, clip: bytes = TONE) -> None:
        self.clip = clip
        self.calls: List[str] = []
        self.entered_start = threading.Event()
        self.release_start = threading.Event()
        self.entered_stop = threading.Event()
        self.release_stop = threading.Event()

    def start(self) -> None:
        self.calls.append("start")
        self.entered_start.set()
        self.release_start.wait(5)

    def stop(self) -> bytes:
        self.calls.append("stop")
        self.entered_stop.set()
        self.release_stop.wait(5)
        return self.clip

    def cancel(self) -> None:
        self.calls.append("cancel")

    def level(self) -> float:
        return 0.0


def slice_of(hub: EventHub) -> Dict[str, Any]:
    value: Dict[str, Any] = hub.snapshot()["state"]["dictation"]
    return value


@pytest.fixture
def t2v(account_engine: AccountEngine) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=account_engine.url())
    yield client
    client.close()


def test_start_stop_delivers_a_transcript_with_the_preferred_model_and_language(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    t2v.preferences.update(stt_model="scribe_v2", stt_language="vi")
    capture = FakeCapture()
    hub = EventHub()
    dictation = Dictation(capture, t2v, allow_anonymous=True)
    dictation.attach(hub)
    try:
        assert slice_of(hub) == {"phase": "idle", "transcript": None, "error": None}
        dictation.start()
        assert slice_of(hub)["phase"] == "listening"
        with pytest.raises(CommandFailure) as refused:
            dictation.start()
        assert refused.value.type == "busy"
        dictation.stop()
        assert wait_for(
            lambda: slice_of(hub)["phase"] == "idle" and slice_of(hub)["transcript"] is not None
        )
        # reason is None for a manual stop: only the MAX_CLIP_SECONDS auto-stop sets it
        # (see test_a_clip_past_max_clip_seconds_stops_and_transcribes_itself below).
        assert slice_of(hub)["transcript"] == {
            "seq": 1, "text": "hello from the mic", "reason": None,
        }
        assert capture.calls == ["start", "stop"]
        posted = account_engine.transcriptions[-1]
        assert posted["model"] == "scribe_v2" and posted["language"] == "vi"
        assert posted["filename"] == "recording.wav" and posted["content_type"] in (
            "audio/wav", "audio/x-wav",
        )
        assert posted["size"] == len(TONE)
        dictation.start()
        assert slice_of(hub)["transcript"] is None  # a new dictation clears the last one
        dictation.stop()
        assert wait_for(lambda: slice_of(hub)["transcript"] is not None)
        assert slice_of(hub)["transcript"]["seq"] == 2
    finally:
        dictation.close()


def test_the_partner_default_then_faster_whisper_base_name_the_model(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    account_engine.config["default_stt_model"] = "scribe_v2"
    t2v.auth.login("ana@example.com", "pw")
    hub = EventHub()
    dictation = Dictation(FakeCapture(), t2v, allow_anonymous=True)
    dictation.attach(hub)
    try:
        dictation.start()
        dictation.stop()
        assert wait_for(lambda: slice_of(hub)["transcript"] is not None)
        assert account_engine.transcriptions[-1]["model"] == "scribe_v2"
        assert "language" not in account_engine.transcriptions[-1]
        account_engine.config["default_stt_model"] = None
        t2v.auth.logout()
        t2v.auth.login("ana@example.com", "pw")  # drops the cached config
        dictation.start()
        dictation.stop()
        assert wait_for(
            lambda: slice_of(hub)["transcript"] is not None
            and slice_of(hub)["transcript"]["seq"] == 2
        )
        assert account_engine.transcriptions[-1]["model"] == "faster-whisper-base"
    finally:
        dictation.close()


def test_a_signed_out_start_mints_the_guest_and_warms_the_key_while_the_user_talks(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    hub = EventHub()
    dictation = Dictation(FakeCapture(), t2v, allow_anonymous=True)
    dictation.attach(hub)
    try:
        dictation.start()
        assert wait_for(lambda: t2v.auth.is_authenticated() and t2v.auth.is_anonymous())
        assert wait_for(
            lambda: any(" /v1/account/warm " in line for line in account_engine.paths())
        )
        dictation.stop()
        assert wait_for(lambda: slice_of(hub)["transcript"] is not None)
    finally:
        dictation.close()


def test_a_signed_out_start_with_guests_off_records_sign_in_required(t2v: Talk2View) -> None:
    hub = EventHub()
    dictation = Dictation(FakeCapture(), t2v, allow_anonymous=False)
    dictation.attach(hub)
    try:
        dictation.start()
        dictation.stop()
        assert wait_for(lambda: slice_of(hub)["error"] is not None)
        assert slice_of(hub)["error"]["type"] == "sign_in_required"
        assert slice_of(hub)["phase"] == "idle"
        assert not t2v.auth.is_authenticated()
    finally:
        dictation.close()


def test_cancel_drops_the_clip_and_a_capture_failure_is_an_error(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    hub = EventHub()
    capture = FakeCapture()
    dictation = Dictation(capture, t2v, allow_anonymous=True)
    dictation.attach(hub)
    try:
        dictation.start()
        dictation.cancel()
        assert slice_of(hub)["phase"] == "idle" and capture.calls == ["start", "cancel"]
        assert account_engine.transcriptions == []
        denied = Dictation(
            FakeCapture(fail_start=CaptureError("microphone_denied", "Denied.")), t2v,
            allow_anonymous=True,
        )
        denied.attach(hub)
        denied.start()
        assert slice_of(hub) == {
            "phase": "idle", "transcript": None,
            "error": {"type": "microphone_denied", "message": "Denied."},
        }
        denied.close()
    finally:
        dictation.close()


def test_an_engine_refusal_and_an_empty_transcript(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    hub = EventHub()
    dictation = Dictation(FakeCapture(), t2v, allow_anonymous=True)
    dictation.attach(hub)
    try:
        account_engine.transcription_answer = (
            400, "model_not_allowed", "Model x is not enabled for this partner"
        )
        dictation.start()
        dictation.stop()
        assert wait_for(lambda: slice_of(hub)["error"] is not None)
        assert slice_of(hub)["error"]["type"] == "model_not_allowed"
        account_engine.transcription_answer = None
        account_engine.transcription_text = "   "
        before = len(account_engine.transcriptions)
        dictation.start()
        assert slice_of(hub)["error"] is None
        dictation.stop()
        assert slice_of(hub)["phase"] == "transcribing"  # stop returned before the answer
        assert wait_for(lambda: slice_of(hub)["phase"] == "idle")
        # A blank transcript is dropped, but only once the round trip actually ran: both
        # ``transcript`` and ``error`` are also None right after ``start()`` clears them, so
        # that alone would pass even if the transcribe thread never ran at all.
        assert len(account_engine.transcriptions) == before + 1
        assert slice_of(hub)["transcript"] is None and slice_of(hub)["error"] is None
    finally:
        dictation.close()


def test_start_and_stop_run_the_capture_on_the_calling_thread_but_transcription_is_off_thread(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    hub = EventHub()
    capture = FakeCapture()
    dictation = Dictation(capture, t2v, allow_anonymous=True)
    dictation.attach(hub)
    try:
        dictation.start()
        # the router's thread, never a host main thread
        assert capture.threads == [threading.current_thread().name]
        dictation.stop()
        assert slice_of(hub)["phase"] == "transcribing"  # stop returned before the answer
        assert wait_for(lambda: slice_of(hub)["phase"] == "idle")
    finally:
        dictation.close()


def test_close_while_capture_start_is_in_flight_cancels_the_capture_and_never_listens(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    hub = EventHub()
    capture = GatedCapture()
    dictation = Dictation(capture, t2v, allow_anonymous=True)
    dictation.attach(hub)
    seeded = slice_of(hub)
    worker = threading.Thread(target=dictation.start, name="test-dictation-start")
    worker.start()
    try:
        assert capture.entered_start.wait(5)
        dictation.close()
        capture.release_start.set()
        worker.join(5)
        assert not worker.is_alive()
        # close() could not see the capture as "listening" yet to cancel it itself (it was
        # still inside capture.start()): start() must catch that once its own call returns.
        assert capture.calls == ["start", "cancel"]
        assert slice_of(hub) == seeded  # never published "listening"
    finally:
        dictation.close()


def test_close_while_capture_stop_is_in_flight_drops_the_clip_without_uploading(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    hub = EventHub()
    capture = GatedCapture()
    capture.release_start.set()  # only stop() is gated in this test
    dictation = Dictation(capture, t2v, allow_anonymous=True)
    dictation.attach(hub)
    try:
        dictation.start()
        assert slice_of(hub)["phase"] == "listening"
        worker = threading.Thread(target=dictation.stop, name="test-dictation-stop")
        worker.start()
        assert capture.entered_stop.wait(5)
        dictation.close()
        capture.release_stop.set()
        worker.join(5)
        assert not worker.is_alive()
        assert capture.calls == ["start", "stop"]
        # A generous settle: the bug this guards is a transcribe thread started anyway,
        # which would reach the (fast, local) fake engine well inside this window.
        time.sleep(0.2)
        assert account_engine.transcriptions == []
    finally:
        dictation.close()


def test_a_clip_past_max_clip_seconds_stops_and_transcribes_itself(
    t2v: Talk2View, account_engine: AccountEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dictation_module, "MAX_CLIP_SECONDS", 0.05)
    t2v.auth.login("ana@example.com", "pw")
    hub = EventHub()
    capture = FakeCapture()
    dictation = Dictation(capture, t2v, allow_anonymous=True)
    dictation.attach(hub)
    try:
        dictation.start()
        assert slice_of(hub)["phase"] == "listening"  # TS keeps showing "listening"
        assert wait_for(
            lambda: slice_of(hub)["phase"] == "idle" and slice_of(hub)["transcript"] is not None,
            timeout=2.0,
        )
        assert capture.calls == ["start", "stop"]  # auto-stop, never cancel: nothing lost
        # The page's own cue for this case (a brief "Stopped at 10 minutes"
        # notice — TS has no cap, so there is nothing there to match).
        assert slice_of(hub)["transcript"]["reason"] == "max_duration"
    finally:
        dictation.close()
