"""talk2view.audio: the capture protocol's helpers."""

from __future__ import annotations

import io
import struct
import wave

from talk2view.audio import (
    CAPTURE_ERROR_TYPES,
    DEFAULT_DICTATION_MODEL,
    AudioCapture,
    CaptureError,
    pcm16_to_wav,
    peak_pcm16,
)


def test_pcm16_to_wav_writes_a_complete_wav_file() -> None:
    frames = struct.pack("<4h", 0, 1000, -1000, 32767)
    data = pcm16_to_wav(frames, 16000)
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    with wave.open(io.BytesIO(data)) as wav:
        assert (
            wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
        ) == (1, 2, 16000, 4)
        assert wav.readframes(4) == frames


def test_peak_pcm16() -> None:
    assert peak_pcm16(b"") == 0
    assert peak_pcm16(struct.pack("<3h", 0, 0, 0)) == 0
    assert peak_pcm16(struct.pack("<3h", 12, -300, 7)) == 300
    assert peak_pcm16(struct.pack("<1h", -32768)) == 32768


def test_capture_error_carries_a_known_type() -> None:
    error = CaptureError("no_microphone", "No microphone was found.")
    assert error.type == "no_microphone" and str(error) == "No microphone was found."
    assert {
        "no_microphone", "microphone_denied", "nothing_heard", "capture_failed"
    } == CAPTURE_ERROR_TYPES
    assert DEFAULT_DICTATION_MODEL == "faster-whisper-base"


class _Fake:
    def start(self) -> None: ...
    def stop(self) -> bytes:
        return b""
    def cancel(self) -> None: ...
    def level(self) -> float:
        return 0.0


def test_the_protocol_is_structural() -> None:
    capture: AudioCapture = _Fake()
    assert capture.level() == 0.0
