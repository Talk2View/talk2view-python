"""Token storage — pluggable persistence for auth tokens and SDK data."""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Tuple, Union, runtime_checkable

logger = logging.getLogger("talk2view.storage")


@runtime_checkable
class TokenStorage(Protocol):
    """Protocol for token persistence backends.

    Implement this to store tokens in your application's storage layer
    (e.g., QSettings for Qt apps, keyring, encrypted file, etc.).

    The SDK calls these methods under its own locks, from whichever thread is signing in,
    signing out or refreshing a token. So they must return without waiting on another
    thread: a storage that hands its work to the host's main thread and waits for it
    (``BlockingQueuedConnection``, a blocking ``run_on_main``) deadlocks against a
    refresh or a sign-in made from that main thread. Do the work on the calling thread;
    QSettings, keyring and a file all can.
    """

    def get(self, key: str) -> Optional[str]: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


class MemoryStorage:
    """In-memory token storage (default). Tokens are lost on process exit."""

    def __init__(self) -> None:
        self._data: Dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


#: ``chmod`` is skipped on Windows: NTFS permissions are ACLs, ``os.chmod`` there can
#: only toggle the read-only bit, and a read-only file would break the next write.
_POSIX_PERMISSIONS = os.name != "nt"
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600
_DIR_MODE = stat.S_IRWXU  # 0700


class _PathState:
    """The lock, cache and load-flag shared by every ``FileStorage`` on one real path, and
    what is wrong with the file on disk when the next write must move it aside first.

    ``stat_key`` is the ``(st_mtime_ns, st_size, st_ino)`` of the bytes ``data`` was last
    loaded from or written as — ``None`` for "the file did not exist". It is how a cache
    entry that outlives the write that made it notices a *different* write happened."""

    __slots__ = ("lock", "data", "loaded", "unusable", "stat_key")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data: Dict[str, str] = {}
        self.loaded = False
        self.unusable: Optional[str] = None
        self.stat_key: Optional[Tuple[int, int, int]] = None


#: One ``_PathState`` per resolved path, so two ``FileStorage`` instances that name the
#: same file (even spelled differently, e.g. via ``..`` or a symlink) share a lock and a
#: cache instead of racing each other. Guarded by ``_registry_lock``; entries are never
#: evicted (the number of distinct paths a process opens is small and bounded by the host
#: app, not by data volume).
_registry: Dict[str, "_PathState"] = {}
_registry_lock = threading.Lock()


def _state_for(path: Path) -> "_PathState":
    key = str(path.expanduser().resolve())
    with _registry_lock:
        state = _registry.get(key)
        if state is None:
            state = _PathState()
            _registry[key] = state
        return state


def _stat_key(path: Path) -> Optional[Tuple[int, int, int]]:
    """This file's identity for cache validation, or ``None`` when it does not exist — a
    stat of its own, distinct from any real file's key. ``os.replace`` always gives the
    replaced name a fresh inode, so this changes on every write this class makes, from
    this instance or any other sharing the path."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _fsync_dir(directory: Path) -> None:
    """Best-effort: fsync the directory entry so the replaced file's name survives a
    crash, not just its content. POSIX only; failures are not fatal — the file itself
    is already durably written by the time this runs."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


class FileStorage:
    """A ``TokenStorage`` that keeps one JSON object in a file the user alone can read.

    Every ``set``/``delete`` rewrites the whole file atomically: the new content goes to
    a temporary file in the same directory, is ``fsync``ed, and then ``os.replace``s the
    old file — so a crash mid-write leaves either the old file or the new one, never a
    truncated mix. The file is created ``0600``. A directory this class had to create for
    the file is created ``0700``; an existing directory is left as it was found (POSIX
    only; see ``_POSIX_PERMISSIONS``). All calls are serialised by one lock, so threads
    may share an instance.

    The file is read on first use, and again whenever its ``(mtime, size, inode)`` no
    longer matches what this cache last loaded or wrote — so a file changed by another
    process, or by a new ``FileStorage`` opened later in this same process (a relaunched
    chat's own instance, as ``Runtime._start`` builds one each time), is picked up
    automatically; nothing here holds a file open or watches it between calls. Two
    *instances* in the same process that name the same path (even spelled differently)
    share one lock and one cache, so neither can lose the other's writes. Two *processes*
    writing at genuinely the same time remain a lost-update race — last writer wins —
    which is a caller's job to serialise (a single-instance lock such as the 3D Slicer
    extension's ``ChatLock``); this class only guarantees that a *sequential* handoff, one process's
    writer finishing before another reads or writes, never loses what was written.

    A missing or empty file starts the store empty. A file that exists but cannot be read
    (any ``OSError`` but not-found: a permission error, a Windows sharing violation while
    an antivirus scans it) makes that ``get``/``set``/``delete`` raise the ``OSError``,
    and nothing is cached: the next call reads the file again. Starting empty instead
    would read as signed out and let the next ``set`` replace data this store never
    managed to read. A file that is read but is not a JSON object of strings (invalid
    JSON, not UTF-8, another JSON type, or values that are not strings) starts the store
    empty — keeping only its string values — and before the next write replaces it, it
    is moved aside to ``<name>.corrupt-<timestamp>`` next to it (best effort; the new
    name is logged).
    """

    def __init__(self, path: Union[str, "os.PathLike[str]"]) -> None:
        self._path = Path(path)
        self._state = _state_for(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def get(self, key: str) -> Optional[str]:
        with self._state.lock:
            return self._load().get(key)

    def set(self, key: str, value: str) -> None:
        with self._state.lock:
            data = self._load()
            data[key] = value
            self._write(data)

    def delete(self, key: str) -> None:
        with self._state.lock:
            data = self._load()
            if key in data:
                del data[key]
                self._write(data)

    # ── internals (``self._state.lock`` held) ──

    def _load(self) -> Dict[str, str]:
        if self._state.loaded and _stat_key(self._path) == self._state.stat_key:
            return self._state.data
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            raw = b""
        # Any other OSError propagates, and nothing is cached: the next call reads again.
        data: Dict[str, str] = {}
        unusable: Optional[str] = None
        if raw.strip():
            parsed: Any = None
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except ValueError:  # invalid JSON, and UnicodeDecodeError (a ValueError too)
                unusable = "is not valid UTF-8 JSON"
            if isinstance(parsed, dict):
                data = {str(k): v for k, v in parsed.items() if isinstance(v, str)}
                if len(data) != len(parsed):
                    unusable = "holds values that are not strings"
            elif unusable is None:
                unusable = "does not hold a JSON object"
        if unusable is not None:
            logger.warning(
                "%s %s; %s, and it is moved aside before the next write",
                self._path,
                unusable,
                "keeping its string values" if data else "starting empty",
            )
        self._state.data = data
        self._state.unusable = unusable
        self._state.loaded = True
        # Stamped after the read: a write racing this one (another process, outside
        # this lock) between the stat above and here is rare, and is caught on the very
        # next call either way — this stamp only needs to describe the bytes just parsed.
        self._state.stat_key = _stat_key(self._path)
        return data

    def _move_aside(self, problem: str) -> None:
        """Keep a file this store could not use: rename it to ``<name>.corrupt-<time>``
        (never over an earlier one) before a write replaces it. Best effort — on failure
        the write goes ahead and replaces it."""
        stamp = time.strftime("%Y%m%dT%H%M%S")
        target = self._path.with_name("{}.corrupt-{}".format(self._path.name, stamp))
        n = 1
        while target.exists():
            target = self._path.with_name(
                "{}.corrupt-{}-{}".format(self._path.name, stamp, n)
            )
            n += 1
        try:
            os.rename(self._path, target)
        except FileNotFoundError:
            return  # gone already: nothing left to keep
        except OSError as exc:
            logger.warning(
                "%s %s and could not be moved aside (%s); it is being replaced",
                self._path,
                problem,
                exc,
            )
            return
        logger.warning("%s %s; it was moved to %s", self._path, problem, target)

    def _write(self, data: Dict[str, str]) -> None:
        if self._state.unusable is not None:
            self._move_aside(self._state.unusable)
            self._state.unusable = None
        directory = self._path.parent
        try:
            # Atomic: a bare mkdir() either creates the leaf or raises FileExistsError,
            # with no separate exists() check to race another thread/process creating
            # the same directory in between. ``mode`` applies to the leaf only (subject
            # to umask); missing parents (if any) get created with default permissions.
            directory.mkdir(mode=_DIR_MODE, parents=True)
            created = True
        except FileExistsError:
            created = False
        if _POSIX_PERMISSIONS and created:
            # umask can only strip bits from ``mode``; pin exactly 0700 regardless.
            os.chmod(directory, _DIR_MODE)
        # Same directory as the target: os.replace is only atomic within a filesystem.
        fd, tmp_name = tempfile.mkstemp(prefix=self._path.name + ".", dir=str(directory))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            if _POSIX_PERMISSIONS:
                os.chmod(tmp, _FILE_MODE)
            os.replace(tmp, self._path)
            if _POSIX_PERMISSIONS:
                _fsync_dir(directory)
        except BaseException:
            # Leave the previous file untouched and never leak the temp file.
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        # This instance's own write, not just its cache, now matches the file: record it
        # so this process's next _load skips a stat round-trip's worth of doubt.
        self._state.stat_key = _stat_key(self._path)
