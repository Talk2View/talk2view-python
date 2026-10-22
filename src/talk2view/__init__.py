"""Talk2View Python SDK — add AI-powered natural language control to any application.

Example:
    from talk2view import Talk2View, tool

    t2v = Talk2View(partner_key="pk_live_abc123")

    user = t2v.auth.login("user@example.com", "password")

    @tool
    def create_shape(shape: str, size: int) -> str:
        '''Create a 3D shape.

        Args:
            shape: The shape type (cube, sphere, cylinder).
            size: The size in units.
        '''
        return "created"

    t2v.tools.register_from_functions([create_shape])

    for event in t2v.chat("Make a red cylinder"):
        if event.type == "text":
            print(event.content, end="")
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import mimetypes
import os
import re
import threading
import uuid
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Optional, Tuple, Type, Union
from urllib.parse import quote

from .audio import DEFAULT_DICTATION_MODEL, AudioCapture, CaptureError
from .auth import T2VAuth
from .cancel import Cancelled, CancelToken
from .client import DEFAULT_TIMEOUT, DEFAULT_UPLOAD_TIMEOUT, SDK_VERSION, T2VClient
from .constants import (
    ALLOWED_ATTACHMENT_MIME,
    ATTACHMENT_ACCEPT,
    ATTACHMENT_TYPES_LABEL,
    is_allowed_attachment_type,
)
from .controller import (
    CANCELLED_APPROVAL_FEEDBACK,
    DEMO_LIMIT_REACHED,
    ChatController,
    ControllerBusyError,
    ControllerClosedError,
    IdentityChange,
)
from .conversations import Conversation, ConversationStore
from .dispatch import (
    Dispatcher,
    InlineDispatcher,
    QueueDispatcher,
    TimerPumpDispatcher,
    bpy_dispatcher,
    qt_dispatcher,
    run_on_main,
)
from .errors import (
    ANONYMOUS_UNAVAILABLE_TYPES,
    SESSION_LOST_AFTER_DENY_MESSAGE,
    SESSION_LOST_MESSAGE,
    AnonymousUnavailableError,
    AuthenticationError,
    NetworkError,
    PartnerKeyError,
    ProviderSignInError,
    SessionError,
    T2VError,
    error_for_type,
    is_session_gone,
)
from .metrics import (
    EngineToolTiming,
    RequestTiming,
    ToolTiming,
    TurnMetrics,
    TurnTiming,
    UiTiming,
)
from .preferences import T2VPreferences, UserPreferences
from .sessions import T2VSession
from .skills import PlatformSkill, Skill, SkillValidationError, T2VSkills
from .state import (
    ChatSnapshot,
    ConversationSnapshot,
    DisplayMessage,
    HistoryEntry,
    Op,
    OpBatch,
    OpError,
    PendingApproval,
    SeqGapError,
    SkillsState,
    ToolStep,
    apply_batch,
)
from .storage import FileStorage, MemoryStorage, TokenStorage
from .tools import T2VTools, strip_null_args, tool
from .types import (
    DEFAULT_MODEL,
    Attachment,
    AttachmentContent,
    AudioModel,
    Capabilities,
    ChatEvent,
    ChatMessage,
    ClientToolSchema,
    Decision,
    Model,
    ModelsResponse,
    PartnerConfig,
    PermissionCheck,
    PermissionResult,
    SignupOutcome,
    TokenResponse,
    ToolEvent,
    TranscriptionResponse,
    User,
    _DefaultModel,
    _refuse_default_model,
)
from .voice import T2VVoice

if TYPE_CHECKING:
    from .memory import T2VMemory

DEFAULT_BASE_URL = "https://engine.talk2view.com"
__version__ = SDK_VERSION

logger = logging.getLogger("talk2view")

SessionRecoveredCallback = Callable[[str], None]
AnonymousUnavailableCallback = Callable[[str], None]


#: A user id that names its memory file as it is; any other is hashed. Lowercase only,
#: as the engine's ids are, so two ids that differ only in case never share a file where
#: the filesystem ignores case (macOS and Windows by default).
_MEMORY_FILE_ID = re.compile(r"[a-z0-9_-]{1,128}")


def _memory_file(user_id: str) -> str:
    """The name of the user's memory file under ``memory_path``: ``<user id>.sqlite3``, or
    for an id that couldn't name a file safely on any filesystem, the start of its
    SHA-256."""
    if _MEMORY_FILE_ID.fullmatch(user_id) is None:
        user_id = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
    return user_id + ".sqlite3"


#: warm_up()'s claim while nobody is signed in (no user id or access token is empty).
_SIGNED_OUT_IDENTITY = ""


def _parse_partner_config(data: Any) -> PartnerConfig:
    """Fail closed: a missing or mistyped field reads as "no partner override", and only
    a literal JSON ``true`` turns web search on. A body that is not an object at all is
    an error, so it is not cached."""
    if not isinstance(data, dict):
        raise T2VError("The partner configuration response was malformed.")

    def text(key: str) -> Optional[str]:
        value = data.get(key)
        return value if isinstance(value, str) else None

    def names(key: str) -> Optional[List[str]]:
        value = data.get(key)
        return [str(v) for v in value] if isinstance(value, list) else None

    return PartnerConfig(
        default_llm_model=text("default_llm_model"),
        default_stt_model=text("default_stt_model"),
        default_image_model=text("default_image_model"),
        system_prompt=text("system_prompt"),
        allowed_llm_models=names("allowed_llm_models"),
        allowed_stt_models=names("allowed_stt_models"),
        gated_llm_models=names("gated_llm_models"),
        gated_stt_models=names("gated_stt_models"),
        web_search_enabled=data.get("web_search_enabled") is True,
    )


class _Flight:
    """One in-flight anonymous start that concurrent ``ensure_session`` callers share."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result = False
        # Why it failed, the same for every caller: the engine's refusal or the start's
        # own error. This default stands only if the start never got to record one.
        self.error: Exception = T2VError("Anonymous sign-in did not complete.")


class _ConfigFlight:
    """One in-flight ``GET /v1/config`` that concurrent ``get_config`` callers share."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Optional[PartnerConfig] = None
        # The same for every caller. This default stands only if the request never
        # got to record an outcome.
        self.error: Exception = T2VError("The partner configuration request did not complete.")


class Talk2View:
    """Main entry point for the Talk2View SDK.

    Args:
        partner_key: Your partner API key (e.g., "pk_live_abc123").
        base_url: Platform API base URL. Defaults to https://engine.talk2view.com.
        storage: Token storage backend. Defaults to MemoryStorage.
        data_storage: Where skills and preferences are kept. Defaults to
            ``storage``. Give a keyring-backed ``storage`` its own
            ``data_storage`` (e.g. a ``FileStorage``): a keyring must never
            hold skill bodies.
        model: Default model id to use for sessions. Leave it out for the partner's
            default: ``DEFAULT_MODEL`` is refused here (``TypeError``); it belongs to
            ``set_chat_defaults``.
        dispatcher: Where tool handlers and permission callbacks run. Defaults
            to InlineDispatcher (the calling thread). Desktop hosts pass
            ``qt_dispatcher(QTimer)`` / ``bpy_dispatcher(bpy.app.timers)`` so
            handlers run on the host's main thread while chat() streams on a
            worker thread.
        capabilities: Opt-in engine features this client can render (engine
            approvals, tool events, reasoning, ...). All off by default.
        anonymous_auto_start: When True, ``chat()`` and ``upload_attachment()``
            start an anonymous demo session if nobody is signed in (what the
            TypeScript SDK does by default). Off by default here: the partner
            must allow anonymous access, and a desktop host usually cannot
            solve the captcha it may require — see ``ensure_session()``.
        request_timeout: Seconds for a request, a token refresh, and a chat stream to
            connect (default 30, as the TypeScript SDK). An open stream then reads for
            up to 300 s between bytes, whatever this is.
        upload_timeout: Seconds for an upload: attachments and audio (default 120, as
            the TypeScript SDK).
        memory_path: A directory for on-device memory. Each user, a guest
            included, keeps their memory there in a file of their own,
            ``<user id>.sqlite3``, that only they can read, and ``memory`` is the signed-in
            user's ``T2VMemory``. The ``memory`` and ``memory_search`` capabilities are set
            on top of ``capabilities`` and kept across ``set_chat_defaults``; each turn
            sends the user's memory, and the agent's ``search_memory`` calls are answered
            from it. None (the default): no memory, and nothing changes.
    """

    def __init__(
        self,
        partner_key: str,
        base_url: str = DEFAULT_BASE_URL,
        storage: Optional[TokenStorage] = None,
        model: Optional[str] = None,
        dispatcher: Optional[Dispatcher] = None,
        capabilities: Optional[Capabilities] = None,
        data_storage: Optional[TokenStorage] = None,
        anonymous_auto_start: bool = False,
        request_timeout: float = DEFAULT_TIMEOUT,
        upload_timeout: float = DEFAULT_UPLOAD_TIMEOUT,
        memory_path: Optional[Union[str, "os.PathLike[str]"]] = None,
    ) -> None:
        _refuse_default_model(model, "Talk2View(model=...)")
        if storage is None:
            storage = MemoryStorage()
        self._storage = storage
        self._partner_key = partner_key
        self._data_storage = data_storage if data_storage is not None else storage

        self._client = T2VClient(
            partner_key=partner_key,
            storage=storage,
            base_url=base_url,
            request_timeout=request_timeout,
            upload_timeout=upload_timeout,
        )
        self._model = model
        self._memory_path = Path(memory_path) if memory_path is not None else None
        self._capabilities = self._with_memory(capabilities)
        self._current_session: Optional[T2VSession] = None
        # set_chat_defaults(model=DEFAULT_MODEL) replaced a model: chat() starts a fresh
        # engine session first (the engine keeps a session's model; see there). Pending
        # until the engine confirms a DELETE, or a later explicit model choice cancels it.
        self._fresh_session = False
        # The model the latest session create named (None: it named none, so that session
        # started on the partner's default). The engine session keeps it after the session
        # object is dropped; set_chat_defaults(model=DEFAULT_MODEL) reads it.
        self._named_session_model: Optional[str] = None
        # +1 whenever set_chat_defaults changes the model or capabilities or the end-user
        # changes, and the count the current session's create started under. A session whose
        # count differs was built on the old settings or for the previous end-user (also one
        # whose create was in flight then): chat() does not reuse it, while
        # respond_to_approval still answers on it. No lock: only these int fields are written.
        self._defaults_version = 0
        self._session_version = 0
        # +1 on every session delete (clear_session, a recycle, a fresh-session reset). The
        # recovery in chat() reads it around its create_session(): a clear that landed at any
        # time during the create, including while its register calls were out, means nobody
        # wants the turn resent. No lock: two racing deletes may count as one, which still
        # moves it.
        self._deletes = 0
        # The in-flight chat()/respond_to_approval() stream, for stop().
        self._active_cancel: Optional[CancelToken] = None
        self._session_recovered_listeners: List[SessionRecoveredCallback] = []
        self._anonymous_auto_start = anonymous_auto_start
        self._anonymous_unavailable_listeners: List[AnonymousUnavailableCallback] = []
        self._anonymous_lock = threading.Lock()
        self._anonymous_flight: Optional[_Flight] = None
        # Partner config is auth-scoped, not session-scoped: cached for the life of a
        # login, one shared request for concurrent callers, dropped on any auth change.
        # The lock guards these two fields only and is never held across the request.
        self._config_lock = threading.Lock()
        self._config_cache: Optional[PartnerConfig] = None
        self._config_flight: Optional[_ConfigFlight] = None
        # warm_up() runs once per identity. The lock guards the identity fields only
        # (these two and _auth_user_id); no request or listener ever runs under it.
        self._warm_lock = threading.Lock()
        self._warmed_identity: Optional[str] = None
        # Set by close(). An anonymous start still in flight then stores nothing and
        # notifies no one; the lock orders that store against close().
        self._close_lock = threading.Lock()
        self._closed = False
        # The signed-in user's memory (memory_path): opened on first use, closed when the
        # identity changes or the client closes. The lock guards both fields; no request
        # runs under it.
        self._memory_lock = threading.Lock()
        self._memory: Optional["T2VMemory"] = None
        self._memory_user: Optional[str] = None

        self.auth = T2VAuth(self._client)
        self.tools = T2VTools(self._client, dispatcher=dispatcher)
        if self._memory_path is not None:
            # Engine-defined: answered here, its schema never registered.
            self.tools.handle("search_memory", self._search_memory)
        self.voice = T2VVoice(self._client, require_session=self._require_session)
        self.skills = T2VSkills(self._client, self._data_storage)
        self.preferences = T2VPreferences(self._data_storage)
        #: Turn timings of this client's ``ChatController`` (``talk2view.metrics``).
        self.metrics = TurnMetrics()
        self._auth_user_id: Optional[str] = None
        try:
            self._auth_user_id = self._current_user_id()
        except OSError as exc:
            # A token store that cannot be read right now (FileStorage raises rather than
            # read as signed out) must not stop the app. This id only decides when the
            # next auth event drops the session and lets warm_up run again.
            logger.warning("Could not read the signed-in user at startup: %s", exc)
        # Early: ahead of every host listener, as the ChatController's is.
        self.auth._add_listener(self._on_auth_state_change, early=True)

    def _current_user_id(self) -> Optional[str]:
        user = self.auth.get_user()
        return user.id if user is not None else None

    def _on_auth_state_change(self, user: Optional[User]) -> None:
        """Login, logout, anonymous start, user switch, a rejected refresh token:
        drop the cached partner config. When the *identity* changed, also forget the
        chat session (the engine keys sessions by user, so the old id would only 404;
        nothing is deleted, the old token is gone) and let ``warm_up`` run again. A
        token refresh that succeeds does not fire this, and a convert keeps the user id.

        Runs as an auth listener: on the host's thread, the controller's worker, a
        warm-up thread or whichever thread met the rejected refresh, possibly while
        the other callers of a shared anonymous start are already on their way. So it
        only updates local state — no request, no listener, no lock held beyond a
        field update. Closing the previous user's memory waits at most for a store
        read or write in progress, and for a scrub a failed one left due, which runs
        here (``MemoryStore.close``); never for an engine call.
        """
        self.invalidate_config_cache()
        user_id = user.id if user is not None else None
        with self._warm_lock:
            changed = user_id != self._auth_user_id
            self._auth_user_id = user_id
            if changed and self._warmed_identity != user_id:
                # A warm_up() for this very identity may have run already (a shared
                # anonymous start releases its waiters before the listeners run): it stands.
                self._warmed_identity = None
        if changed:
            # A create still in flight for the previous identity then stores a session
            # chat() does not reuse (it would only 404 and be resent).
            self._defaults_version += 1
            self._current_session = None
            # The previous user's memory stops at once: its queued calls are dropped, and
            # the next use opens the new user's file.
            self._retire_memory()

    @property
    def storage(self) -> TokenStorage:
        """The token store."""
        return self._storage

    @property
    def model(self) -> Optional[str]:
        """The model new turns name: the constructor's ``model`` or the last
        ``set_chat_defaults(model=...)``; None after ``DEFAULT_MODEL`` (the partner's
        default). Read-only: change it with ``set_chat_defaults``."""
        return self._model

    @property
    def anonymous_auto_start(self) -> bool:
        """Whether a signed-out ``chat()``, upload, model list, voice call
        (``voice.transcribe``, ``voice.list_models``) or warm-up starts an anonymous demo
        session (the constructor's ``anonymous_auto_start``; read-only)."""
        return self._anonymous_auto_start

    @property
    def partner_key(self) -> str:
        """The partner key this client was built with (read-only)."""
        return self._partner_key

    @property
    def data_storage(self) -> TokenStorage:
        """The store for skills and preferences (``storage`` unless one was given)."""
        return self._data_storage

    @property
    def memory(self) -> Optional["T2VMemory"]:
        """The signed-in user's on-device memory (``memory_path``): their ``T2VMemory``,
        over ``<memory_path>/<user id>.sqlite3``, opened on first use. None without
        ``memory_path``, with nobody signed in, or once closed. Signing in as someone else
        closes the previous user's; a guest has one of their own. Raises when the file
        can't be opened."""
        if self._memory_path is None:
            return None
        user_id = self._auth_user_id
        with self._memory_lock:
            if self._closed:  # read under the lock: close() retires the memory after setting it
                return None
            if self._memory is not None and self._memory_user == user_id:
                return self._memory
            previous, self._memory, self._memory_user = self._memory, None, None
            if previous is not None:
                previous.close()
            if user_id is None:
                return None
            from .memory import MemoryStore, T2VMemory

            store = MemoryStore(self._memory_path / _memory_file(user_id))
            capabilities = self._capabilities
            self._memory = T2VMemory(
                self._client,
                store,
                timezone=capabilities.timezone if capabilities is not None else None,
                is_anonymous=self.auth.is_anonymous,
            )
            self._memory_user = user_id
            return self._memory

    def _with_memory(self, capabilities: Optional[Capabilities]) -> Optional[Capabilities]:
        """``capabilities`` with the memory flags set, when this client keeps memory: they
        go with the memory field every turn sends."""
        if self._memory_path is None:
            return capabilities
        return dataclasses.replace(capabilities or Capabilities(), memory=True, memory_search=True)

    def _retire_memory(self) -> None:
        """Close the open memory, if any: the next use opens the signed-in user's."""
        with self._memory_lock:
            memory, self._memory, self._memory_user = self._memory, None, None
        if memory is not None:
            memory.close()

    def _search_memory(self, **arguments: Any) -> str:
        """The ``search_memory`` handler: the signed-in user's memory answers it."""
        try:
            memory = self.memory
        except Exception as error:
            logger.warning("Memory: opening the memory failed (%s)", type(error).__name__)
            memory = None
        if memory is None:
            from .memory import NO_RESULTS

            return NO_RESULTS
        return memory.handle_search(arguments)

    def _memory_turn(self, message: str) -> Tuple[Optional["T2VMemory"], Optional[Dict[str, Any]]]:
        """The memory a turn goes with, and its ``memory`` field, once what is due is queued
        (``run_due`` never waits). Neither without ``memory_path``, or when the memory can't
        be opened (logged); never the field without the ``memory`` capability."""
        capabilities = self._capabilities
        if self._memory_path is None or capabilities is None or not capabilities.memory:
            return None, None
        try:
            memory = self.memory
        except Exception as error:
            logger.warning("Memory: opening the memory failed (%s)", type(error).__name__)
            return None, None
        if memory is None:
            return None, None
        memory.timezone = capabilities.timezone  # the user's zone, as the turn sends it
        memory.run_due()
        return memory, memory.turn_payload(message)

    def create_session(self, model: Optional[str] = None) -> T2VSession:
        """Create a new chat session.

        Tools and skills are session-scoped on the engine, so the registered
        tool schemas and the enabled skills are re-sent to the new session
        (non-fatal if that fails) once the create has answered, both at once, on
        threads of their own; this returns once both have answered. The skills'
        ``on_change`` listeners (a ``ChatController``'s included) and an auth
        listener told of a rejected refresh may therefore run on the
        ``t2v-register-skills`` or ``t2v-register-tools`` thread.

        Args:
            model: Model id override for this session. It is a later choice than a
                pending ``set_chat_defaults(model=DEFAULT_MODEL)`` and cancels it: the
                engine session switches to this model (a create that raises cancels
                nothing). A ``DEFAULT_MODEL`` reset made while this request is in flight is
                the later choice, and stands. ``DEFAULT_MODEL`` is refused here
                (``TypeError``).
        """
        _refuse_default_model(model, "create_session(model=...)")
        return self._create_session(model)

    def _create_session(
        self, model: Optional[str] = None, *, known_fresh: bool = False
    ) -> T2VSession:
        """``create_session``. ``known_fresh``: this create follows this client's own
        confirmed DELETE, so the engine's get-or-create hands back a brand-new session."""
        version = self._defaults_version  # before the settings are read: see chat()
        use_model = model or self._model
        body: Dict[str, Any] = {}
        if use_model:
            body["model"] = use_model
            # Before the request, then the flag: set_chat_defaults' order. A Default that
            # lands while the request is in flight sees the model this session will carry,
            # and its reset stands.
            self._named_session_model = use_model
        cancelled = False
        if model:
            cancelled = self._fresh_session
            self._fresh_session = False  # the caller's explicit model supersedes a reset

        try:
            data = self._client.request("/v1/sessions", method="POST", body=body)
        except BaseException:
            if cancelled:
                self._fresh_session = True  # nothing superseded it: the session never switched
            raise
        if not use_model:
            self._named_session_model = None
        session = T2VSession(
            session_id=data["session_id"],
            thread_id=data["thread_id"],
            model=data["model"],
            client=self._client,
            tools=self.tools,
            default_model=use_model,
            capabilities=self._capabilities,
        )
        self._session_version = version
        self._current_session = session
        # After the create, never before or beside it: the registers would otherwise land on
        # whatever session the engine held then, and one it dropped before the create
        # arrived (an eviction, another client's DELETE) would leave the new session with
        # no tools or skills and nothing to say so. The two registers go out together.
        self._after_session_created(known_fresh=known_fresh)
        return session

    def _after_session_created(self, *, known_fresh: bool = False) -> None:
        """Re-establish session-scoped engine state on a new session, and wait for it.

        Runs after every create_session(): the first chat, after clear_session(), and
        during recovery from a lost session. Every step is non-fatal — the session stays
        usable if one fails. The tools and the enabled skills are sent at the same time
        (``_start_re_register``). ``known_fresh``: the session followed this client's own
        confirmed DELETE; any other may be one the engine kept (``POST /v1/sessions`` is
        get-or-create), which ``skills.re_register`` treats differently when it fails.
        """
        self._start_re_register(known_fresh=known_fresh)()

    def _start_re_register(self, *, known_fresh: bool = False) -> Callable[[], None]:
        """Send the registered tool schemas and the enabled skills again, each on a thread
        of its own, and return the function that waits for both (at most a request timeout
        each; a skills call may first wait out one already in flight). Both are
        non-fatal: a failure is logged. The two are independent of each other; both
        belong after the create (see ``_create_session``). The skills' ``on_change``
        listeners run on ``t2v-register-skills``."""

        def tools() -> None:
            try:
                self.tools.re_register()
            except Exception as exc:
                logger.warning("Failed to re-register tools on the new session: %s", exc)

        def skills() -> None:
            try:
                self.skills.re_register(known_fresh=known_fresh)
            except Exception as exc:
                logger.warning("Failed to re-register skills on the new session: %s", exc)

        threads = [
            threading.Thread(target=tools, name="t2v-register-tools", daemon=True),
            threading.Thread(target=skills, name="t2v-register-skills", daemon=True),
        ]
        for thread in threads:
            thread.start()

        def wait() -> None:
            for thread in threads:
                thread.join()

        return wait

    def recycle_session(self) -> T2VSession:
        """Delete the engine session and create a fresh one (tools and skills re-sent).

        The fallback for ``skills.needs_session_reset()``: an engine older than the
        ``replace`` flag merges skill registrations and never removes one, and a
        failed replace may have left one behind, so there a removed or disabled
        skill only surely goes away with a new session. Chat history is not lost:
        every turn replays the history the caller (or ``ChatController``) passes.
        ``ChatController`` calls this when it is idle; ``chat()`` does it itself
        before a turn when a reset is still pending.

        When the engine confirmed the DELETE, the new session is known to hold no
        user skill, so a failed skills register on it asks for no further reset.
        """
        known_fresh = self._delete_session()
        return self._create_session(known_fresh=known_fresh)

    def _is_current_session(self, session: T2VSession) -> bool:
        """False if clear_session() replaced ``session`` since it was created."""
        current = self._current_session
        return current is not None and current.id == session.id

    def on_session_recovered(self, callback: SessionRecoveredCallback) -> Callable[[], None]:
        """Subscribe to "the engine lost the chat session and a new one replaced
        it". The callback receives the new session id. Returns an unsubscribe
        function."""
        self._session_recovered_listeners.append(callback)

        def unsubscribe() -> None:
            try:
                self._session_recovered_listeners.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def ensure_session(self) -> bool:
        """Make sure someone is signed in, starting an anonymous demo session if not.

        True when a session exists (already signed in, or the anonymous start
        succeeded). False on any failure: a refusal only signing in can fix —
        the ``on_anonymous_unavailable`` listeners get the error type — or any
        other error (a 503, a 429, the network, a garbled answer; nothing is
        remembered and the next call tries again). With ``anonymous_auto_start``,
        ``chat()`` and ``upload_attachment()`` raise instead: ``sign_in_required``
        for the refusal, the start's own error otherwise. Concurrent callers
        share ONE request: the two model lists, a message and an upload arriving
        together must not each create a guest account.

        Safe from any thread. Every caller of a shared start gets its answer
        before any listener runs: ``on_auth_state_change`` (started) or
        ``on_anonymous_unavailable`` (refused) run afterwards, with no lock held,
        on the thread that made the shared request — not necessarily this
        caller's: it may be one of the SDK's own threads (a ``ChatController``
        worker, the ``t2v-warm-up`` daemon), so Qt/bpy hosts must marshal UI work
        to their main thread. A listener may call ``ensure_session()`` itself
        (after a refusal that answers False at once, without a second request) or
        hand work to a thread that was waiting here. Once ``close()`` has been
        called, a start still in flight stores nothing and notifies no one.
        """
        flight = self._shared_anonymous_start()
        return flight is None or flight.result

    def _shared_anonymous_start(self, warm: bool = False) -> Optional[_Flight]:
        """The settled anonymous start this caller shares, or None when someone is
        signed in already. Starts one when none is in flight; ``warm``: for a message
        already on its way (see ``T2VAuth.start_anonymous``). A caller that joins a start
        in flight shares it as it was sent: warm or not, it is one guest."""
        if self.auth.is_authenticated():
            return None
        with self._anonymous_lock:
            flight = self._anonymous_flight
            owner = flight is None
            if flight is None:
                if self.auth.is_authenticated():
                    return None  # a start finished between the check above and this lock
                flight = self._anonymous_flight = _Flight()
        if not owner:
            flight.done.wait()
            return flight
        try:
            user = self._start_anonymous_once(flight, warm)
            flight.result = user is not None
            flight.done.set()  # settled before any listener runs
            if user is not None:
                # Unregistered first: callers now take the is_authenticated() fast path,
                # and a listener that signs out and asks again starts afresh instead of
                # reading this True.
                self._end_flight(flight)
                self._notify_signed_in(user)
            elif isinstance(flight.error, AnonymousUnavailableError):
                # Still registered while they run: a caller meanwhile, the listener
                # itself included, gets this refusal at once rather than a second request.
                self._notify_anonymous_unavailable(flight.error.type)
        finally:
            flight.done.set()  # also when the start raised: waiters get flight.error
            self._end_flight(flight)
        return flight

    def _end_flight(self, flight: _Flight) -> None:
        with self._anonymous_lock:
            if self._anonymous_flight is flight:  # never a newer start's registration
                self._anonymous_flight = None

    def _start_anonymous_once(self, flight: _Flight, warm: bool = False) -> Optional[User]:
        """Request the anonymous session and store it; no listener runs here. A
        failure is recorded on ``flight``, for every caller that shares it."""
        try:
            return self.auth._start_anonymous_session(
                store=self._store_unless_closed, warm=warm
            )
        except Exception as exc:
            if not isinstance(exc, AnonymousUnavailableError) and not self._closed:
                logger.warning("Anonymous auto-start failed: %s", exc)
            flight.error = exc
            return None

    def _store_unless_closed(self, response: TokenResponse) -> None:
        """Store an anonymous start's session — unless ``close()`` came first. Under
        the close lock, so the two are ordered: either the session was stored before
        the close, or it never is."""
        with self._close_lock:
            if self._closed:
                raise T2VError("Talk2View was closed during the anonymous sign-in.")
            self.auth._store_tokens(response)

    def _notify_signed_in(self, user: User) -> None:
        """The auth-state listeners, each on its own (``T2VAuth``'s order and
        isolation): one that raises is logged and the rest still run (the session is
        stored already). None after ``close()``."""
        if self._closed:
            return
        self.auth._notify_listeners(user)

    def _notify_anonymous_unavailable(self, error_type: str) -> None:
        if self._closed:
            return
        for listener in list(self._anonymous_unavailable_listeners):
            try:
                listener(error_type)
            except Exception:
                logger.exception("on_anonymous_unavailable listener raised")

    def on_anonymous_unavailable(
        self, callback: AnonymousUnavailableCallback
    ) -> Callable[[], None]:
        """Subscribe to "the engine refused an anonymous session and the end-user has
        to sign in". The callback receives the engine's error type (one of
        ``ANONYMOUS_UNAVAILABLE_TYPES``). Returns an unsubscribe function.

        The callback runs on the thread that made the refused request, which can be
        one of the SDK's own (a ``ChatController`` worker, the ``t2v-warm-up`` daemon
        thread): Qt/bpy hosts must marshal any UI work to their main thread (for
        example with ``run_on_main``). It does not run after ``close()``."""
        self._anonymous_unavailable_listeners.append(callback)

        def unsubscribe() -> None:
            try:
                self._anonymous_unavailable_listeners.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    # ── Partner config, models, completions, warm-up ─────────────────────

    def get_config(self) -> PartnerConfig:
        """The partner's configuration (``GET /v1/config``): default models, allow-lists,
        tier-gated models and feature flags. Cached for the life of a login and fetched
        once even when several callers ask at the same time: they all get the same
        answer, or the same error — the very same exception object, raised in each
        caller's thread, so its traceback grows with every raise; read ``type`` and
        the message rather than the traceback. A failure is not cached, so the next
        call retries. Invalidated on any auth state change (or by
        ``invalidate_config_cache()``).

        The allow-lists and gates are advisory here (``None`` = no partner override):
        the engine enforces them on every request.
        """
        with self._config_lock:
            cached = self._config_cache
            if cached is not None:
                return cached
            flight = self._config_flight
            owner = flight is None
            if flight is None:
                flight = self._config_flight = _ConfigFlight()
        if owner:
            try:
                flight.result = _parse_partner_config(self._client.request("/v1/config"))
            except Exception as exc:
                flight.error = exc
            finally:
                with self._config_lock:
                    # Cached only while still registered: an auth change meanwhile
                    # detached it, and an answer for the previous login must not outlive it.
                    if self._config_flight is flight:
                        self._config_flight = None
                        if flight.result is not None:
                            self._config_cache = flight.result
                flight.done.set()
        else:
            flight.done.wait()  # bounded by the owner's request timeout
        if flight.result is None:
            raise flight.error
        return flight.result

    def invalidate_config_cache(self) -> None:
        """Drop the cached partner config so the next ``get_config()`` refetches. A
        request already in flight still answers its own callers, but is not cached and
        no later caller waits for it."""
        with self._config_lock:
            self._config_cache = None
            self._config_flight = None

    def list_models(self) -> ModelsResponse:
        """The LLM models this end-user may pick (``GET /v1/models``). ``Model.name`` is
        the display name when the catalogue has one — show ``name or id``."""
        self._require_session()
        data = self._client.request("/v1/models")
        items = data.get("data", []) if isinstance(data, dict) else []
        models = [
            Model(
                id=str(item["id"]),
                object=str(item.get("object", "model")),
                created=item["created"] if isinstance(item.get("created"), int) else 0,
                owned_by=str(item.get("owned_by", "talk2view")),
                name=item["name"] if isinstance(item.get("name"), str) else None,
            )
            for item in items
            if isinstance(item, dict) and item.get("id")
        ]
        return ModelsResponse(object="list", data=models)

    def completions(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """A direct, non-agentic model call routed through the engine
        (``POST /v1/completions``; e.g. MedGemma). The response is the provider's
        OpenAI-shaped completion, passed through untouched. ``model`` is a model id:
        ``DEFAULT_MODEL`` is refused (``TypeError``) before any request."""
        _refuse_default_model(model, "completions()")
        body: Dict[str, Any] = {"model": model, "messages": messages}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        self._require_session(warm=True)
        result = self._client.request(
            "/v1/completions", method="POST", body=body, timeout=timeout
        )
        return result if isinstance(result, dict) else {}

    def warm_up(self) -> None:
        """Ask the engine to get this end-user ready for their first AI call.

        A new end-user's virtual key is minted on first use and that takes seconds;
        call this at the first sign of intent (a first keystroke, a mic tap) so it
        happens while they are still typing. Returns at once: the request — and, with
        ``anonymous_auto_start``, the anonymous start before it — runs on a daemon
        thread, so this is safe on a UI thread and inside an ``on_auth_state_change``
        listener.

        Safe to call as often as you like: one attempt per identity, even when it
        failed, and it never raises. An engine without ``POST /v1/account/warm`` (a
        404) is a silent no-op, as is any other failure (logged at DEBUG); the first
        real request then mints the key itself. Does nothing while nobody is signed
        in, unless ``anonymous_auto_start`` is on; a refused or failed anonymous start
        is then not retried here until the auth state changes (``chat()`` still
        starts its own). Does nothing after ``close()``.

        Threads: an anonymous start made here runs the ``on_auth_state_change`` (or
        ``on_anonymous_unavailable``) listeners on the ``t2v-warm-up`` daemon thread,
        so Qt/bpy hosts must marshal any UI work in them to their main thread.
        ``close()`` does not interrupt a warm-up request already on the wire: that
        thread finishes when its request does (at most the client's timeout), and
        its answer is ignored.
        """
        if self._closed:
            return
        signed_in = self.auth.is_authenticated()
        if not signed_in and not self._anonymous_auto_start:
            return
        if not self._claim_warm_up(self._warm_identity() if signed_in else _SIGNED_OUT_IDENTITY):
            return
        threading.Thread(
            target=self._warm_up_in_background,
            args=(signed_in,),
            name="t2v-warm-up",
            daemon=True,
        ).start()

    def _warm_identity(self) -> str:
        return self._current_user_id() or self.storage.get("access_token") or ""

    def _claim_warm_up(self, identity: str) -> bool:
        """True for the one caller that gets to warm ``identity`` up."""
        with self._warm_lock:
            if self._warmed_identity == identity:
                return False
            self._warmed_identity = identity
            return True

    def _warm_up_in_background(self, signed_in: bool) -> None:
        try:
            if not signed_in:
                # Shared with every other caller. Holds no lock of ours, so the auth
                # listeners it may run here (this SDK's own hook included) never wait on it.
                # A keystroke or a mic tap: a message is on its way, so a warm start.
                flight = self._shared_anonymous_start(warm=True)
                if flight is not None and not flight.result:
                    return  # the signed-out claim stays until the auth state changes
                if not self._claim_warm_up(self._warm_identity()):
                    return  # a warm_up() for the new identity got there first
            if self._closed:
                return
            self._client.request("/v1/account/warm", method="POST")
        except Exception as exc:
            logger.debug("warm_up skipped: %s", exc)

    def _require_session(self, warm: bool = False) -> None:
        """With ``anonymous_auto_start``, start the demo session before an
        authenticated call. ``warm`` for a call that is itself the end-user's message
        (``chat()``, ``completions()``): the engine then mints the guest's key at once.

        Raises ``AuthenticationError`` (``type="sign_in_required"``, caused by the
        ``AnonymousUnavailableError``) only for a refusal only signing in can fix.
        Any other failed start (the network, a 429, a 5xx, a garbled answer)
        raises that start's own error, the same exception object for every caller
        that shared it: a blip must not read as "sign in".
        """
        if not self._anonymous_auto_start:
            return
        flight = self._shared_anonymous_start(warm)
        if flight is None or flight.result:
            return
        if isinstance(flight.error, AnonymousUnavailableError):
            raise AuthenticationError(
                "Sign in to continue.", "sign_in_required", None
            ) from flight.error
        raise flight.error

    def set_chat_defaults(
        self,
        *,
        model: Union[str, _DefaultModel, None] = None,
        capabilities: Optional[Capabilities] = None,
    ) -> None:
        """Change the model and/or capabilities used by chat sessions from now on.

        Both are bound to a ``T2VSession`` when it is created, so when either changes the
        next ``chat()`` does not reuse the current session object: it builds one with the new
        settings (the engine keeps one session per end-user and hands it back, switched to
        the new model, conversation and all). Until then the object stays, so an approval
        pending on it can still be answered (``respond_to_approval``, on the model it started
        with). ``None`` leaves that setting as it is.

        ``model=DEFAULT_MODEL`` forgets the model: chat then sends none and the engine uses
        the partner's default (TS Settings' "Default"). The engine keeps the model a
        session last used when a message names none, so when this replaces a model the
        next ``chat()`` deletes the engine session and starts a fresh one first, as a
        skills reset does; the conversation is the history the caller replays
        (``ChatController`` replays its own). With no session object in hand (an earlier
        change dropped it) that ``chat()`` recycles the one the create hands back, which the
        engine may have kept. The reset stays pending until the engine confirms a
        ``DELETE``: a turn whose ``DELETE`` fails runs on the session it has, and the next
        turn tries again. It applies as well when no model is set here but the session
        was created with one (``create_session(model=...)``). A later explicit choice
        cancels it: a named ``model`` here, or ``create_session(model=...)``; the session
        then switches to that model and keeps its conversation. Nothing is sent from
        here: it only writes fields, and may be called under a caller's lock.
        """
        fresh = False
        stale = False
        if isinstance(model, _DefaultModel):
            if self._model is not None or self._named_session_model is not None:
                self._model = None
                fresh = True
        elif model is not None and model != self._model:
            self._model = model
            self._fresh_session = False  # an explicit choice supersedes a pending reset
            stale = True
        if capabilities is not None:
            capabilities = self._with_memory(capabilities)
        if capabilities is not None and capabilities != self._capabilities:
            self._capabilities = capabilities
            stale = True
        if fresh:
            # chat() deletes the session by its id before the next turn. Written after
            # _model: _delete_session relies on that order (see there).
            self._fresh_session = True
        if stale:
            # The session object is kept, for an approval pending on it; chat() sees the
            # counts differ and builds a new one (so does a create in flight right now).
            self._defaults_version += 1

    def chat(
        self,
        message: str,
        system_prompt: Optional[str] = None,
        history: Optional[List[ChatMessage]] = None,
        attachments: Optional[List[Attachment]] = None,
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[ChatEvent]:
        """Send a message with automatic session management and tool handling.

        Creates a session if one doesn't exist. If the engine has lost the
        session (a deploy, a restart, an eviction) and nothing of the reply has
        arrived yet, a new session is created and the turn is resent once. Not when
        someone else has signed in since the turn began: its history and memory are
        the previous user's, so it ends with a ``session_lost`` error instead.

        ``stop()`` (or ``cancel.cancel()``) from another thread ends the
        iteration quietly; events already yielded are kept. A token that is
        already cancelled ends the turn before a session is even created.
        """
        token = cancel if cancel is not None else CancelToken()
        self._active_cancel = token
        try:
            if token.cancelled:
                return  # nothing to send — don't create a session for it
            self._require_session(warm=True)
            if token.cancelled:
                return  # stop() landed during the anonymous start
            user_id = self._auth_user_id  # whose turn it is, a guest just started included
            session = self._current_session
            known_fresh = False
            if session is not None:
                # set_chat_defaults changed the model or capabilities since this session's
                # create started: it was built on the old settings, and is not reused (a new
                # create follows, which switches the engine session to them).
                stale = self._session_version != self._defaults_version
                if not stale:
                    # Send a pending skill edit before the turn (never raises). On an engine
                    # that honours `replace` this is how a removed or disabled skill leaves
                    # the session — no recycle.
                    self.skills.flush()
                if self._fresh_session or (not stale and self.skills.needs_session_reset()):
                    # The engine still holds a removed/disabled skill it cannot drop (it
                    # merges, or the replace failed) and the controller could not recycle
                    # while busy: do it now, before the turn. Or DEFAULT_MODEL replaced a
                    # model, which this session would keep using: only a new one starts on
                    # the partner's default. A stale session is still the one the engine
                    # holds for this end-user, so its id is the right one to delete.
                    known_fresh = self._delete_session()
                    session = None
                elif stale:
                    session = None
            if session is None:
                session = self._create_session(known_fresh=known_fresh)
                if self._fresh_session or self.skills.needs_session_reset():
                    # The new session's skills register failed on a session the engine
                    # may have kept, still holding a removed skill. Or a DEFAULT_MODEL
                    # reset is still pending: no DELETE confirmed it (there was no session
                    # object to delete, or its DELETE failed), so the create may have
                    # handed back a session the engine kept, on its old model. Recycle
                    # once — at most once a turn, so this cannot loop — and run the turn
                    # on the new one. A reset whose DELETE fails again waits for the next.
                    session = self.recycle_session()
            # Once per turn: the same field rides /messages, every /resume and the resend.
            memory, field = self._memory_turn(message)
            events = self._send_with_session_recovery(
                session,
                token,
                message,
                system_prompt,
                history,
                attachments,
                field,
                user_id=user_id,
            )
            yield from memory.observe_turn(message, events) if memory is not None else events
        except Cancelled:
            return
        finally:
            if self._active_cancel is token:
                self._active_cancel = None

    def _send_with_session_recovery(
        self,
        session: T2VSession,
        cancel: CancelToken,
        message: str,
        system_prompt: Optional[str],
        history: Optional[List[ChatMessage]],
        attachments: Optional[List[Attachment]],
        memory: Optional[Dict[str, Any]] = None,
        *,
        user_id: Optional[str],
    ) -> Iterator[ChatEvent]:
        """Send a turn, and recover once if the engine has lost the chat session.

        A new session rebuilds the conversation from the history this turn
        replays, so resending is safe — but only before any event has been
        yielded: once the reply has started, a client tool handler may already
        have run, and resending could run it twice. ``memory``, the turn's memory
        field, goes with the resend as it went the first time. ``user_id`` (required,
        so no caller can skip the check) is who was signed in as the turn began:
        when that has changed by the resend, the history and the memory field belong
        to someone else, so nothing is resent and the turn ends with ``session_lost``.
        """
        started = False
        try:
            for event in session.send_message(
                message,
                system_prompt=system_prompt,
                history=history,
                attachments=attachments,
                cancel=cancel,
                memory=memory,
            ):
                started = True
                yield event
            return
        except T2VError as err:
            if not is_session_gone(err):
                raise
            if started:
                self._current_session = None
                yield ChatEvent(
                    type="error", message=SESSION_LOST_MESSAGE, error_type="session_lost"
                )
                return
            # stop() landed between the 404 and here — don't recover into a
            # turn nobody wants anymore.
            if cancel.cancelled:
                raise

        self._current_session = None
        deletes = self._deletes
        new_session = self.create_session()
        # clear_session() may have run on another thread while create_session() was out,
        # during the create itself or while its tools and skills were re-registered. Only
        # resend if nothing cleared the session meanwhile and this recovery's session is
        # still the one in play.
        if self._deletes != deletes or not self._is_current_session(new_session):
            return
        if self._auth_user_id != user_id:
            # Someone else signed in during the turn: the session just created is theirs,
            # and the history and the memory field are the previous user's.
            yield ChatEvent(type="error", message=SESSION_LOST_MESSAGE, error_type="session_lost")
            return
        for listener in list(self._session_recovered_listeners):
            try:
                listener(new_session.id)
            except Exception:
                # A partner's listener must not take the recovered turn down
                # with it, nor strand the listeners queued behind it.
                logger.exception("on_session_recovered listener raised")
        yield from new_session.send_message(
            message,
            system_prompt=system_prompt,
            history=history,
            attachments=attachments,
            cancel=cancel,
            memory=memory,
        )

    def respond_to_approval(
        self,
        approval: ChatEvent,
        decision: Decision,
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[ChatEvent]:
        """Answer an ``approval_required`` event from chat().

        If the engine lost the chat session before the answer arrived, nothing
        is resent (an approved client tool has already run here): the session
        is dropped and a ``session_lost`` error event is yielded instead.

        This is a generator, so a ``SessionError`` for "no active session" is
        raised on the first ``next()``, not when it is called.
        """
        session = self._current_session
        if session is None:
            raise SessionError("No active session. Start a conversation first.")
        token = cancel if cancel is not None else CancelToken()
        self._active_cancel = token
        memory = self._memory  # an approval pauses a turn only an open memory follows
        try:
            events = session.respond_to_approval(approval, decision, cancel=token)
            yield from memory.observe_approval(events) if memory is not None else events
        except Cancelled:
            return
        except T2VError as err:
            if not is_session_gone(err):
                raise
            self._current_session = None
            # Only an APPROVED CLIENT tool ran on this side, so only that case
            # warns that the step may already have been applied. A platform
            # tool runs on the engine, which never got the approval.
            nothing_applied = not decision.approved or approval.kind == "engine"
            yield ChatEvent(
                type="error",
                message=(
                    SESSION_LOST_AFTER_DENY_MESSAGE
                    if nothing_applied
                    else SESSION_LOST_MESSAGE
                ),
                error_type="session_lost",
            )
        finally:
            if self._active_cancel is token:
                self._active_cancel = None

    def stop(self) -> None:
        """Stop the in-flight chat()/respond_to_approval() stream.

        Safe to call from any thread. Events already yielded are kept; the
        iteration then ends quietly. No-op when nothing is streaming.

        chat() is a generator, so it registers nothing until the consumer's
        first ``next()``: a stop() between the call and that first ``next()``
        is lost, and the turn then runs normally. Pass your own
        ``CancelToken`` when you need to cancel before the first event.
        """
        token = self._active_cancel
        if token is not None:
            token.cancel()

    def upload_attachment(
        self,
        file: Union[str, Path, bytes],
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Attachment:
        """Upload a file to attach to chat messages.

        Accepts images (png/jpeg/webp/gif) and PDFs up to the server's size
        limit. Pass the result via chat(..., attachments=[attachment]). Any
        other type — or one that is empty or cannot be guessed — raises
        ``T2VError`` (``type="unsupported_attachment_type"``) before any request.

        Args:
            file: Path to a file, or raw bytes.
            filename: Required when passing bytes; inferred from the path otherwise.
            mime_type: Guessed from the filename when omitted (``None``).
        """
        if isinstance(file, (str, Path)):
            name = filename or Path(file).name
        else:
            name = filename or "file"

        if mime_type is None:
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        else:
            mime = mime_type  # as given: an explicit "" is an empty type, not "guess"
        # Rejected up front, before the file is even read and before any request (the
        # anonymous start included): a file dialog lets users bypass any filter, and
        # this guards headless callers too. Only an allow-listed type passes, so an
        # empty or unknown one fails closed.
        if not is_allowed_attachment_type(mime):
            raise T2VError(
                "Unsupported attachment type {!r}. Talk2View supports {}.".format(
                    mime, ATTACHMENT_TYPES_LABEL
                ),
                "unsupported_attachment_type",
            )
        data = Path(file).read_bytes() if isinstance(file, (str, Path)) else file

        self._require_session()
        result = self._client.upload_request(
            "/v1/attachments",
            files={"file": (name, data, mime)},
        )
        return Attachment(
            id=result["id"],
            filename=result["filename"],
            mime_type=result["mime_type"],
            size_bytes=result["size_bytes"],
        )

    def get_attachment_content(self, attachment_id: str) -> AttachmentContent:
        """Download an attachment's bytes (``GET /v1/attachments/{id}/content``): one of
        this end-user's uploads, or an image the agent generated. Missing, expired and
        someone else's all raise a 404 ``T2VError`` (type ``attachment_not_found``).

        ``attachment_id`` must be a UUID, as the engine types it: anything else raises
        ``T2VError`` (type ``invalid_attachment_id``) before any request — an id such
        as ``".."`` or ``"../x"`` could only address another route with this
        authenticated GET. At most ``client.MAX_DOWNLOAD_BYTES`` are downloaded."""
        try:
            canonical = str(uuid.UUID(attachment_id))
        except (ValueError, TypeError, AttributeError):
            raise T2VError("Not an attachment id.", "invalid_attachment_id") from None
        # Quoted as one path segment too (a canonical UUID never needs it): a second
        # layer, should the check above ever loosen.
        data, mime = self._client.request_bytes(
            "/v1/attachments/{}/content".format(quote(canonical, safe=""))
        )
        return AttachmentContent(data=data, mime_type=mime)

    def get_session(self) -> Optional[T2VSession]:
        """Get the current session (if any)."""
        return self._current_session

    def clear_session(self) -> None:
        """Clear the current session so the next chat() creates a new one.

        Also deletes the session on the server so conversation history is reset.
        With ``memory_path``, the memory starts a new conversation too, and the one
        that ended is queued for extraction (``T2VMemory.new_conversation``).
        """
        self._delete_session()
        memory = self._memory
        if memory is not None:
            memory.new_conversation()

    def _delete_session(self) -> bool:
        """``clear_session``. True only when the engine confirmed the DELETE: the
        next create then makes a brand-new session. A failure is swallowed
        (fire-and-forget), and so is a 404 — which may mean the engine holds a
        different session for this user, one the next create hands back."""
        session = self._current_session
        deleted = False
        if session is not None:
            try:
                self._client.request(
                    "/v1/sessions/{}".format(session.id),
                    method="DELETE",
                )
                deleted = True
            except Exception:  # noqa: S110 — fire-and-forget session cleanup
                pass
        self._current_session = None
        self._deletes += 1
        if deleted:
            # A pending DEFAULT_MODEL reset is done: the next create starts a new engine
            # session. A reset set meanwhile loses its flag here only after it has cleared
            # _model (set_chat_defaults writes _model first), and that next create reads
            # _model after this, so it still starts on the partner's default.
            self._fresh_session = False
            self._named_session_model = None  # the engine holds no session for us now
        return deleted

    def detach_session(self) -> None:
        """Forget the engine session without deleting it, so the next ``chat()`` opens a
        fresh one (TS ``detachSession``). Unlike ``clear_session`` no ``DELETE`` goes out:
        the conversation this session belongs to may be one the end-user switches back
        to, and its turn may still be finishing. The transcript is replayed with every
        message, so a new session costs a round trip and nothing else. A session recovery
        in flight sees the change through ``_deletes``, as it does after a delete. With
        ``memory_path``, the memory starts a new conversation too, as at ``clear_session``:
        the one put aside is queued for extraction, and is no longer the current one that
        the next turn's memory leaves out."""
        self._current_session = None
        self._deletes += 1
        memory = self._memory
        if memory is not None:
            memory.new_conversation()

    def close(self) -> None:
        """Close the underlying HTTP client and release pooled connections.

        Skills are closed first, so their debounce timer cannot fire a register
        call into the closed client. A skill edit still waiting for that timer is
        dropped: call ``skills.flush()`` before ``close()`` to send it. The memory
        closes before them: its queued engine calls are dropped, and one in flight
        is not waited for. With a ``ChatController`` on this client, close the
        controller before the Talk2View.

        Long-lived integrations (e.g. a desktop app) should call this on
        shutdown, or use ``Talk2View`` as a context manager.
        """
        with self._close_lock:
            self._closed = True  # an anonymous start still in flight now stores nothing
        self._retire_memory()
        self.skills.close()
        self.metrics.close()  # a turn still waiting for its page report goes out without it
        self._client.close()

    def __enter__(self) -> "Talk2View":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.close()


__all__ = [
    "__version__",
    # Main class
    "Talk2View",
    # Chat state machine
    "ChatController",
    "ControllerBusyError",
    "ControllerClosedError",
    "IdentityChange",
    "ChatSnapshot",
    "Conversation",
    "ConversationSnapshot",
    "ConversationStore",
    "DisplayMessage",
    "HistoryEntry",
    "Op",
    "OpBatch",
    "OpError",
    "PendingApproval",
    "SeqGapError",
    "ToolStep",
    "apply_batch",
    "CANCELLED_APPROVAL_FEEDBACK",
    "DEMO_LIMIT_REACHED",
    # Decorator
    "tool",
    # Modules
    "T2VAuth",
    "T2VClient",
    "T2VPreferences",
    "T2VSession",
    "T2VSkills",
    "T2VTools",
    "T2VVoice",
    # Audio (dictation)
    "AudioCapture",
    "CaptureError",
    "DEFAULT_DICTATION_MODEL",
    # Preferences
    "UserPreferences",
    # Turn timings
    "TurnMetrics",
    "TurnTiming",
    "RequestTiming",
    "ToolTiming",
    "EngineToolTiming",
    "UiTiming",
    # Skills
    "Skill",
    "PlatformSkill",
    "SkillValidationError",
    "SkillsState",
    # Storage
    "TokenStorage",
    "MemoryStorage",
    "FileStorage",
    # Cancellation
    "CancelToken",
    "Cancelled",
    # Dispatchers
    "Dispatcher",
    "InlineDispatcher",
    "QueueDispatcher",
    "TimerPumpDispatcher",
    "qt_dispatcher",
    "bpy_dispatcher",
    "run_on_main",
    # Types
    "Attachment",
    "AttachmentContent",
    "AudioModel",
    "Capabilities",
    "ChatEvent",
    "ChatMessage",
    "ClientToolSchema",
    "Decision",
    "DEFAULT_MODEL",
    "Model",
    "ModelsResponse",
    "PartnerConfig",
    "PermissionCheck",
    "PermissionResult",
    "SignupOutcome",
    "ToolEvent",
    "TranscriptionResponse",
    "User",
    # Tool helpers
    "strip_null_args",
    # Attachments
    "ALLOWED_ATTACHMENT_MIME",
    "ATTACHMENT_ACCEPT",
    "ATTACHMENT_TYPES_LABEL",
    "is_allowed_attachment_type",
    # Errors
    "T2VError",
    "AnonymousUnavailableError",
    "ANONYMOUS_UNAVAILABLE_TYPES",
    "AuthenticationError",
    "PartnerKeyError",
    "ProviderSignInError",
    "SessionError",
    "NetworkError",
    "error_for_type",
    "is_session_gone",
    "SESSION_LOST_MESSAGE",
    "SESSION_LOST_AFTER_DENY_MESSAGE",
]
