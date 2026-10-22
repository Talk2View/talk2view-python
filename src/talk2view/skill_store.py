"""Where a user's own skills live. ``FolderSkillStore`` keeps them as the Agent Skills
specification lays them out, ``<root>/<name>/SKILL.md``, and still reads the older flat
``<root>/<name>.md``. Entries whose name starts with "." are the store's own
(``.trash/``, ``.t2v-skills.json``) and never skills. An account-backed store can follow
the same ``SkillStore`` protocol later."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional, Protocol, Set, Tuple, Union

from .skill_format import Problem, SkillDoc, parse, render, validate, with_fields
from .skills import MAX_REGISTER_BODY_BYTES, is_filesystem_safe_name

logger = logging.getLogger("talk2view.skills")

STATE_FILE = ".t2v-skills.json"
TRASH_DIR = ".trash"
OnConflict = Literal["ask", "replace", "keep_both"]


@dataclass(frozen=True)
class StoredSkill:
    """One skill as read from disk: its parsed ``doc``, the ``SKILL.md`` (or flat
    ``<name>.md``) ``path`` it came from, whether its folder holds files besides
    ``SKILL.md``, and any ``Problem``s found reading or validating it.

    ``key`` is the skill's ON-DISK entry: its folder's name, or a flat file's stem. It is
    what ``read``/``delete``/``restore``/``write(replaces=...)`` take, and it is the same
    as ``doc.name`` for every skill the SDK writes; a hand-made skill whose frontmatter
    ``name`` differs from its folder is still addressed by its folder. ``""`` only for a
    ``StoredSkill`` built by hand."""

    doc: SkillDoc
    path: str
    has_extra_files: bool
    problems: List[Problem]
    key: str = ""


class InvalidSkillError(ValueError):
    """Raised instead of writing or importing a ``SkillDoc`` that fails ``validate()``.
    Nothing is written to disk."""

    def __init__(self, problems: List[Problem]) -> None:
        super().__init__("; ".join("{}: {}".format(p.field, p.message) for p in problems))
        self.problems = problems


class SkillExistsError(ValueError):
    """Raised by ``write``/``import_path`` when ``name`` is already taken and the caller
    asked for neither a replace nor an overwrite."""

    def __init__(self, name: str) -> None:
        super().__init__("A skill named {!r} already exists".format(name))
        self.name = name


class SkillStore(Protocol):
    """A place a user's skills live: this folder today, an account-backed store later.
    ``writable`` says whether ``write``/``delete``/... may be called at all; ``folder`` is
    the filesystem path backing the store, or ``None`` for one that has none."""

    writable: bool
    folder: Optional[str]

    def list(self) -> List[StoredSkill]: ...
    def read(self, key: str) -> Optional[StoredSkill]: ...
    def write(
        self, doc: SkillDoc, *, replaces: Optional[str] = None, overwrite: bool = False
    ) -> StoredSkill: ...
    def import_path(
        self,
        path: Union[str, "os.PathLike[str]"],
        *,
        on_conflict: OnConflict = "ask",
        description: Optional[str] = None,
    ) -> StoredSkill: ...
    def delete(self, key: str) -> None: ...
    def restore(self, key: str) -> StoredSkill: ...
    def disabled(self) -> Set[str]: ...
    def set_enabled(self, name: str, enabled: bool) -> None: ...
    def close(self) -> None: ...


def _combine_problems(*groups: List[Problem]) -> List[Problem]:
    """``groups`` concatenated with duplicates (same field + message) dropped, keeping
    the order they were first seen in. ``parse()`` and ``validate()`` can both report the
    same problem (a scalar ``metadata``, say), and a caller that runs both should not list
    it twice."""
    seen: Set[Tuple[str, str]] = set()
    combined: List[Problem] = []
    for group in groups:
        for problem in group:
            key = (problem.field, problem.message)
            if key in seen:
                continue
            seen.add(key)
            combined.append(problem)
    return combined


class FolderSkillStore:
    """A ``SkillStore`` backed by a directory of ``SKILL.md`` folders (and older flat
    ``<name>.md`` files, which an edit moves into a folder).

    Every skill is addressed by its on-disk ``key`` (``StoredSkill.key``), and every key
    that reaches the filesystem goes through ``_entry`` first: one safe path component
    (no separator, no drive, not ``.``/``..``/hidden, none of ``_UNSAFE_NAME_CHARS``) that
    names an existing skill entry resolving inside the folder. Anything else is an
    unknown skill (``KeyError``), never a filesystem operation.

    The mutating operations (``write``, ``import_path``, ``delete``, ``restore``,
    ``set_enabled``, ``close``) run one at a time under one lock: the chat server's router
    thread and the assistant's tool thread can both reach them."""

    writable: bool = True

    def __init__(self, root: Union[str, "os.PathLike[str]"]) -> None:
        self._root = Path(root)
        self.folder: Optional[str] = str(self._root)
        # In-memory only: a key deleted in one process is not restorable from another.
        self._trashed: Dict[str, Path] = {}
        self._lock = threading.Lock()

    # ── reading ──────────────────────────────────────────────────────────────

    def list(self) -> List[StoredSkill]:
        """Every skill in the folder, one per frontmatter ``name`` (the first entry in
        sorted order wins a clash), each carrying its on-disk ``key``."""
        if not self._root.is_dir():
            return []
        found: Dict[str, StoredSkill] = {}
        for entry in sorted(self._root.iterdir()):
            if entry.name.startswith("."):
                continue
            stored = self._read_entry(entry)
            if stored is not None and stored.doc.name not in found:
                found[stored.doc.name] = stored
        return list(found.values())

    def read(self, key: str) -> Optional[StoredSkill]:
        """The skill whose on-disk entry is ``key``, or None for any key that is not one
        (including every unsafe one)."""
        try:
            entry = self._entry(key)
        except KeyError:
            return None
        return self._read_entry(entry)

    @staticmethod
    def _safe_key(key: object) -> bool:
        """One plain path component: ``is_filesystem_safe_name`` (no separators, no
        control or reserved characters -- so no drive letter's ``:`` -- not ``.``/``..``,
        not hidden, no surrounding whitespace), and not absolute on any platform."""
        if not isinstance(key, str) or not is_filesystem_safe_name(key):
            return False
        return not os.path.isabs(key) and Path(key).name == key

    def _entry(self, key: object) -> Path:
        """The existing skill entry for ``key``: the folder ``<root>/<key>`` holding a
        ``SKILL.md``, else the flat file ``<root>/<key>.md``. ``KeyError`` for an unsafe
        key, for one that names nothing, and for an entry that resolves outside the
        folder (a symlink out). The one resolver every key-taking operation uses."""
        if not isinstance(key, str) or not self._safe_key(key):
            raise KeyError(key)
        folder = self._root / key
        candidates = [folder] if (folder / "SKILL.md").is_file() else []
        candidates += [self._root / (key + ext) for ext in (".md", ".MD")]
        for entry in candidates:
            if entry.is_dir() or entry.is_file():
                if self._inside(entry):
                    return entry
                logger.warning("Skill entry %s resolves outside %s; refused", entry, self._root)
                raise KeyError(key)
        raise KeyError(key)

    @staticmethod
    def _key_of(entry: Path) -> str:
        return entry.name if entry.is_dir() else entry.stem

    def _read_entry(self, entry: Path) -> Optional[StoredSkill]:
        folder: Optional[str]
        if entry.is_dir():
            file, folder = entry / "SKILL.md", entry.name
            if not file.is_file():
                return None
        elif entry.is_file() and entry.suffix.lower() == ".md":
            file, folder = entry, None
        else:
            return None
        if not self._inside(file):
            logger.warning("Skill file %s resolves outside %s; skipped", file, self._root)
            return None
        try:
            if file.stat().st_size > MAX_REGISTER_BODY_BYTES:
                logger.warning("Skill file %s is over the engine's size cap; skipped", file)
                return None
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Could not read skill file %s: %s", file, exc)
            return None
        doc, parse_problems = parse(text, fallback_name=entry.stem if folder is None else folder)
        problems = _combine_problems(parse_problems, validate(doc, folder=folder))
        extras = folder is not None and any(p.name != "SKILL.md" for p in entry.iterdir())
        return StoredSkill(
            doc=doc,
            path=str(file),
            has_extra_files=extras,
            problems=problems,
            key=self._key_of(entry),
        )

    def _inside(self, file: Path) -> bool:
        try:
            resolved = file.resolve(strict=True)
            root = self._root.resolve(strict=True)
        except OSError:
            return False
        return resolved != root and resolved.is_relative_to(root)

    # ── writing ──────────────────────────────────────────────────────────────

    def write(
        self, doc: SkillDoc, *, replaces: Optional[str] = None, overwrite: bool = False
    ) -> StoredSkill:
        """Write ``doc`` as ``<root>/<doc.name>/SKILL.md``. ``replaces`` is the on-disk
        ``key`` of the existing skill this edits (``KeyError`` when it names none: nothing
        is written); a rename moves that skill's folder, extra files and on/off state to
        the new name. A name another skill already holds raises ``SkillExistsError``
        unless it is the replaced skill itself, or ``overwrite`` is set and this is not a
        rename. ``InvalidSkillError`` for a doc that fails ``validate()`` or is over the
        engine's request cap; nothing is written then either."""
        with self._lock:
            return self._write(doc, replaces=replaces, overwrite=overwrite)

    def _write(self, doc: SkillDoc, *, replaces: Optional[str], overwrite: bool) -> StoredSkill:
        problems = validate(doc)
        if problems:
            raise InvalidSkillError(problems)
        text = render(doc)
        size = len(text.encode("utf-8"))
        if size > MAX_REGISTER_BODY_BYTES:
            raise InvalidSkillError(
                [
                    Problem(
                        "size",
                        "the skill is {} bytes; the most the assistant accepts is {}".format(
                            size, MAX_REGISTER_BODY_BYTES
                        ),
                    )
                ]
            )
        old = self._entry(replaces) if replaces is not None else None  # KeyError: unknown
        old_stored = self._read_entry(old) if old is not None else None
        old_name = old_stored.doc.name if old_stored is not None else None
        target = self._root / doc.name
        try:
            existing: Optional[Path] = self._entry(doc.name)
        except KeyError:
            existing = None
        if existing is not None and not (old is not None and self._same(existing, old)):
            if replaces is not None or not overwrite:
                # A rename must never merge into or clobber a different skill, even with
                # overwrite=True: that flag only ever means "overwrite this exact name".
                raise SkillExistsError(doc.name)
        self._refuse_links(doc.name)
        self._root.mkdir(parents=True, exist_ok=True)
        if old is not None and old.is_dir() and old != target:
            os.replace(old, target)  # a rename keeps the extra files with the skill
        target.mkdir(exist_ok=True)
        self._atomic_write(target / "SKILL.md", text)
        flat = self._root / (doc.name + ".md")
        if flat.is_file():
            flat.unlink()
        if old is not None and old.is_file() and old.exists() and old != flat:
            old.unlink()
        if old_name is not None and old_name != doc.name:
            self._move_off_state(old_name, doc.name)
        stored = self._read_entry(target)
        if stored is None:
            raise RuntimeError("just-written skill {!r} could not be read back".format(doc.name))
        return stored

    def _refuse_links(self, name: str) -> None:
        """Refuse to write ``name`` when its folder or flat file in the skills folder is
        a link, or resolves outside the folder. ``_entry`` already refuses to *address*
        such an entry, so without this a write would see the name as free and go
        straight through the link: ``mkdir`` and the atomic write follow it."""
        for path in (self._root / name, self._root / (name + ".md"), self._root / (name + ".MD")):
            if path.is_symlink() or (path.exists() and not self._inside(path)):
                logger.warning(
                    "Skill entry %s is a link or outside %s; not written", path, self._root
                )
                raise InvalidSkillError(
                    [
                        Problem(
                            "name",
                            "{!r} in your skills folder is a link to somewhere else; rename or "
                            "remove it, or pick another name".format(path.name),
                        )
                    ]
                )

    @staticmethod
    def _same(a: Path, b: Path) -> bool:
        try:
            return a == b or os.path.samefile(a, b)
        except OSError:
            return False

    @staticmethod
    def _atomic_write(file: Path, text: str, *, suffix: str = ".md") -> None:
        # The temp file's name starts with "." so a scan never reads it as a skill, even
        # if a crash leaves it behind.
        fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=".tmp-", suffix=suffix)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, file)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def import_path(
        self,
        path: Union[str, "os.PathLike[str]"],
        *,
        on_conflict: OnConflict = "ask",
        description: Optional[str] = None,
    ) -> StoredSkill:
        """Copy the skill at ``path`` into this store. ``description``, when given, is
        the one the end-user typed for a file that had none (a missing
        description stops the import and asks for one); it replaces the file's own before
        validation, and is validated like any other. A folder with no ``SKILL.md``, or a
        file that is not UTF-8 text, is an ``InvalidSkillError`` on ``path``."""
        with self._lock:
            return self._import(Path(path), on_conflict, description)

    def _import(
        self, source: Path, on_conflict: OnConflict, description: Optional[str]
    ) -> StoredSkill:
        if self._inside(source):
            raise InvalidSkillError([Problem("path", "is already in your skills folder")])
        if source.is_dir():
            skill_file = source / "SKILL.md"
            if skill_file.is_symlink():
                raise InvalidSkillError([Problem("path", "SKILL.md is a link")])
            if not skill_file.is_file():
                raise InvalidSkillError([Problem("path", "there is no SKILL.md in that folder")])
            fallback = source.name
        else:
            skill_file = source
            fallback = (
                source.stem.lower() if source.name.lower() != "skill.md" else source.parent.name
            )
        try:
            text = skill_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise InvalidSkillError([Problem("path", "the file is not UTF-8 text")]) from None
        doc, parse_problems = parse(text, fallback_name=fallback)
        if description is not None:
            doc = with_fields(doc, description=description)
        problems = _combine_problems(parse_problems, validate(doc))
        if problems:
            raise InvalidSkillError(problems)
        if self.read(doc.name) is not None:
            if on_conflict == "ask":
                raise SkillExistsError(doc.name)
            if on_conflict == "keep_both":
                doc = with_fields(doc, name=self._free_name(doc.name))
        stored = self._write(doc, replaces=None, overwrite=True)
        if source.is_dir():
            for item in source.iterdir():
                if item.name == "SKILL.md" or item.name.startswith("."):
                    continue
                dest = Path(stored.path).parent / item.name
                self._copy_no_symlinks(item, dest)
            stored = self.read(stored.key) or stored
        return stored

    def _copy_no_symlinks(self, src: Path, dst: Path) -> None:
        """Copy ``src`` (an imported skill's extra file or folder) into ``dst``,
        skipping every symlink — file or directory, at any depth — rather than
        following it: a link inside an imported folder must never pull in content from
        outside that folder."""
        if src.is_symlink():
            logger.warning("Import: not following symlink %s; skipped", src)
            return
        if src.is_dir():
            dst.mkdir(exist_ok=True)
            for child in src.iterdir():
                self._copy_no_symlinks(child, dst / child.name)
        else:
            shutil.copy2(src, dst)

    def _free_name(self, name: str) -> str:
        n = 2
        while self.read("{}-{}".format(name, n)) is not None:
            n += 1
        return "{}-{}".format(name, n)

    # ── delete / undo ────────────────────────────────────────────────────────

    def delete(self, key: str) -> None:
        """Move the skill whose on-disk entry is ``key`` to the trash (``restore`` brings
        it back until ``close``). ``KeyError`` for any key that is not one, unsafe keys
        included: nothing is moved. Its name leaves the disabled list, so a new skill of
        that name starts on."""
        with self._lock:
            entry = self._entry(key)
            stored = self._read_entry(entry)
            trash_dir = self._root / TRASH_DIR
            if trash_dir.is_symlink() or (trash_dir.exists() and not self._inside(trash_dir)):
                # Moving the skill "into the trash" would move it out of the folder.
                raise OSError("the skills folder's {} is a link; not deleting".format(TRASH_DIR))
            trash = trash_dir / "{}-{}".format(key, time.monotonic_ns())
            trash_dir.mkdir(parents=True, exist_ok=True)
            os.replace(entry, trash)
            self._trashed[key] = trash
            if stored is not None:
                self._set_enabled(stored.doc.name, True)

    def restore(self, key: str) -> StoredSkill:
        """Undo the last ``delete(key)``. ``KeyError`` when nothing is trashed under that
        key (unsafe keys included), ``SkillExistsError`` when the key is taken again
        since (the trashed copy is kept, so Undo is still possible later)."""
        with self._lock:
            if not self._safe_key(key) or key not in self._trashed:
                raise KeyError(key)
            trash = self._trashed[key]
            dest = self._root / (key if trash.is_dir() else key + ".md")
            if self.read(key) is not None or dest.exists() or dest.is_symlink():
                raise SkillExistsError(key)
            del self._trashed[key]
            os.replace(trash, dest)
            stored = self._read_entry(dest)
            if stored is None:
                raise RuntimeError("just-restored skill {!r} could not be read back".format(key))
            return stored

    def close(self) -> None:
        with self._lock:
            shutil.rmtree(self._root / TRASH_DIR, ignore_errors=True)
            self._trashed.clear()

    # ── on / off ─────────────────────────────────────────────────────────────

    def disabled(self) -> Set[str]:
        """The names (frontmatter ``name``s, as the skills are registered) switched off."""
        try:
            data = json.loads((self._root / STATE_FILE).read_text(encoding="utf-8"))
            return {str(n) for n in data.get("disabled", [])}
        except (OSError, ValueError, AttributeError):
            return set()

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            self._set_enabled(name, enabled)

    def _set_enabled(self, name: str, enabled: bool) -> None:
        names = self.disabled()
        if (name not in names) == enabled:
            return
        if enabled:
            names.discard(name)
        else:
            names.add(name)
        self._save_disabled(names)

    def _move_off_state(self, old: str, new: str) -> None:
        """A rename: ``old``'s on/off state moves to ``new``."""
        names = self.disabled()
        if old not in names:
            return
        names.discard(old)
        names.add(new)
        self._save_disabled(names)

    def _save_disabled(self, names: Set[str]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            self._root / STATE_FILE, json.dumps({"disabled": sorted(names)}), suffix=".json"
        )


__all__ = [
    "FolderSkillStore",
    "InvalidSkillError",
    "OnConflict",
    "SkillExistsError",
    "SkillStore",
    "StoredSkill",
]
