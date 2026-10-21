"""FileStorage: JSON file, atomic replace, 0600/0700, thread-safe; Talk2View data_storage."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from talk2view import FileStorage, MemoryStorage, Talk2View, TokenStorage
from talk2view.storage import _POSIX_PERMISSIONS

posix_only = pytest.mark.skipif(not _POSIX_PERMISSIONS, reason="chmod is skipped on Windows")


def test_round_trip_and_json_on_disk(tmp_path: Path) -> None:
    store = FileStorage(tmp_path / "auth.json")
    assert store.get("access_token") is None
    store.set("access_token", "tok")
    store.set("user", '{"id":"u1"}')
    assert store.get("access_token") == "tok"
    assert json.loads((tmp_path / "auth.json").read_text(encoding="utf-8")) == {
        "access_token": "tok",
        "user": '{"id":"u1"}',
    }
    store.delete("access_token")
    assert store.get("access_token") is None
    assert json.loads((tmp_path / "auth.json").read_text(encoding="utf-8")) == {
        "user": '{"id":"u1"}'
    }


def test_is_a_token_storage() -> None:
    assert isinstance(FileStorage("unused.json"), TokenStorage)


def test_reopen_reads_what_was_written(tmp_path: Path) -> None:
    path = tmp_path / "store" / "auth.json"
    FileStorage(path).set("refresh_token", "ref")
    assert FileStorage(path).get("refresh_token") == "ref"


def test_delete_of_a_missing_key_does_not_create_the_file(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    FileStorage(path).delete("nope")
    assert not path.exists()


@posix_only
def test_file_is_0600_and_directory_is_0700(tmp_path: Path) -> None:
    path = tmp_path / "t2v" / "auth.json"
    FileStorage(path).set("k", "v")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@posix_only
def test_an_existing_file_is_tightened_on_write(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o644)
    FileStorage(path).set("k", "v")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@posix_only
def test_a_newly_created_directory_is_0700(tmp_path: Path) -> None:
    path = tmp_path / "brand_new" / "auth.json"
    FileStorage(path).set("k", "v")
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@posix_only
def test_an_existing_directorys_permissions_are_left_alone(tmp_path: Path) -> None:
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)  # noqa: S103 — mkdir(mode=...) is masked by umask; force it
    FileStorage(directory / "auth.json").set("k", "v")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


@posix_only
def test_a_directory_created_by_another_process_between_check_and_mkdir_keeps_its_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the TOCTOU in exists()-then-mkdir(exist_ok=True): a directory that
    springs into existence between the check and the mkdir call must not be chmod'ed —
    only a directory *this* call actually created should be tightened."""
    path = tmp_path / "raced" / "auth.json"
    directory = path.parent
    real_mkdir = Path.mkdir

    def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        # Simulate another thread/process winning the race: create the leaf directory
        # (at a looser mode) right before this call's own mkdir runs.
        if self == directory and not self.exists():
            os.mkdir(str(self), 0o755)
            os.chmod(self, 0o755)  # noqa: S103 — simulating a pre-existing shared dir
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    FileStorage(path).set("k", "v")
    monkeypatch.undo()

    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


def test_a_crash_before_replace_leaves_the_old_file_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "auth.json"
    store = FileStorage(path)
    store.set("k", "old")

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.set("k", "new")
    monkeypatch.undo()

    assert json.loads(path.read_text(encoding="utf-8")) == {"k": "old"}
    assert [p.name for p in tmp_path.iterdir()] == ["auth.json"]
    # The in-memory value moved on; the next successful write persists it.
    assert store.get("k") == "new"
    store.set("other", "x")
    assert json.loads(path.read_text(encoding="utf-8")) == {"k": "new", "other": "x"}


def test_write_goes_through_a_temp_file_in_the_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "auth.json"
    seen = []
    real_replace = os.replace

    def spy(src: object, dst: object) -> None:
        seen.append((Path(str(src)), Path(str(dst))))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    FileStorage(path).set("k", "v")
    assert len(seen) == 1
    src, dst = seen[0]
    assert dst == path
    assert src.parent == path.parent
    assert src != path


def test_concurrent_set_from_threads_loses_nothing(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    store = FileStorage(path)
    workers = 8
    per_worker = 25
    start = threading.Barrier(workers)

    def run(worker: int) -> None:
        start.wait()
        for i in range(per_worker):
            store.set("w{}_{}".format(worker, i), str(i))

    threads = [threading.Thread(target=run, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert len(on_disk) == workers * per_worker
    for w in range(workers):
        for i in range(per_worker):
            assert store.get("w{}_{}".format(w, i)) == str(i)


def test_a_second_instance_sees_the_first_instances_write_without_reconstruction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "auth.json"
    a = FileStorage(path)
    b = FileStorage(path)
    a.set("k", "v1")
    assert b.get("k") == "v1"
    b.set("k", "v2")
    assert a.get("k") == "v2"


def test_two_instances_on_the_same_path_lose_nothing_under_concurrency(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    a = FileStorage(path)
    b = FileStorage(path)
    per_instance = 200

    def run(store: FileStorage, prefix: str) -> None:
        for i in range(per_instance):
            store.set("{}_{}".format(prefix, i), str(i))

    t1 = threading.Thread(target=run, args=(a, "a"))
    t2 = threading.Thread(target=run, args=(b, "b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert len(on_disk) == 2 * per_instance
    fresh = FileStorage(path)
    for prefix in ("a", "b"):
        for i in range(per_instance):
            assert fresh.get("{}_{}".format(prefix, i)) == str(i)


def test_a_new_instance_sees_a_write_made_by_another_process(tmp_path: Path) -> None:
    """The process-wide cache (``_registry``, keyed by resolved path, never evicted) must
    not survive past what the file actually says. Two OS processes, in sequence (never
    concurrently): this one writes, a child process writes more, then a brand-new
    ``FileStorage`` in THIS process reads and writes. Before the stat-validated cache
    the second instance still saw the first instance's
    stale, already-``loaded`` data — a relaunch of a long-lived app losing every key
    another process had written since (the 3D Slicer extension's ``conversations.json``, alternating
    with another Slicer window on the same machine, is exactly this shape)."""
    path = tmp_path / "auth.json"
    FileStorage(path).set("k", "A1")  # loads and caches this process's _PathState

    subprocess.run(  # noqa: S603 — fixed interpreter and inline script, no shell
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from talk2view.storage import FileStorage\n"
            "f = FileStorage(sys.argv[1])\n"
            "f.set('k', 'B2')\n"
            "f.set('b', 'B1')\n",
            str(path),
        ],
        check=True,
        timeout=30,
    )

    # A brand-new instance, but the SAME process as the first FileStorage above, so it
    # shares that _PathState — already loaded, before the subprocess ever ran.
    a2 = FileStorage(path)
    assert a2.get("k") == "B2"  # not the stale "A1" a never-evicted cache would remember
    assert a2.get("b") == "B1"

    a2.set("x", "A3")
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {"k": "B2", "b": "B1", "x": "A3"}  # B's keys survive A's write


def test_a_dotdot_spelling_of_the_same_path_shares_state(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    a = FileStorage(sub / "auth.json")
    respelled = str(tmp_path / "sub" / ".." / "sub" / "auth.json")
    b = FileStorage(respelled)
    assert b.get("k") is None  # force b to load before a writes
    a.set("k", "v")
    assert b.get("k") == "v"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need admin privileges on Windows")
def test_a_symlinked_directory_shares_state_with_the_real_one(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    os.symlink(real_dir, link_dir)

    a = FileStorage(real_dir / "auth.json")
    b = FileStorage(link_dir / "auth.json")
    assert b.get("k") is None  # force b to load before a writes
    a.set("k", "v")
    assert b.get("k") == "v"


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"a string"', "42", ""])
def test_corrupt_or_wrong_shaped_file_starts_empty(tmp_path: Path, content: str) -> None:
    path = tmp_path / "auth.json"
    path.write_text(content, encoding="utf-8")
    store = FileStorage(path)
    assert store.get("k") is None
    store.set("k", "v")
    assert json.loads(path.read_text(encoding="utf-8")) == {"k": "v"}


def test_non_string_values_are_dropped_on_load(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text('{"k": "v", "n": 1, "o": {"a": 1}}', encoding="utf-8")
    store = FileStorage(path)
    assert store.get("k") == "v"
    assert store.get("n") is None
    assert store.get("o") is None


class _Unreadable:
    """Reading ``path`` raises ``PermissionError`` while ``on`` — a Windows sharing
    violation, an antivirus lock: a file that is there but cannot be read right now."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
        self.on = True
        for name in ("read_bytes", "read_text"):
            real = getattr(Path, name)

            def fake(p: Path, *args: object, _real: object = real, **kwargs: object) -> object:
                if self.on and Path(p) == path:
                    raise PermissionError(13, "The file is locked", str(p))
                return _real(p, *args, **kwargs)  # type: ignore[operator]

            monkeypatch.setattr(Path, name, fake)


def test_an_unreadable_file_raises_and_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "auth.json"
    path.write_text('{"access_token": "real", "refresh_token": "ref"}', encoding="utf-8")
    store = FileStorage(path)
    blocked = _Unreadable(monkeypatch, path)

    with pytest.raises(PermissionError):
        store.get("access_token")
    with pytest.raises(PermissionError):
        store.set("access_token", "anonymous")  # must not replace what it could not read
    with pytest.raises(PermissionError):
        store.delete("refresh_token")

    blocked.on = False
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "access_token": "real",
        "refresh_token": "ref",
    }
    assert store.get("access_token") == "real"  # read again, not cached as empty
    store.set("user", "u")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "access_token": "real",
        "refresh_token": "ref",
        "user": "u",
    }


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[1, 2]", b'"a string"', b"42", b"\xff\xfe\x00 not utf-8"],
    ids=["invalid-json", "list", "string", "number", "not-utf-8"],
)
def test_an_unusable_file_is_moved_aside_before_the_next_write(
    tmp_path: Path, content: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "auth.json"
    path.write_bytes(content)
    store = FileStorage(path)
    with caplog.at_level("WARNING", logger="talk2view.storage"):
        assert store.get("k") is None  # starts empty
        assert path.read_bytes() == content  # untouched until a write needs the path
        store.set("k", "v")
    moved = sorted(tmp_path.glob("auth.json.corrupt-*"))
    assert len(moved) == 1
    assert moved[0].read_bytes() == content
    assert json.loads(path.read_text(encoding="utf-8")) == {"k": "v"}
    assert str(moved[0]) in caplog.text
    store.set("k2", "v2")  # moved once, not on every write
    assert len(sorted(tmp_path.glob("auth.json.corrupt-*"))) == 1


def test_values_that_are_not_strings_are_kept_aside(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    original = '{"k": "v", "n": 1, "o": {"a": 1}}'
    path.write_text(original, encoding="utf-8")
    store = FileStorage(path)
    assert store.get("k") == "v"
    store.set("x", "y")
    moved = sorted(tmp_path.glob("auth.json.corrupt-*"))
    assert [m.read_text(encoding="utf-8") for m in moved] == [original]
    assert json.loads(path.read_text(encoding="utf-8")) == {"k": "v", "x": "y"}


def test_an_earlier_copy_set_aside_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import talk2view.storage as storage_module

    monkeypatch.setattr(storage_module.time, "strftime", lambda *_args: "20260918T120000")
    path = tmp_path / "auth.json"
    for n in range(2):  # both within the same second
        path.write_text("{broken %d" % n, encoding="utf-8")
        store = FileStorage(path)
        store._state.loaded = False  # read the file again, as a new process would
        assert store.get("k") is None
        store.set("k", "v")
    moved = sorted(p.read_text(encoding="utf-8") for p in tmp_path.glob("auth.json.corrupt-*"))
    assert moved == ["{broken 0", "{broken 1"]


def test_a_failed_move_is_logged_and_the_write_goes_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import talk2view.storage as storage_module

    path = tmp_path / "auth.json"
    path.write_text("{not json", encoding="utf-8")
    store = FileStorage(path)
    assert store.get("k") is None

    def refuse(*_args: object) -> None:
        raise PermissionError(13, "denied")

    monkeypatch.setattr(storage_module.os, "rename", refuse)
    with caplog.at_level("WARNING", logger="talk2view.storage"):
        store.set("k", "v")
    assert "could not be moved aside" in caplog.text
    assert json.loads(path.read_text(encoding="utf-8")) == {"k": "v"}


def test_talk2view_starts_while_its_token_store_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A transient lock at startup must not stop the app: the user reads as unknown until
    # the next auth event, and every later call reads the file again.
    path = tmp_path / "auth.json"
    path.write_text('{"access_token": "tok", "user": "{\\"id\\": \\"u1\\"}"}', encoding="utf-8")
    blocked = _Unreadable(monkeypatch, path)
    with caplog.at_level("WARNING", logger="talk2view"):
        t2v = Talk2View("pk_test", base_url="http://localhost:9999", storage=FileStorage(path))
    try:
        assert "Could not read the signed-in user" in caplog.text
        with pytest.raises(PermissionError):
            t2v.auth.is_authenticated()  # still raising: never taken for "signed out"
        blocked.on = False
        assert t2v.auth.is_authenticated() is True
    finally:
        t2v.close()


class TestTalk2ViewDataStorage:
    def test_data_storage_defaults_to_storage(self) -> None:
        storage = MemoryStorage()
        t2v = Talk2View("pk_test", base_url="http://localhost:9999", storage=storage)
        try:
            assert t2v.data_storage is storage
            assert t2v.storage is storage
        finally:
            t2v.close()

    def test_data_storage_can_be_a_separate_store(self, tmp_path: Path) -> None:
        tokens = MemoryStorage()
        data = FileStorage(tmp_path / "data.json")
        t2v = Talk2View(
            "pk_test", base_url="http://localhost:9999", storage=tokens, data_storage=data
        )
        try:
            assert t2v.storage is tokens
            assert t2v.data_storage is data
        finally:
            t2v.close()

    def test_default_storage_is_memory_for_both(self) -> None:
        t2v = Talk2View("pk_test", base_url="http://localhost:9999")
        try:
            assert isinstance(t2v.storage, MemoryStorage)
            assert t2v.data_storage is t2v.storage
        finally:
            t2v.close()
