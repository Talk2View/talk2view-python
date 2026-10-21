"""Audio capture for dictation: what a host gives ``HostAdapter.audio()``.

The SDK records nothing itself (the standard library has no microphone): a host that can
record — 3D Slicer with ``sounddevice``, a Qt app with ``QAudioInput``, a CLI with
``pyaudio`` — implements ``AudioCapture`` and returns one from its adapter's ``audio()``;
``capabilities.audio`` on the chat page is exactly "the host returned one". The chat's
dictation (``talk2view.ui.dictation``) drives it: ``start()``, the end-user talks,
``stop()`` hands back one WAV file, which the engine transcribes.

Threads: ``start``/``stop``/``cancel`` are called on the UI server's router thread, or
(``stop`` only) ``Dictation``'s own ``MAX_CLIP_SECONDS`` auto-stop timer -- never the
host's main thread; a host records on whatever thread its audio library uses. ``level()``
may be called from any thread while recording.
"""

from __future__ import annotations

import io
import wave
from typing import FrozenSet, Protocol

__all__ = [
    "CAPTURE_ERROR_TYPES",
    "DEFAULT_DICTATION_MODEL",
    "MAX_CLIP_BYTES",
    "MAX_CLIP_SECONDS",
    "AudioCapture",
    "CaptureError",
    "pcm16_to_wav",
    "peak_pcm16",
]

#: Used when neither the end-user's preference nor the partner's config names a model
#: (TS ``DEFAULT_DICTATION_MODEL``).
DEFAULT_DICTATION_MODEL = "faster-whisper-base"

#: The engine refuses an upload over this (``packages/server/src/t2v/api/audio.py``'s
#: ``MAX_AUDIO_SIZE``, ``file_too_large``): a defensive check before uploading, in case a
#: host's capture runs at a higher rate than ``Dictation``'s own timer assumes.
MAX_CLIP_BYTES = 25 * 1024 * 1024

#: How long ``Dictation`` lets one clip run before stopping it itself, as if the end-user
#: had pressed stop, so a forgotten recording is never lost outright. 16 kHz mono 16-bit
#: PCM is ~32 KB/s, far denser than TS's Opus/WebM clip, so an unbounded recording could
#: reach ``MAX_CLIP_BYTES`` well within a plausible dictation; ten minutes of PCM is
#: ~19 MB, comfortably under the cap.
MAX_CLIP_SECONDS = 600

#: ``CaptureError.type`` values. ``microphone_denied``: the platform refused the device;
#: ``no_microphone``: none present; ``nothing_heard``: the clip is silence (on macOS a
#: denied microphone delivers zeros rather than an error); ``capture_failed``: anything else.
CAPTURE_ERROR_TYPES: FrozenSet[str] = frozenset(
    {"no_microphone", "microphone_denied", "nothing_heard", "capture_failed"}
)


class CaptureError(Exception):
    """A host's capture could not do what was asked; ``type`` is one of
    ``CAPTURE_ERROR_TYPES`` and the page turns it into copy."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.type = error_type
        self.message = message


class AudioCapture(Protocol):
    """One microphone, recording one clip at a time."""

    def start(self) -> None:
        """Begin recording. Raises ``CaptureError`` when the device cannot be opened."""

    def stop(self) -> bytes:
        """End recording and return the clip as a complete WAV file (``audio/wav``).
        Raises ``CaptureError("nothing_heard", ...)`` for a silent clip."""

    def cancel(self) -> None:
        """End recording and drop the clip. Never raises; a no-op when not recording."""

    def level(self) -> float:
        """The current input level, 0.0 to 1.0, while recording; 0.0 otherwise. A host
        with no meter answers 0.0."""


def pcm16_to_wav(frames: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """A WAV file around little-endian 16-bit PCM ``frames``."""
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)
    return out.getvalue()


def peak_pcm16(frames: bytes) -> int:
    """The largest absolute sample in 16-bit PCM ``frames``; 0 for silence or nothing.

    Walks a zero-copy ``int16`` view rather than unpacking every sample into a Python
    ``int`` tuple at once (``MAX_CLIP_BYTES`` is 12.5 million samples)."""
    count = len(frames) // 2
    if count == 0:
        return 0
    even = frames if len(frames) == count * 2 else frames[: count * 2]
    samples = memoryview(even).cast("h")
    return max(max(samples), -min(samples))
