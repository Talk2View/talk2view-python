"""User preferences — the end-user's UI choices, kept in ``data_storage``.

Mirrors the TypeScript SDK's ``useUserPreferences`` (``react/useUserPreferences.ts``):
one JSON object under the storage key ``preferences`` with the camelCase keys ``model``,
``sttModel``, ``sttLanguage`` and ``fontSize``, so a host that shares a store between the
two SDKs sees one set of preferences. Keys this SDK does not know are preserved.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional, Union

from .storage import TokenStorage

logger = logging.getLogger("talk2view.preferences")

#: Storage key, identical to the TypeScript SDK's (``storage.ts`` ``getUserPreferences``).
STORAGE_KEY = "preferences"

FontSize = Literal["small", "medium", "large"]
FONT_SIZES = ("small", "medium", "large")


@dataclass(frozen=True)
class UserPreferences:
    """``None`` means "not set" (the app's default applies)."""

    model: Optional[str] = None
    stt_model: Optional[str] = None
    stt_language: Optional[str] = None
    font_size: Optional[FontSize] = None


# Python attribute -> wire key (the TS ``UserPreferences`` interface).
_WIRE_KEYS: Dict[str, str] = {
    "model": "model",
    "stt_model": "sttModel",
    "stt_language": "sttLanguage",
    "font_size": "fontSize",
}


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


#: Sentinel for "leave this preference as it is" in ``update``.
UNSET = _Unset()
_Value = Union[str, None, _Unset]

PreferencesChangeCallback = Callable[[UserPreferences], None]


class T2VPreferences:
    """Read and write the end-user's preferences. Thread-safe; every ``update`` and
    ``clear`` notifies the ``on_change`` listeners (on the mutating thread, outside the
    lock, with the preferences as stored when they run)."""

    def __init__(self, storage: TokenStorage) -> None:
        self._storage = storage
        self._lock = threading.Lock()
        self._listeners: List[PreferencesChangeCallback] = []

    def get(self) -> UserPreferences:
        with self._lock:
            return _from_wire(self._load())

    def update(
        self,
        *,
        model: _Value = UNSET,
        stt_model: _Value = UNSET,
        stt_language: _Value = UNSET,
        font_size: _Value = UNSET,
    ) -> UserPreferences:
        """Change some preferences. Pass ``None`` to unset one; leave the rest out to keep
        them. Every value is a string: anything else raises ``TypeError``, and
        ``font_size`` must be ``"small"``, ``"medium"`` or ``"large"`` (``ValueError``).
        Either way nothing is stored."""
        changes = {
            "model": model,
            "stt_model": stt_model,
            "stt_language": stt_language,
            "font_size": font_size,
        }
        for attribute, value in changes.items():
            if value is not None and not isinstance(value, (str, _Unset)):
                raise TypeError(
                    "{} must be a string or None, not {}".format(attribute, type(value).__name__)
                )
        if isinstance(font_size, str) and font_size not in FONT_SIZES:
            raise ValueError("font_size must be one of {}".format(", ".join(FONT_SIZES)))
        with self._lock:
            raw = self._load()
            for attribute, value in changes.items():
                if isinstance(value, _Unset):
                    continue
                key = _WIRE_KEYS[attribute]
                if value is None:
                    raw.pop(key, None)
                else:
                    raw[key] = value
            self._storage.set(STORAGE_KEY, json.dumps(raw))
            current = _from_wire(raw)
        self._notify()
        return current

    def clear(self) -> None:
        with self._lock:
            self._storage.delete(STORAGE_KEY)
        self._notify()

    def on_change(self, callback: PreferencesChangeCallback) -> Callable[[], None]:
        """Receive the preferences after every change. Returns an unsubscribe function."""
        with self._lock:
            self._listeners.append(callback)

        def off() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return off

    def _load(self) -> Dict[str, Any]:
        raw = self._storage.get(STORAGE_KEY)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            logger.warning("Saved preferences are not valid JSON; starting fresh")
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    def _notify(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        if not listeners:
            return
        # Read now, not passed in: after two concurrent updates every listener's last
        # call carries what is stored, whichever update notifies last.
        current = self.get()
        for listener in listeners:
            try:
                listener(current)
            except Exception:
                logger.exception("preferences on_change listener raised")


def _from_wire(raw: Dict[str, Any]) -> UserPreferences:
    def text(key: str) -> Optional[str]:
        value = raw.get(key)
        return value if isinstance(value, str) else None

    font_size = text("fontSize")
    return UserPreferences(
        model=text("model"),
        stt_model=text("sttModel"),
        stt_language=text("sttLanguage"),
        font_size=font_size if font_size in FONT_SIZES else None,  # type: ignore[arg-type]
    )


__all__ = ["FONT_SIZES", "STORAGE_KEY", "UNSET", "T2VPreferences", "UserPreferences"]
