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
    TYPE_CHECKING,
    Any,
    Callable,
    ContextManager,
    Dict,
    List,
    Literal,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)

from .client import T2VClient
from .errors import T2VError
from .state import LocalSkillState, SkillsState
from .storage import TokenStorage
from .types import RegisterSkillsResponse

if TYPE_CHECKING:
    from .skill_format import SkillDoc
    from .skill_store import SkillStore, StoredSkill

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

Origin = Literal["code", "dir", "bundled", "user"]
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
    #: False for a ``"bundled"``/``"user"`` skill that fails ``validate_skill`` or the
    #: register body's byte cap: it stays listed (with ``problems``) instead of vanishing,
    #: but is never part of ``enabled()``, a register payload, or a body-size running total.
    #: Always True for ``"code"``/``"dir"`` — those origins keep the old drop-on-failure
    #: behaviour (see ``load_directory``).
    registrable: bool = True
    has_extra_files: bool = False
    problems: Tuple[Tuple[str, str], ...] = ()
    #: The on-disk entry a ``"bundled"``/``"user"`` skill came from: its folder's name,
    #: or a flat file's stem. What the page addresses it by (``StoredSkill.key``); None
    #: for a skill added from code.
    key: Optional[str] = None


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


def _engine_limit_problem(skill: Skill) -> Tuple[str, str]:
    """The ``(field, message)`` problem for whichever thing ``validate_skill(skill)`` just
    raised about — same checks, same order, as a non-raising lookup. Only meaningful right
    after ``validate_skill(skill)`` has actually raised: used to keep an engine-invalid
    ``"user"``/``"bundled"`` skill listed (``registrable=False``) instead of dropping it,
    per the "existing skills that break the spec must still load, with problems shown,
    never vanish" rule extended to the engine's own limits."""
    if not isinstance(skill.name, str) or len(skill.name) < 1:
        return ("name", "a skill needs a name")
    if len(skill.name) > MAX_SKILL_NAME_CODEPOINTS:
        return (
            "name",
            "{} code points; the engine allows at most {}".format(
                len(skill.name), MAX_SKILL_NAME_CODEPOINTS
            ),
        )
    if not isinstance(skill.content, str) or len(skill.content) < 1:
        return ("body", "the instructions are empty")
    if not isinstance(skill.description, str):
        return ("description", "must be a string")
    return ("allowed-tools", "must be a list of tool names")


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
        self._store: Optional["SkillStore"] = None
        self._bundled: List[str] = []
        # Bundled skills a user skill of the same name replaces: listed (the page shows
        # "Replaced by yours"), never registered, never in `_entries` — so a rescan never
        # swaps the user's entry out and back in.
        self._shadowed: Dict[str, _Entry] = {}

    # ── Local list ──────────────────────────────────────────────────────────

    def add(self, skill: Skill) -> None:
        """Add or replace a skill from code. Code always wins over a directory skill of the
        same name. Registered after the debounce window."""
        validate_skill(skill)
        with self._lock:
            others = [
                e.skill
                for n, e in self._entries.items()
                if e.enabled and e.registrable and n != skill.name
            ]
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
        concerned (see ``remove``). For a ``"bundled"``/``"user"`` skill, the on/off flag
        is also written to the store (outside ``_lock``) so it survives a new
        ``T2VSkills``'s ``use_store()``."""
        persist: Optional[Tuple["SkillStore", str, bool]] = None
        with self._lock:
            entry = self._entries[name]
            if entry.enabled == enabled:
                return
            if enabled:
                check_body_size(
                    [
                        e.skill
                        for n, e in self._entries.items()
                        if e.registrable and (e.enabled or n == name)
                    ]
                )
            entry.enabled = enabled
            if not enabled:
                self._drop(name)
            if self._store is not None and entry.origin in ("bundled", "user"):
                persist = (self._store, name, enabled)
            self._mark_dirty()
        if persist is not None:
            store, persisted_name, persisted_enabled = persist
            store.set_enabled(persisted_name, persisted_enabled)
        self._notify()

    def get_all(self) -> List[Skill]:
        """Every local skill, enabled or not, in precedence-input order."""
        with self._lock:
            return [e.skill for e in self._entries.values()]

    def enabled(self) -> List[Skill]:
        """The enabled AND registrable skills: what the next register call would send."""
        with self._lock:
            return [e.skill for e in self._entries.values() if e.enabled and e.registrable]

    def get(self, name: str) -> Optional[Skill]:
        with self._lock:
            entry = self._entries.get(name)
            return entry.skill if entry is not None else None

    def clear(self) -> None:
        """Forget every local skill and the saved list."""
        with self._lock:
            names = list(self._entries)
            self._entries.clear()
            self._shadowed.clear()
            self._storage.delete(STORAGE_KEY)
            for name in names:
                self._drop(name)
            self._mark_dirty()
        self._notify()

    # ── Directories of SKILL.md files ───────────────────────────────────────

    def load_directory(
        self,
        path: Union[str, "os.PathLike[str]"],
        *,
        label: str = "dir",
        origin: Origin = "dir",
    ) -> List[Skill]:
        """Load every ``<dir>/<skill>/SKILL.md`` and ``<dir>/<skill>.md`` under ``path``.

        Calling it again with the same ``label`` is a rescan: skills whose file is gone
        are dropped (and ``needsSessionReset`` set if they had been registered). By name,
        a skill added from code wins over one from a directory, and a directory loaded
        later wins over one loaded earlier. Unreadable files and unsafe names are always
        skipped with a warning. A skill the engine would reject (``validate_skill``) or
        that would take the register body over its 8 MB cap is, for ``origin="dir"``,
        skipped the same way (unchanged pre-existing behaviour: a host's own folder isn't
        shown with problems) — but for ``origin="bundled"`` it stays listed instead, with
        a ``problems`` entry, and is simply never registered (``registrable=False``); it
        never just vanishes. A missing directory loads nothing. Returns the skills loaded
        from this directory (registrable or not).

        A rescan only marks the register call dirty when something that actually affects
        it changed: a name added or removed, a different ``Skill`` value, or a flipped
        ``enabled``/``registrable`` — not a bare ``path``/``problems`` refresh. The page
        can call this (via ``rescan()``) every time the Skills view opens without spamming
        the engine's 10-calls-a-minute register route.

        ``origin`` is the ``_Entry`` origin recorded for what is loaded here — ``"dir"``
        for a host's own directory, ``"bundled"`` for a read-only folder shipped with the
        host (``use_store``'s ``rescan``). Entries whose name starts with "." are skipped
        outright: a store's own files (``.trash/``, ``.t2v-skills.json``) are never skills.

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
        loaded: List[Tuple[Skill, str, str]] = []
        if not root.is_dir():
            logger.info("Skills directory does not exist: %s", root)
        else:
            for candidate in sorted(root.iterdir()):
                if candidate.name.startswith("."):
                    continue  # the store's own files (.trash/, .t2v-skills.json), never skills
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
                    key = candidate.name
                elif candidate.is_file() and candidate.suffix.lower() == ".md":
                    if self._resolve_in_root(root, candidate) is None:
                        continue
                    parsed = self._read_skill_file(candidate, candidate.stem)
                    key = candidate.stem
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
                    loaded.append((parsed, str(candidate), key))

        with self._lock:
            previous = {
                n for n, e in self._entries.items() if e.origin == origin and e.label == label
            }
            if origin == "bundled":
                # Rebuilt below: which of this folder's skills a user skill replaces.
                for name in [n for n, e in self._shadowed.items() if e.label == label]:
                    del self._shadowed[name]
            kept: List[Skill] = []
            seen: set[str] = set()
            changed = False
            # The body cap is checked against a running total of the enabled+registrable
            # set's bytes: re-serialising every other skill for each new one is quadratic,
            # and this loop holds `_lock` (1000 skills near the cap took about 9 s that way).
            sizes = {
                n: _wire_bytes(e.skill)
                for n, e in self._entries.items()
                if e.enabled and e.registrable
            }
            total = sum(sizes.values())
            for skill, skill_path, skill_key in loaded:
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
                shadowed_by_user = (
                    origin == "bundled" and existing is not None and existing.origin == "user"
                )

                # A "dir" skill that breaks the engine's own limits is dropped, as before
                # (a host's own folder, never shown with problems). A "bundled" one stays
                # listed instead — never registered, but never silently gone either.
                registrable = True
                problem: Optional[Tuple[str, str]] = None
                try:
                    validate_skill(skill)
                except SkillValidationError as exc:
                    if origin != "bundled":
                        logger.warning("Skipping %s: %s", skill_path, exc)
                        continue
                    logger.warning("%s: %s; kept but not registered", skill_path, exc)
                    registrable = False
                    problem = _engine_limit_problem(skill)

                if shadowed_by_user:
                    # The user's own copy stays the registered one; this is only listed.
                    self._shadowed[skill.name] = _Entry(
                        skill=skill,
                        origin=origin,
                        label=label,
                        path=skill_path,
                        enabled=False,
                        registrable=registrable,
                        problems=(problem,) if problem is not None else (),
                        key=skill_key,
                    )
                    seen.add(skill.name)
                    kept.append(skill)
                    continue

                # As check_body_size(the other enabled+registrable skills + this one).
                size = _wire_bytes(skill)
                others_bytes = total - sizes.get(skill.name, 0)
                others_count = len(sizes) - (1 if skill.name in sizes else 0)
                if registrable:
                    try:
                        _check_body_bytes(_body_bytes(others_bytes + size, others_count + 1))
                    except SkillValidationError as exc:
                        if origin != "bundled":
                            logger.warning("Skipping %s: %s", skill_path, exc)
                            continue
                        logger.warning("%s: %s; kept but not registered", skill_path, exc)
                        registrable = False
                        problem = ("size", str(exc))

                enabled = existing.enabled if existing is not None else True
                if (
                    existing is None
                    or existing.skill != skill
                    or existing.enabled != enabled
                    or existing.registrable != registrable
                ):
                    changed = True
                self._entries[skill.name] = _Entry(
                    skill=skill,
                    origin=origin,
                    label=label,
                    path=skill_path,
                    enabled=enabled,
                    registrable=registrable,
                    problems=(problem,) if problem is not None else (),
                    key=skill_key,
                )
                if enabled and registrable:
                    sizes[skill.name] = size
                    total = others_bytes + size
                elif skill.name in sizes:
                    del sizes[skill.name]
                    total = others_bytes
                seen.add(skill.name)
                kept.append(skill)
            for name in previous - seen:
                entry = self._entries.get(name)
                if entry is not None and entry.origin == origin and entry.label == label:
                    del self._entries[name]
                    self._drop(name)
                    changed = True
            if changed:
                self._mark_dirty()
        self._notify()
        return kept

    # ── A store of the user's own skills (talk2view.skill_store) ────────────

    @property
    def store(self) -> Optional["SkillStore"]:
        return self._store

    def use_store(self, store: "SkillStore", bundled: Sequence[str] = ()) -> None:
        """Keep the user's own skills in ``store`` and read ``bundled`` folders read-only.
        Loads both at once; ``rescan()`` reads them again. A user skill wins over a
        bundled one of the same name; code skills still win over both."""
        with self._lock:
            self._store = store
            self._bundled = [str(p) for p in bundled]
        self.rescan()

    def rescan(self) -> None:
        """Reload the bundled folders, then the store's skills over them, then apply the
        store's disabled set. A bundled skill whose name a user skill holds is kept aside
        (``_shadowed``: listed as replaced, never registered) and never overwrites the
        user's entry, so a rescan with nothing changed on disk changes nothing and
        schedules no register call. ``_merge_stored`` moves a bundled skill aside when a
        user skill takes its name, and brings it back when that user skill is gone."""
        store = self._store
        if store is None:
            return
        for folder in self._bundled:
            self.load_directory(folder, label="bundled", origin="bundled")
        self._merge_stored(store.list())
        disabled = store.disabled()
        changed = False
        with self._lock:
            for name, entry in self._entries.items():
                wanted = name not in disabled
                if entry.origin in ("bundled", "user") and entry.enabled != wanted:
                    entry.enabled = wanted
                    changed = True
                    if not wanted:
                        self._drop(name)
            if changed:
                self._mark_dirty()
        self._notify()

    def _merge_stored(self, stored: List["StoredSkill"]) -> None:
        """Merge the store's skills in as ``"user"`` entries. A skill that fails
        ``validate_skill`` (the engine's own limits) or the register body's byte cap stays
        listed — ``registrable=False``, with the engine's reason added to its ``problems``
        (de-duplicated against the spec ``problems`` the store already found) — instead of
        vanishing; only ``_mark_dirty()`` when a name was actually added, removed, or a
        kept one's ``Skill``/``enabled``/``registrable`` changed."""
        from .skill_format import to_skill

        with self._lock:
            previous = {n for n, e in self._entries.items() if e.origin == "user"}
            seen: set[str] = set()
            changed = False
            # Same running-total trick as `load_directory`: the cap applies to the whole
            # enabled+registrable set, not just this store's skills.
            sizes = {
                n: _wire_bytes(e.skill)
                for n, e in self._entries.items()
                if e.enabled and e.registrable
            }
            total = sum(sizes.values())
            for item in stored:
                skill = to_skill(item.doc)
                existing = self._entries.get(skill.name)
                if existing is not None and existing.origin == "code":
                    continue
                if existing is not None and existing.origin == "bundled":
                    # The user's copy replaces it: keep the bundled one listed, aside.
                    self._shadowed[skill.name] = _Entry(
                        skill=existing.skill,
                        origin="bundled",
                        label=existing.label,
                        path=existing.path,
                        enabled=False,
                        registrable=existing.registrable,
                        problems=existing.problems,
                        key=existing.key,
                    )
                    sizes.pop(skill.name, None)
                    total = sum(sizes.values())
                    changed = True
                    existing = None

                spec_problems = tuple((p.field, p.message) for p in item.problems)
                registrable = True
                engine_problem: Optional[Tuple[str, str]] = None
                try:
                    validate_skill(skill)  # the engine's own limits; spec problems only warn
                except SkillValidationError as exc:
                    registrable = False
                    engine_problem = _engine_limit_problem(skill)
                    logger.warning("%s: %s; kept but not registered", item.path, exc)

                size = _wire_bytes(skill)
                others_bytes = total - sizes.get(skill.name, 0)
                others_count = len(sizes) - (1 if skill.name in sizes else 0)
                if registrable:
                    try:
                        _check_body_bytes(_body_bytes(others_bytes + size, others_count + 1))
                    except SkillValidationError as exc:
                        registrable = False
                        engine_problem = ("size", str(exc))
                        logger.warning("%s: %s; kept but not registered", item.path, exc)

                problems = spec_problems
                if engine_problem is not None and engine_problem not in spec_problems:
                    problems = spec_problems + (engine_problem,)

                enabled = existing.enabled if existing is not None else True
                if (
                    existing is None
                    or existing.skill != skill
                    or existing.enabled != enabled
                    or existing.registrable != registrable
                ):
                    changed = True
                self._entries[skill.name] = _Entry(
                    skill=skill, origin="user", label="user", path=item.path,
                    enabled=enabled, registrable=registrable,
                    has_extra_files=item.has_extra_files,
                    problems=problems,
                    key=item.key or skill.name,
                )
                if enabled and registrable:
                    sizes[skill.name] = size
                    total = others_bytes + size
                elif skill.name in sizes:
                    del sizes[skill.name]
                    total = others_bytes
                seen.add(skill.name)
            for name in previous - seen:
                if self._entries.get(name) is not None and self._entries[name].origin == "user":
                    del self._entries[name]
                    self._drop(name)
                    changed = True
                    back = self._shadowed.pop(name, None)
                    if back is not None:
                        # The user's copy is gone: the bundled one it replaced is back.
                        back.enabled = True
                        if back.registrable:
                            try:
                                _check_body_bytes(
                                    _body_bytes(total + _wire_bytes(back.skill), len(sizes) + 1)
                                )
                            except SkillValidationError as exc:
                                back.registrable = False
                                back.problems = back.problems + (("size", str(exc)),)
                        self._entries[name] = back
                        if back.registrable:
                            sizes[name] = _wire_bytes(back.skill)
                            total = sum(sizes.values())
            if changed:
                self._mark_dirty()

    def _require_store(self) -> "SkillStore":
        if self._store is None or not self._store.writable:
            raise SkillValidationError("This app keeps no skills of its own")
        return self._store

    def _user_key(self, ref: str) -> str:
        """The on-disk key of the user skill ``ref`` names: ``ref`` itself when it is a
        listed user skill's key, else the key of the listed user skill whose ``name`` it
        is, else ``ref`` unchanged -- the store then looks it up by key, safely, and
        raises ``KeyError`` when it names nothing (never a path)."""
        with self._lock:
            users = [e for e in self._entries.values() if e.origin == "user"]
        for entry in users:
            if entry.key == ref:
                return ref
        for entry in users:
            if entry.skill.name == ref and entry.key:
                return entry.key
        return ref

    def save_doc(
        self, doc: "SkillDoc", *, replaces: Optional[str] = None, overwrite: bool = False
    ) -> str:
        """Write ``doc`` to the store and reload. ``replaces`` names the existing user
        skill this edits -- by its on-disk key (what the page sends) or by its name (what
        the assistant knows); ``KeyError`` when it names no user skill, and nothing is
        written. Propagates the store's ``InvalidSkillError``/``SkillExistsError`` and any
        ``OSError`` unchanged."""
        store = self._require_store()
        key = self._user_key(replaces) if replaces is not None else None
        stored = store.write(doc, replaces=key, overwrite=overwrite)
        self.rescan()
        return stored.doc.name

    def import_path(
        self, path: str, on_conflict: str = "ask", description: Optional[str] = None
    ) -> str:
        """Import a ``SKILL.md`` file or folder from outside the store and reload.
        ``description`` supplies one the file lacks. Propagates the store's errors
        unchanged."""
        stored = self._require_store().import_path(
            path, on_conflict=on_conflict, description=description  # type: ignore[arg-type]
        )
        self.rescan()
        return stored.doc.name

    def delete_user(self, key: str) -> None:
        """Trash a user skill (by its on-disk key, or its name) and reload. Raises
        ``KeyError`` when there is none."""
        store = self._require_store()
        store.delete(self._user_key(key))
        self.rescan()

    def restore_user(self, key: str) -> None:
        """Undo the last ``delete_user`` of the skill whose on-disk key is ``key`` and
        reload. Raises ``KeyError`` when nothing is trashed under that key,
        ``SkillExistsError`` when it is taken again since."""
        self._require_store().restore(key)
        self.rescan()

    def _listed(self, ref: str, origin: str) -> Optional[_Entry]:
        """The listed ``origin`` skill (a replaced bundled one included) whose on-disk key,
        else whose name, is ``ref``."""
        with self._lock:
            entries = [e for e in self._entries.values() if e.origin == origin]
            if origin == "bundled":
                entries += list(self._shadowed.values())
        for entry in entries:
            if entry.key is not None and entry.key == ref:
                return entry
        for entry in entries:
            if entry.skill.name == ref:
                return entry
        return None

    def read_markdown(self, key: str) -> str:
        """The ``SKILL.md`` text of a listed user or bundled skill (by its on-disk key, or
        its name; a user skill first), verbatim, for the page's editor. Only a skill
        already listed is looked up -- never a path -- so nothing outside the store and
        the bundled folders can be read. Raises ``KeyError`` for anything else, including
        a code-origin skill (it has no file)."""
        with self._lock:
            store = self._store
        user = self._listed(key, "user")
        if user is not None:
            stored = store.read(user.key or user.skill.name) if store is not None else None
            if stored is None:
                raise KeyError(key)
            return Path(stored.path).read_text(encoding="utf-8")
        bundled = self._listed(key, "bundled")
        if bundled is None or bundled.path is None:
            raise KeyError(key)
        file = Path(bundled.path)
        file = file / "SKILL.md" if file.is_dir() else file
        return file.read_text(encoding="utf-8")

    def customize(self, key: str) -> str:
        """Copy a bundled skill (by its on-disk key, or its name) into the user's store;
        the copy then replaces it. Returns the copy's name, which is also its key.
        ``KeyError`` for no such bundled skill; ``SkillExistsError`` when the user already
        has a skill of that name (the copy would overwrite it)."""
        from .skill_format import parse
        from .skill_store import SkillExistsError

        entry = self._listed(key, "bundled")
        if entry is None or entry.path is None:
            raise KeyError(key)
        with self._lock:
            mine = self._entries.get(entry.skill.name)
            if mine is not None and mine.origin == "user":
                raise SkillExistsError(entry.skill.name)
        # The copy is written to the folder named after the skill: a hand-made skill that
        # already lives there under another name would be overwritten, with no Undo.
        store = self._store
        if store is not None and store.read(entry.skill.name) is not None:
            raise SkillExistsError(entry.skill.name)
        file = Path(entry.path)
        file = file / "SKILL.md" if file.is_dir() else file
        doc, _problems = parse(file.read_text(encoding="utf-8"), fallback_name=entry.skill.name)
        return self.save_doc(doc, overwrite=True)

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
        # `validate_skill` (the engine's own limits) is checked by the caller, which knows
        # `origin` and — for "bundled" — keeps a failing skill listed instead of dropping it.
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
                    e.skill
                    for n, e in self._entries.items()
                    if e.enabled and e.registrable and n not in incoming
                ]
                check_body_size(others + list(incoming.values()))
                for skill in incoming.values():
                    self._entries[skill.name] = _Entry(skill=skill, origin="code", label="code")
        try:
            with self._holding_sign_out(), self._post_lock:
                with self._lock:
                    self._cancel_timer()
                    self._dirty = False
                    to_send = [
                        e.skill for e in self._entries.values() if e.enabled and e.registrable
                    ]
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
                    to_send = [
                        e.skill for e in self._entries.values() if e.enabled and e.registrable
                    ]
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
        flight is not waited for. Idempotent. Also closes the store, if one is in use
        (``use_store``), outside ``_lock``."""
        with self._lock:
            self._closed = True
            self._cancel_timer()
        if self._store is not None:
            self._store.close()

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
                to_send = [
                    e.skill for e in self._entries.values() if e.enabled and e.registrable
                ]
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
        """True when the engine may hold a skill that is not enabled+registrable here (lock
        held) — the same set a register call would send."""
        wanted = {n for n, e in self._entries.items() if e.enabled and e.registrable}
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
                    "editable": entry.origin == "user",
                    "hasExtraFiles": entry.has_extra_files,
                    "problems": [{"field": f, "message": m} for f, m in entry.problems],
                }
                if entry.path is not None:
                    item["path"] = entry.path
                if entry.key is not None:
                    item["key"] = entry.key
                if entry.skill.name in platform_names:
                    item["overridesPlatform"] = True
                local.append(item)
            for entry in self._shadowed.values():
                shadowed: LocalSkillState = {
                    "name": entry.skill.name,
                    "description": entry.skill.description,
                    "enabled": False,
                    "origin": "bundled",
                    "label": entry.label,
                    "editable": False,
                    "hasExtraFiles": entry.has_extra_files,
                    "problems": [{"field": f, "message": m} for f, m in entry.problems],
                    "overridden": True,
                }
                if entry.path is not None:
                    shadowed["path"] = entry.path
                if entry.key is not None:
                    shadowed["key"] = entry.key
                local.append(shadowed)
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
                "store": {
                    "writable": bool(self._store is not None and self._store.writable),
                    "canOpenFolder": bool(self._store is not None and self._store.folder),
                },
            }

    def on_change(self, callback: SkillsChangeCallback) -> Callable[[], None]:
        """Receive ``state()`` after every change (on the mutating thread, outside the
        lock): the caller's, a debounce timer's, or ``t2v-register-skills`` when a new
        session re-registers. Marshal UI work to your main thread. Returns an unsubscribe
        function."""
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
