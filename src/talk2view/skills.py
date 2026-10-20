"""Skills — user-defined SKILL.md knowledge documents, registered per engine session.

A skill is a markdown document the agent can discover (``list_skills``) and load
(``load_skill``) on demand. Platform (dashboard) skills need no SDK code; this module
manages the *user* skills a host adds from code (``add``) or from directories of
``SKILL.md`` files (``load_directory``), keeps them across restarts (``save``/``load``),
and sends them to the engine with ``POST /v1/skills/register``.

Engine facts this module is built on:

* the session is one per (partner, user), and every register call here sends
  ``replace: true``. An engine that honours it always answers with ``"replaced": true``
  and the session's user skills become exactly the set sent, so removing or disabling a
  skill is just the next debounced call. An older engine leaves ``replaced`` out and
  **merges**: it never drops a skill, so there removing or disabling a registered skill
  sets ``needsSessionReset`` and the session is recycled (DELETE + create) when idle.
  ``replace`` is last-writer-wins across every client of one (partner, user) session;
* ``name`` is 1..255 Unicode **code points** (Pydantic ``max_length`` on ``str``);
  ``content`` is at least 1; the whole request body is capped at 8 MB in **bytes**;
* ``/register`` allows 10 calls per minute per user, which is why local edits are
  batched into one debounced call; ``GET /v1/skills`` allows 60;
* ``400 skills_disabled`` (``{"error": {"type": "skills_disabled", ...}}``) means the
  session has no skill registry: non-fatal on both routes, ``available`` becomes False;
* ``GET /v1/skills`` lists the user, partner and built-in skills in precedence order,
  each with a ``source`` and a ``shadowed`` flag, never the content; a 404 means an
  engine older than that route.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    Tuple,
    Union,
)

from .client import T2VClient
from .errors import T2VError
from .state import LocalSkillState, SkillsState
from .storage import TokenStorage
from .types import RegisterSkillsResponse

logger = logging.getLogger("talk2view.skills")

#: Storage key, identical to the TypeScript SDK's (``skills.ts``) so a host that shares a
#: store between the two finds the same list.
STORAGE_KEY = "t2v_user_skills"
#: Engine ``UserSkillInput.name`` ``max_length`` — counted in code points, as ``len(str)``.
MAX_SKILL_NAME_CODEPOINTS = 255
#: Engine ``max_request_body_bytes`` — counted in bytes of the JSON body.
MAX_REGISTER_BODY_BYTES = 8 * 1024 * 1024
#: A skill above this many characters is legal but is loaded whole into the model's
#: context on every ``load_skill``; warn so the author notices.
LARGE_SKILL_WARN_CHARS = 100_000
#: Local edits within this window are sent as one register call (the route allows 10/min).
REGISTER_DEBOUNCE_S = 0.5

Origin = Literal["code", "dir"]
SkillsChangeCallback = Callable[[SkillsState], None]

#: The engine's frontmatter regex, verbatim: ``---`` on its own line
#: opens and closes the block, so ``|---|---|`` inside a markdown table never splits it.
#: The closing ``---`` must be followed by a newline; a file that ends right after it has,
#: to the engine, no frontmatter at all — and so it has none here either.
FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", re.DOTALL)
#: A skill name doubles as a folder/file name in a host's skills directory.
_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f/\\:*?"<>|]')


class SkillValidationError(ValueError):
    """A skill was rejected before any request was made (name, content, or body size)."""


@dataclass(frozen=True)
class Skill:
    """One user skill. ``allowed_tools`` restricts the agent's client tools while the
    skill is loaded (engine-side gating); ``None`` means no gating."""

    name: str
    description: str = ""
    content: str = ""
    allowed_tools: Optional[List[str]] = None

    def to_wire(self) -> Dict[str, Any]:
        wire: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "content": self.content,
        }
        if self.allowed_tools is not None:
            wire["allowed_tools"] = list(self.allowed_tools)
        return wire


@dataclass(frozen=True)
class PlatformSkill:
    """A skill the platform offers this session (``GET /v1/skills``): one entry per name,
    the one the agent loads unless a user skill claims that name. ``source`` is the
    engine's word — ``"partner"`` or ``"builtin"`` today; any other value is kept as-is.
    The engine's ``"user"`` rows are this SDK's own registrations and never appear here."""

    name: str
    description: str = ""
    source: str = ""


class _TimerHandle(Protocol):
    def cancel(self) -> None: ...


StartTimer = Callable[[float, Callable[[], None]], _TimerHandle]


def _thread_timer(delay_s: float, fn: Callable[[], None]) -> _TimerHandle:
    timer = threading.Timer(delay_s, fn)
    timer.daemon = True
    timer.start()
    return timer


@dataclass
class _Entry:
    skill: Skill
    origin: Origin
    label: str
    path: Optional[str] = None
    enabled: bool = True


def validate_skill(skill: Skill) -> None:
    """Raise ``SkillValidationError`` for what the engine would 422 (same units)."""
    if not isinstance(skill.name, str) or len(skill.name) < 1:
        raise SkillValidationError("A skill needs a name")
    if len(skill.name) > MAX_SKILL_NAME_CODEPOINTS:
        raise SkillValidationError(
            "Skill name is {} code points; the engine allows at most {}".format(
                len(skill.name), MAX_SKILL_NAME_CODEPOINTS
            )
        )
    if not isinstance(skill.content, str) or len(skill.content) < 1:
        raise SkillValidationError("Skill {!r} has no content".format(skill.name))
    if not isinstance(skill.description, str):
        raise SkillValidationError("Skill {!r}: description must be a string".format(skill.name))
    if skill.allowed_tools is not None and (
        not isinstance(skill.allowed_tools, list)
        or not all(isinstance(t, str) and t for t in skill.allowed_tools)
    ):
        raise SkillValidationError(
            "Skill {!r}: allowed_tools must be a list of tool names".format(skill.name)
        )
    if len(skill.content) > LARGE_SKILL_WARN_CHARS:
        logger.warning(
            "Skill %r is %d characters; it is loaded whole into the model's context",
            skill.name,
            len(skill.content),
        )


def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str, List[str]]:
    """Split a SKILL.md into (metadata, body, warnings) exactly as the engine does:
    line-wise ``key: value``, no YAML. A frontmatter line without a colon (a YAML list
    item or a folded-scalar continuation) is dropped by the engine, so it is warned about
    here rather than silently lost."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text, []
    meta: Dict[str, str] = {}
    warnings: List[str] = []
    for line in match.group(1).strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
        elif line.strip():
            warnings.append(
                "frontmatter line {!r} has no 'key: value' form (multi-line YAML values are "
                "not supported by the engine) and was ignored".format(line.strip())
            )
    for key in ("description", "name"):
        if meta.get(key) in (">", "|", ">-", "|-"):
            warnings.append(
                "frontmatter {!r} uses a multi-line YAML scalar, which the engine reads as "
                "the literal {!r}".format(key, meta[key])
            )
    return meta, match.group(2), warnings


def parse_allowed_tools(meta: Dict[str, str]) -> Optional[List[str]]:
    """``allowed-tools`` (or ``allowed_tools``), whitespace- or comma-separated; None when
    absent or empty — the engine's rule."""
    raw = meta.get("allowed-tools") or meta.get("allowed_tools")
    if not raw:
        return None
    tools = [t for t in re.split(r"[,\s]+", raw.strip()) if t]
    return tools or None


def parse_skill_markdown(text: str, fallback_name: str) -> Skill:
    """A ``Skill`` from SKILL.md text. ``name`` falls back to ``fallback_name`` (the file
    stem or folder name) and the body is stripped, as the engine's loader does."""
    meta, body, _warnings = parse_frontmatter(text)
    return Skill(
        name=meta.get("name") or fallback_name,
        description=meta.get("description", ""),
        content=body.strip(),
        allowed_tools=parse_allowed_tools(meta),
    )


def is_filesystem_safe_name(name: str) -> bool:
    """True when ``name`` can be a file or folder name on every platform the SDK
    supports: no separators, control characters or reserved punctuation, not ``.``/``..``,
    not hidden, no leading/trailing whitespace."""
    if not name or name in (".", "..") or name.startswith(".") or name != name.strip():
        return False
    return _UNSAFE_NAME_CHARS.search(name) is None


def register_payload(skills: List[Skill]) -> Dict[str, Any]:
    """The ``/v1/skills/register`` body. ``replace`` is ignored by an engine older than the
    flag (Pydantic drops unknown fields) and honoured — and echoed as ``replaced`` — by
    one that has it."""
    return {"skills": [s.to_wire() for s in skills], "replace": True}


def check_body_size(skills: List[Skill]) -> int:
    """Return the body size in bytes, or raise when it would exceed the engine's cap."""
    return _check_body_bytes(len(json.dumps(register_payload(skills)).encode("utf-8")))


def _check_body_bytes(size: int) -> int:
    if size > MAX_REGISTER_BODY_BYTES:
        raise SkillValidationError(
            "Registering these skills would send {} bytes; the engine accepts at most {}".format(
                size, MAX_REGISTER_BODY_BYTES
            )
        )
    return size


#: The register body with no skills in it: what every body carries around its list.
_EMPTY_BODY_BYTES = len(json.dumps(register_payload([])).encode("utf-8"))
#: ``json.dumps`` writes ", " between two items of a list.
_ITEM_SEPARATOR_BYTES = len(", ")


def _wire_bytes(skill: Skill) -> int:
    """One skill's share of the register body: its serialised entry, in bytes."""
    return len(json.dumps(skill.to_wire()).encode("utf-8"))


def _body_bytes(items_bytes: int, count: int) -> int:
    """``check_body_size``'s number from per-skill sizes: ``count`` skills whose
    ``_wire_bytes`` add up to ``items_bytes``. Exactly equal, so a running total can stand
    in for re-serialising the whole set."""
    return _EMPTY_BODY_BYTES + items_bytes + _ITEM_SEPARATOR_BYTES * max(count - 1, 0)


class T2VSkills:
    """The user-skill set of one ``Talk2View``: local edits, persistence, registration.

    Local mutations (``add``, ``remove``, ``set_enabled``, ``clear``, ``load_directory``)
    take effect immediately in ``state()`` and are sent to the engine as ONE
    ``/v1/skills/register`` call after ``debounce_s`` of quiet. ``register()`` sends at
    once. ``re_register()`` is what ``Talk2View`` calls on every new session.

    Whether a removal needs the session recycled depends on the engine, which the last
    register answer tells (``replace_honoured()``): with ``replaced: true`` a removal is
    just the next call; from an engine that merges — or one not heard from yet — a removal
    of a registered skill sets ``needsSessionReset``. So does a failed call while the
    engine still holds a skill that is no longer enabled, and a failed ``re_register`` on a
    session that may be one the engine kept (see there): a removed skill must not stay
    usable because a request failed (fail closed).

    Register calls from every path (the debounce timer, ``flush``, ``register``,
    ``re_register``) run one at a time, so the engine and ``registered`` end on the last
    snapshot taken. ``close()`` (``Talk2View.close()`` calls it) stops the timer and every
    automatic register call.

    Every mutation ends with the ``on_change`` callbacks receiving the new ``state()``,
    outside this object's locks; ``ChatController`` registers ``set_skills_state`` there so
    the slice rides the controller's ``seq`` line.
    """

    def __init__(
        self,
        client: T2VClient,
        storage: TokenStorage,
        *,
        debounce_s: float = REGISTER_DEBOUNCE_S,
        start_timer: Optional[StartTimer] = None,
    ) -> None:
        self._client = client
        self._storage = storage
        self._debounce_s = debounce_s
        self._start_timer: StartTimer = start_timer or _thread_timer
        # Held across one whole register call (snapshot, POST, applying the answer) so the
        # last snapshot taken is the last POST sent and the last answer applied. Always
        # taken BEFORE `_lock`, never while holding it, and released before `_notify()`:
        # a listener may take its own lock, and the thread holding that lock may be waiting
        # here (the controller flushes under its lock). Re-entrant, so a same-thread
        # re-entry cannot self-deadlock.
        self._post_lock = threading.RLock()
        self._lock = threading.RLock()
        self._entries: Dict[str, _Entry] = {}  # insertion order = precedence input
        self._registered: List[str] = []  # what the engine holds, as far as we know
        self._available = False
        self._needs_reset = False
        # None until a register call is answered; then whether that answer said
        # `replaced: true` (the engine honours `replace`) or left it out (it merges).
        self._replace_honoured: Optional[bool] = None
        self._platform: Optional[List[PlatformSkill]] = None
        self._timer: Optional[_TimerHandle] = None
        self._dirty = False
        self._closed = False  # set by close(): no timer, no automatic register call
        self._listeners: List[SkillsChangeCallback] = []

    # ── Local list ──────────────────────────────────────────────────────────

    def add(self, skill: Skill) -> None:
        """Add or replace a skill from code. Code always wins over a directory skill of the
        same name. Registered after the debounce window."""
        validate_skill(skill)
        with self._lock:
            others = [e.skill for n, e in self._entries.items() if e.enabled and n != skill.name]
            check_body_size(others + [skill])
            self._entries[skill.name] = _Entry(skill=skill, origin="code", label="code")
            self._mark_dirty()
        self._notify()

    def remove(self, name: str) -> bool:
        """Drop a skill. True if it was there. The removal is sent with the next (debounced)
        register call; on an engine that merges, a registered skill only leaves with the
        session, so ``needsSessionReset`` is set for it."""
        with self._lock:
            entry = self._entries.pop(name, None)
            if entry is None:
                return False
            self._drop(name)
            self._mark_dirty()
        self._notify()
        return True

    def set_enabled(self, name: str, enabled: bool) -> None:
        """Enable or disable a skill without forgetting it. Raises ``KeyError`` for an
        unknown name, and ``SkillValidationError`` (changing nothing) when enabling it
        would take the register body over the engine's 8 MB cap; disabling is never
        refused. Disabling a registered skill is a removal as far as the engine is
        concerned (see ``remove``)."""
        with self._lock:
            entry = self._entries[name]
            if entry.enabled == enabled:
                return
            if enabled:
                check_body_size(
                    [e.skill for n, e in self._entries.items() if e.enabled or n == name]
                )
            entry.enabled = enabled
            if not enabled:
                self._drop(name)
            self._mark_dirty()
        self._notify()

    def get_all(self) -> List[Skill]:
        """Every local skill, enabled or not, in precedence-input order."""
        with self._lock:
            return [e.skill for e in self._entries.values()]

    def enabled(self) -> List[Skill]:
        with self._lock:
            return [e.skill for e in self._entries.values() if e.enabled]

    def get(self, name: str) -> Optional[Skill]:
        with self._lock:
            entry = self._entries.get(name)
            return entry.skill if entry is not None else None

    def clear(self) -> None:
        """Forget every local skill and the saved list."""
        with self._lock:
            names = list(self._entries)
            self._entries.clear()
            self._storage.delete(STORAGE_KEY)
            for name in names:
                self._drop(name)
            self._mark_dirty()
        self._notify()

    # ── Directories of SKILL.md files ───────────────────────────────────────

    def load_directory(
        self, path: Union[str, "os.PathLike[str]"], *, label: str = "dir"
    ) -> List[Skill]:
        """Load every ``<dir>/<skill>/SKILL.md`` and ``<dir>/<skill>.md`` under ``path``.

        Calling it again with the same ``label`` is a rescan: skills whose file is gone
        are dropped (and ``needsSessionReset`` set if they had been registered). By name,
        a skill added from code wins over one from a directory, and a directory loaded
        later wins over one loaded earlier. Unreadable files, unsafe names and skills the
        engine would reject are skipped with a warning, and so is a skill that would take
        the enabled set over the engine's 8 MB body cap; a missing directory loads nothing.
        Returns the skills loaded from this directory.

        The directory is trusted to the extent its writers are: a symlink that resolves
        outside it is refused, but a file is checked (resolved, sized) and then read in two
        steps, so someone who can write there while this runs could swap it in between.
        Load only directories the end-user controls.

        Does not POST: only ``_lock`` is taken (never ``_post_lock``), so a rescan cannot
        block on, or be blocked by, an in-flight register call. Mutations are marked dirty
        through ``_mark_dirty()`` (the debounced timer picks them up) and ``_notify()`` runs
        after ``_lock`` is released, same as every other mutator.
        """
        root = Path(path)
        loaded: List[Tuple[Skill, str]] = []
        if not root.is_dir():
            logger.info("Skills directory does not exist: %s", root)
        else:
            for candidate in sorted(root.iterdir()):
                if candidate.is_dir():
                    skill_file = candidate / "SKILL.md"
                    if not skill_file.is_file():
                        continue
                    if self._resolve_in_root(root, skill_file) is None:
                        continue
                    siblings = sorted(p.name for p in candidate.iterdir() if p != skill_file)
                    if siblings:
                        logger.warning(
                            "Skill folder %s: only SKILL.md is sent to the engine; ignoring %s",
                            candidate,
                            ", ".join(siblings),
                        )
                    parsed = self._read_skill_file(skill_file, candidate.name)
                elif candidate.is_file() and candidate.suffix.lower() == ".md":
                    if self._resolve_in_root(root, candidate) is None:
                        continue
                    parsed = self._read_skill_file(candidate, candidate.stem)
                else:
                    if candidate.is_symlink():
                        # Broken (or looping) — neither branch above matched it, so it
                        # would otherwise vanish silently instead of being reported.
                        try:
                            candidate.resolve(strict=True)
                        except OSError as exc:
                            logger.warning(
                                "Skill file %s could not be resolved: %s", candidate, exc
                            )
                    continue
                if parsed is not None:
                    loaded.append((parsed, str(candidate)))

        with self._lock:
            previous = {
                n for n, e in self._entries.items() if e.origin == "dir" and e.label == label
            }
            kept: List[Skill] = []
            seen: set[str] = set()
            # The body cap is checked against a running total of the enabled set's bytes:
            # re-serialising every other skill for each new one is quadratic, and this loop
            # holds `_lock` (1000 skills near the cap took about 9 s that way).
            sizes = {n: _wire_bytes(e.skill) for n, e in self._entries.items() if e.enabled}
            total = sum(sizes.values())
            for skill, skill_path in loaded:
                if skill.name in seen:
                    logger.warning(
                        "Skill %r appears twice under %s; keeping the first (%s)",
                        skill.name,
                        root,
                        self._entries[skill.name].path,
                    )
                    continue
                existing = self._entries.get(skill.name)
                if existing is not None and existing.origin == "code":
                    logger.warning(
                        "Skill %r from %s is shadowed by the one added from code",
                        skill.name,
                        skill_path,
                    )
                    continue
                # As check_body_size(the other enabled skills + this one), same bytes.
                size = _wire_bytes(skill)
                others_bytes = total - sizes.get(skill.name, 0)
                others_count = len(sizes) - (1 if skill.name in sizes else 0)
                try:
                    _check_body_bytes(_body_bytes(others_bytes + size, others_count + 1))
                except SkillValidationError as exc:
                    logger.warning("Skipping %s: %s", skill_path, exc)
                    continue
                enabled = existing.enabled if existing is not None else True
                self._entries[skill.name] = _Entry(
                    skill=skill, origin="dir", label=label, path=skill_path, enabled=enabled
                )
                if enabled:
                    sizes[skill.name] = size
                    total = others_bytes + size
                seen.add(skill.name)
                kept.append(skill)
            for name in previous - seen:
                entry = self._entries.get(name)
                if entry is not None and entry.origin == "dir" and entry.label == label:
                    del self._entries[name]
                    self._drop(name)
            self._mark_dirty()
        self._notify()
        return kept

    @staticmethod
    def _resolve_in_root(root: Path, file: Path) -> Optional[Path]:
        """The real (symlink-free) path of ``file``, or None — with a warning — when it
        cannot be resolved (a broken symlink or a symlink loop) or resolves to somewhere
        outside ``root``: a bundle must not be able to read an arbitrary host file through
        a symlink planted inside a skills directory."""
        try:
            resolved = file.resolve(strict=True)
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            logger.warning("Skill file %s could not be resolved: %s", file, exc)
            return None
        if not resolved.is_relative_to(resolved_root):
            logger.warning("Skill file %s resolves outside %s; skipped", file, root)
            return None
        return resolved

    @staticmethod
    def _read_skill_file(file: Path, fallback_name: str) -> Optional[Skill]:
        try:
            size = file.stat().st_size
        except OSError as exc:
            logger.warning("Could not read skill file %s: %s", file, exc)
            return None
        if size > MAX_REGISTER_BODY_BYTES:
            logger.warning(
                "Skill file %s is %d bytes, over the engine's %d byte request cap; skipped",
                file,
                size,
                MAX_REGISTER_BODY_BYTES,
            )
            return None
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Could not read skill file %s: %s", file, exc)
            return None
        _meta, _body, warnings = parse_frontmatter(text)
        for warning in warnings:
            logger.warning("%s: %s", file, warning)
        skill = parse_skill_markdown(text, fallback_name)
        if not is_filesystem_safe_name(skill.name):
            logger.warning(
                "%s: skill name %r is not a safe file name; skipped", file, skill.name
            )
            return None
        try:
            validate_skill(skill)
        except SkillValidationError as exc:
            logger.warning("%s: %s; skipped", file, exc)
            return None
        return skill

    # ── Persistence (``data_storage``) ──────────────────────────────────────

    def save(self) -> None:
        """Persist the code-origin skills (directory skills are re-read from disk)."""
        with self._lock:
            items = [
                {**e.skill.to_wire(), "enabled": e.enabled}
                for e in self._entries.values()
                if e.origin == "code"
            ]
            self._storage.set(STORAGE_KEY, json.dumps(items))

    def load(self) -> List[Skill]:
        """Add the saved skills back (as code skills). Does NOT register them: the next
        session creation does, or call ``register()``. Corrupt data is ignored.

        The next register call sends the enabled set whatever was loaded — an empty one
        too, when nothing is saved — so a session the engine kept from an earlier run
        cannot go on holding a skill removed since. A host that stops using skills calls
        ``clear()`` once."""
        with self._lock:
            # The saved set, even an empty one, is what the next register call sends: a session
            # the engine kept from an earlier run may still hold skills removed since.
            self._mark_dirty(schedule=False)
        raw = self._storage.get(STORAGE_KEY)
        loaded: List[Skill] = []
        if not raw:
            return loaded
        try:
            items = json.loads(raw)
        except ValueError:
            logger.warning("Saved skills are not valid JSON; ignoring")
            return loaded
        if not isinstance(items, list):
            return loaded
        with self._lock:
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    skill = Skill(
                        name=item.get("name", ""),
                        description=item.get("description", ""),
                        content=item.get("content", ""),
                        allowed_tools=item.get("allowed_tools"),
                    )
                    validate_skill(skill)
                except SkillValidationError as exc:
                    logger.warning("Skipping a saved skill: %s", exc)
                    continue
                # A TS-saved list has no flag: enabled. Otherwise only a real `true` enables
                # (fail closed); a flag that is not a bool at all is also worth a warning.
                flag = item.get("enabled", True)
                if not isinstance(flag, bool):
                    logger.warning(
                        "Saved skill %r has enabled=%r, not a boolean; loading it disabled",
                        skill.name,
                        flag,
                    )
                self._entries[skill.name] = _Entry(
                    skill=skill,
                    origin="code",
                    label="code",
                    enabled=flag is True,
                )
                loaded.append(skill)
        if loaded:
            self._notify()
        return loaded

    # ── Registration ────────────────────────────────────────────────────────

    def register(self, skills: Optional[List[Skill]] = None) -> RegisterSkillsResponse:
        """Send the enabled skills now. With ``skills`` given, they are first added as code
        skills (replacing same-named ones). Returns an empty response when the session has
        skills disabled (``available`` is then False). Any other failure is raised, and the
        edit stays pending for the next flush. As with ``add``, ``skills`` that fail
        validation or the body cap raise before any of them is added."""
        incoming: Dict[str, Skill] = {}
        for skill in skills or []:
            validate_skill(skill)
            incoming[skill.name] = skill
        if incoming:
            with self._lock:
                others = [
                    e.skill for n, e in self._entries.items() if e.enabled and n not in incoming
                ]
                check_body_size(others + list(incoming.values()))
                for skill in incoming.values():
                    self._entries[skill.name] = _Entry(skill=skill, origin="code", label="code")
        try:
            with self._holding_sign_out(), self._post_lock:
                with self._lock:
                    self._cancel_timer()
                    self._dirty = False
                    to_send = [e.skill for e in self._entries.values() if e.enabled]
                response = self._send(to_send)
        finally:
            self._notify()
        return response if response is not None else RegisterSkillsResponse()

    def re_register(self, known_fresh: bool = False) -> Optional[RegisterSkillsResponse]:
        """Called by ``Talk2View`` on every new engine session, to send the enabled skills
        again. Returns None when nothing was sent (nothing enabled and no edit pending) or
        skills are disabled. A failure is raised (``Talk2View`` logs it) and leaves the edit
        pending, so the next flush — ``chat()`` flushes before every turn — sends it again.
        After ``close()`` it does nothing and returns None.

        ``known_fresh`` says the session followed this client's own confirmed DELETE (a
        recycle), so it is brand new and holds no user skill. Any other session may be one
        the engine kept and handed back (``POST /v1/sessions`` is get-or-create), holding
        skills this client no longer wants — after a restart ``registered`` cannot say
        which. So when the call fails for any reason but the body cap, a possibly-reused
        session sets ``needsSessionReset`` (fail closed: what it holds is unknown), and a
        known-fresh one sets nothing: nothing unwanted is live there, and the edit waits
        for the next flush."""
        response: Optional[RegisterSkillsResponse] = None
        notify = True
        try:
            with self._holding_sign_out(), self._post_lock:
                with self._lock:
                    if self._closed:
                        # Checked under the post lock: a close() that ran while this call
                        # waited out another register call stops it too.
                        notify = False
                        return None
                    self._cancel_timer()
                    if known_fresh:
                        # A new session: no user skill is registered, so none is unwanted.
                        self._registered = []
                        self._needs_reset = False
                    to_send = [e.skill for e in self._entries.values() if e.enabled]
                    # A pending edit is sent even when it leaves nothing enabled: the engine
                    # may have handed back the session that still holds the skills (it is one
                    # per user), and an empty replace is what empties it.
                    send = bool(to_send) or self._dirty
                    self._dirty = False
                    if not send:
                        # Nothing enabled and nothing pending: the new session holds no user
                        # skills.
                        self._registered = []
                        self._needs_reset = False
                if send:
                    try:
                        response = self._send(to_send, fresh_session=True)
                    except SkillValidationError as exc:
                        # Nothing was sent, and a new session would fail the same way.
                        logger.warning("Skills not re-registered: %s", exc)
                    except Exception:
                        if not known_fresh:
                            with self._lock:
                                self._needs_reset = True
                        raise
        finally:
            if notify:
                self._notify()
        return response

    def flush(self) -> None:
        """Send any pending local edit now instead of after the debounce window. Also a
        barrier: a register call already in flight (the timer's, say) is waited out first,
        so ``needs_session_reset()`` read afterwards reflects how it ended — ``chat()``
        flushes and then checks it before every turn. Never raises: a failure is logged and
        the edit stays pending. After ``close()`` it sends nothing."""
        with self._lock:
            self._cancel_timer()
        # Unconditionally: `_dirty` may already be clear because an in-flight call took the
        # edit, and `_sync` waits for that call on the post lock before re-checking it.
        self._sync()

    def close(self) -> None:
        """Stop registering: cancel the debounce timer; no automatic register call follows.

        ``Talk2View.close()`` calls this before it closes the HTTP client, so the timer
        cannot fire a register call into a closed client. A pending edit is dropped, not
        sent — ``flush()`` first to send it. Afterwards the timer is never started again,
        ``flush()`` sends nothing and ``re_register()`` returns None; local edits and reads
        (``add``, ``get_all``, ``state``, ``save``) still work. A register call already in
        flight is not waited for. Idempotent."""
        with self._lock:
            self._closed = True
            self._cancel_timer()

    def replace_honoured(self) -> Optional[bool]:
        """Whether the engine's last register answer said ``replaced: true``. None until a
        register call is answered; False for an engine that merges (older than the flag)."""
        with self._lock:
            return self._replace_honoured

    def _send(
        self, to_send: List[Skill], *, fresh_session: bool = False
    ) -> Optional[RegisterSkillsResponse]:
        """Check the size and post (the caller holds ``_post_lock``, not ``_lock``). On ANY
        failure (a 429, a 5xx, an oversized body) the edit is marked pending again, and a
        session reset is flagged if the engine may still hold a skill that is no longer
        enabled — then the exception propagates."""
        try:
            check_body_size(to_send)
            return self._post(to_send, fresh_session=fresh_session)
        except Exception:
            with self._lock:
                self._dirty = True
                if self._holds_unwanted():
                    self._needs_reset = True
            raise

    def _post(
        self, skills: List[Skill], *, fresh_session: bool = False
    ) -> Optional[RegisterSkillsResponse]:
        """One ``/v1/skills/register`` call. Updates ``registered``/``available``/the reset
        flag. ``skills_disabled`` is swallowed (None); every other error propagates."""
        try:
            data = self._client.request(
                "/v1/skills/register", method="POST", body=register_payload(skills)
            )
        except T2VError as err:
            if err.type == "skills_disabled":
                with self._lock:
                    self._available = False
                logger.warning("Skills are disabled for this session: %s", err)
                return None
            raise
        body = data if isinstance(data, dict) else {}
        names = [str(n) for n in body.get("registered", [])]
        removed = [str(n) for n in body.get("removed", [])]
        replaced_field = body.get("replaced")
        replaced = replaced_field is True
        with self._lock:
            self._available = True
            self._replace_honoured = replaced
            if replaced or fresh_session:
                # The engine replaced its user skills with this set, or the session is new:
                # either way it holds exactly `names` now.
                self._registered = names
            else:
                for name in names:
                    if name not in self._registered:
                        self._registered.append(name)
            # With `replaced`, anything still unwanted was removed after this call's set was
            # taken, and that edit is pending — the next replace drops it. A merging engine
            # can only be brought in line by a new session.
            self._needs_reset = not replaced and self._holds_unwanted()
        return RegisterSkillsResponse(
            registered=names,
            count=len(names),
            removed=removed,
            replaced=replaced_field if isinstance(replaced_field, bool) else None,
        )

    def _holding_sign_out(self) -> ContextManager[None]:
        """Entered before ``_post_lock`` and left after it: a register call whose refresh
        token is rejected clears the session at once, but the auth listeners hear about it
        (and a ``ChatController`` resets) only once the post lock is released — never
        under it, as ``_notify()`` never runs under it either."""
        hold = getattr(self._client, "holding_sign_out", None)
        return hold() if callable(hold) else contextlib.nullcontext()

    def _sync(self) -> None:
        # `_timer` is left alone: this may be a fired timer that waited on the post lock
        # while a newer one was scheduled, and cancelling a fired timer is harmless.
        with self._holding_sign_out(), self._post_lock:
            with self._lock:
                if self._closed or not self._dirty:
                    # Closed: possibly while this fired timer waited on the post lock. The
                    # client is closed or closing, so nothing is posted.
                    return
                if self._needs_reset and self._replace_honoured is False:
                    # A merging engine cannot drop what made the reset pending; the recycle's
                    # re_register() sends the whole set. Keep the edit pending until then.
                    return
                self._dirty = False
                to_send = [e.skill for e in self._entries.values() if e.enabled]
            try:
                self._send(to_send)
            except SkillValidationError as exc:
                # Over the body cap (a loaded set is not size-checked): every tick and flush
                # fails the same way until the host disables something, so one line each.
                logger.warning("Skills not registered: %s", exc)
            except Exception:
                # Background: nothing to raise into. `_send` kept the edit pending; the next
                # flush (``chat()`` does one before every turn) or session sends it.
                with self._lock:
                    closed = self._closed
                if closed:
                    # close() ran while this call was in flight and the client closed under
                    # it: expected at shutdown, so not an error.
                    logger.debug("Skills registration cut off by close()", exc_info=True)
                else:
                    logger.exception("Skills registration failed")
        self._notify()

    def _drop(self, name: str) -> None:
        """``name`` left the enabled set (lock held). The engine only lets go of a skill it
        holds with the next replace, which needs an engine known to honour it; otherwise
        (an engine that merges, or none heard from yet) the session must be recycled."""
        if name in self._registered and self._replace_honoured is not True:
            self._needs_reset = True

    def _holds_unwanted(self) -> bool:
        """True when the engine may hold a skill that is not enabled here (lock held)."""
        wanted = {n for n, e in self._entries.items() if e.enabled}
        return any(n not in wanted for n in self._registered)

    def _mark_dirty(self, *, schedule: bool = True) -> None:
        self._dirty = True
        if schedule and not self._closed:
            self._cancel_timer()
            self._timer = self._start_timer(self._debounce_s, self._sync)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    # ── Platform catalogue and state ────────────────────────────────────────

    def list_platform(self) -> Optional[List[PlatformSkill]]:
        """``GET /v1/skills``: the partner and built-in skills this session can see.

        The engine lists every skill in precedence order (user > partner > builtin), a
        row hidden by a same-named higher-precedence skill included with
        ``shadowed: true``. The ``user`` rows are this SDK's own registrations and are
        skipped; of the rest the first row per name is kept, so each name appears once.
        A ``source`` this SDK does not know is kept as-is. None when the engine is older
        than the route (404: the catalogue is unavailable) or skills are disabled for the
        session (``400 skills_disabled``, as ``register`` treats it); any other error
        propagates.
        """
        try:
            data = self._client.request("/v1/skills")
        except T2VError as err:
            disabled = err.type == "skills_disabled"
            if err.status_code != 404 and not disabled:
                raise
            with self._lock:
                self._platform = None
                if disabled:
                    self._available = False
            if disabled:
                logger.warning("Skills are disabled for this session: %s", err)
            self._notify()
            return None
        items = data.get("skills", []) if isinstance(data, dict) else []
        platform: List[PlatformSkill] = []
        seen: set[str] = set()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            source = str(item.get("source") or "")
            name = str(item["name"])
            if source == "user" or name in seen:
                continue
            seen.add(name)
            platform.append(
                PlatformSkill(
                    name=name, description=str(item.get("description") or ""), source=source
                )
            )
        with self._lock:
            self._platform = platform
        self._notify()
        return list(platform)

    def needs_session_reset(self) -> bool:
        with self._lock:
            return self._needs_reset

    def state(self) -> SkillsState:
        with self._lock:
            # `_platform` holds partner/built-in names only (user rows are skipped), so a
            # local skill overrides the platform exactly when its name is in it.
            platform_names = {p.name for p in self._platform} if self._platform else set()
            local: List[LocalSkillState] = []
            for entry in self._entries.values():
                item: LocalSkillState = {
                    "name": entry.skill.name,
                    "description": entry.skill.description,
                    "enabled": entry.enabled,
                    "origin": entry.origin,
                    "label": entry.label,
                }
                if entry.path is not None:
                    item["path"] = entry.path
                if entry.skill.name in platform_names:
                    item["overridesPlatform"] = True
                local.append(item)
            return {
                "local": local,
                "platform": (
                    [
                        {"name": p.name, "description": p.description, "source": p.source}
                        for p in self._platform
                    ]
                    if self._platform is not None
                    else None
                ),
                "registered": list(self._registered),
                "available": self._available,
                "needsSessionReset": self._needs_reset,
            }

    def on_change(self, callback: SkillsChangeCallback) -> Callable[[], None]:
        """Receive ``state()`` after every change (on the mutating thread, outside the
        lock). Returns an unsubscribe function."""
        with self._lock:
            self._listeners.append(callback)

        def off() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return off

    def _notify(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        if not listeners:
            return
        state = self.state()
        for listener in listeners:
            try:
                listener(state)
            except Exception:
                logger.exception("skills on_change listener raised")


__all__ = [
    "FRONTMATTER_RE",
    "LARGE_SKILL_WARN_CHARS",
    "MAX_REGISTER_BODY_BYTES",
    "MAX_SKILL_NAME_CODEPOINTS",
    "REGISTER_DEBOUNCE_S",
    "STORAGE_KEY",
    "PlatformSkill",
    "Skill",
    "SkillValidationError",
    "T2VSkills",
    "check_body_size",
    "is_filesystem_safe_name",
    "parse_allowed_tools",
    "parse_frontmatter",
    "parse_skill_markdown",
    "register_payload",
    "validate_skill",
]
