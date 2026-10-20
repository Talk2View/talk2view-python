"""T2VPreferences: the TS ``preferences`` JSON shape on data_storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from talk2view import FileStorage, Talk2View, UserPreferences
from talk2view.preferences import STORAGE_KEY, T2VPreferences
from talk2view.storage import MemoryStorage


def test_storage_key_and_wire_keys_match_the_typescript_sdk() -> None:
    storage = MemoryStorage()
    prefs = T2VPreferences(storage)
    prefs.update(
        model="gemini-3.8-flash", stt_model="scribe_v2", stt_language="en", font_size="large"
    )
    assert STORAGE_KEY == "preferences"
    assert json.loads(storage.get("preferences") or "") == {
        "model": "gemini-3.8-flash",
        "sttModel": "scribe_v2",
        "sttLanguage": "en",
        "fontSize": "large",
    }


def test_get_reads_a_typescript_written_object_and_keeps_unknown_keys() -> None:
    storage = MemoryStorage()
    storage.set("preferences", '{"model":"m","sttModel":"s","fontSize":"small","theme":"dark"}')
    prefs = T2VPreferences(storage)
    assert prefs.get() == UserPreferences(model="m", stt_model="s", font_size="small")
    prefs.update(stt_language="de")
    assert json.loads(storage.get("preferences") or "")["theme"] == "dark"


def test_update_merges_and_none_unsets() -> None:
    prefs = T2VPreferences(MemoryStorage())
    prefs.update(model="a", font_size="medium")
    assert prefs.update(model=None) == UserPreferences(font_size="medium")
    assert prefs.get() == UserPreferences(font_size="medium")


def test_invalid_font_size_is_rejected() -> None:
    prefs = T2VPreferences(MemoryStorage())
    with pytest.raises(ValueError, match="font_size"):
        prefs.update(font_size="huge")
    storage = MemoryStorage()
    storage.set("preferences", '{"fontSize":"huge","model":5}')
    assert T2VPreferences(storage).get() == UserPreferences()


@pytest.mark.parametrize("field", ["model", "stt_model", "stt_language", "font_size"])
@pytest.mark.parametrize("value", [5, 1.5, True, b"large", ["a"], {"a": 1}])
def test_a_value_that_is_not_a_string_is_rejected_before_anything_is_stored(
    field: str, value: object
) -> None:
    storage = MemoryStorage()
    prefs = T2VPreferences(storage)
    prefs.update(model="kept")
    heard: List[UserPreferences] = []
    prefs.on_change(heard.append)
    with pytest.raises(TypeError, match=field):
        prefs.update(**{field: value})  # type: ignore[arg-type]
    assert json.loads(storage.get("preferences") or "") == {"model": "kept"}
    assert heard == []


def test_several_fields_at_once_store_nothing_when_one_is_wrong() -> None:
    storage = MemoryStorage()
    prefs = T2VPreferences(storage)
    with pytest.raises(TypeError, match="stt_language"):
        prefs.update(model="m", stt_language=7)  # type: ignore[arg-type]
    assert storage.get("preferences") is None


def test_corrupt_data_starts_fresh() -> None:
    storage = MemoryStorage()
    storage.set("preferences", "{oops")
    prefs = T2VPreferences(storage)
    assert prefs.get() == UserPreferences()
    prefs.update(model="m")
    assert json.loads(storage.get("preferences") or "") == {"model": "m"}


def test_clear_and_on_change() -> None:
    prefs = T2VPreferences(MemoryStorage())
    seen: List[UserPreferences] = []
    off = prefs.on_change(seen.append)
    prefs.update(model="m")
    prefs.clear()
    assert seen == [UserPreferences(model="m"), UserPreferences()]
    off()
    prefs.update(model="n")
    assert len(seen) == 2


def test_listeners_run_with_no_lock_held_and_a_raising_one_does_not_stop_the_rest() -> None:
    prefs = T2VPreferences(MemoryStorage())
    seen: List[UserPreferences] = []

    def boom(_: UserPreferences) -> None:
        raise RuntimeError("listener bug")

    prefs.on_change(boom)
    prefs.on_change(lambda _: seen.append(prefs.get()))  # re-enters: deadlocks under the lock
    assert prefs.update(model="m") == UserPreferences(model="m")
    assert seen == [UserPreferences(model="m")]


def test_talk2view_preferences_live_on_data_storage(tmp_path: Path) -> None:
    data = FileStorage(tmp_path / "data.json")
    t2v = Talk2View("pk_test", base_url="http://localhost:9999", data_storage=data)
    try:
        t2v.preferences.update(model="m")
        assert json.loads((tmp_path / "data.json").read_text(encoding="utf-8")) == {
            "preferences": '{"model": "m"}'
        }
        assert t2v.storage.get("preferences") is None  # tokens store untouched
    finally:
        t2v.close()
