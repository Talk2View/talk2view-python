"""AppState: the slices the page shows besides the chat, on the same ``seq`` line.

Each slice is one top-level key of the page's state, next to the chat's keys (which never
collide with these). Shapes reuse the TypeScript SDK's types where one exists:

- ``auth``: ``{"user": {"id", "email", "user_metadata"?} | null, "isAuthenticated",
  "isAnonymous", "oauthPending": {"provider"} | null, "oauthError": {"type", "message"} |
  null}`` (TS ``User``). ``oauthPending`` is the provider sign-in waiting in the browser
  (``ProviderSignIn``); ``oauthError`` the last attempt's failure (a cancel is not one),
  kept until the next ``oauthStart`` or an ``oauthDismissError``, across auth changes;
- ``models``: ``{"list": [TS Model: id, object, created, owned_by, name,
  reasoning_efforts, default_reasoning_effort, gated_reasoning_efforts], "selected": id |
  null, "selectedEffort": effort | null}``; the three effort keys are always present, null
  for a model with no effort control. ``selected`` is the end-user's own choice when it is
  listed, else null (the host's or the partner's default applies); a stored pick of an
  older id that named its effort (``X-thinking``) shows as ``X`` once only ``X`` is
  listed (``model_pick.resolve_pick``). ``selectedEffort`` is the effort the pick runs
  at: the stored one when the model offers it, else the model's default; null when
  nothing is selected or the model has no effort control. Every auth change empties it
  until the refresh lists the new identity's models;
- ``audioModels``: ``{"list": [TS AudioModel: id, name, supported_languages,
  description], "state": "loading" | "ready" | "error"}``, the speech-to-text models
  Settings offers ("Voice model"); read alongside ``models`` (signed in only, emptied and
  put back to "loading" on every auth change), independent of ``capabilities.audio`` (a
  host with no microphone still shows the field, for a stored pick or a future host).
  ``state`` is "ready" once the list has actually been read, even when it comes back
  empty (a real answer with nothing in it, distinct from "loading"), and "error" when the
  read itself failed;
- ``config``: the engine's partner config, TS ``PartnerConfig`` keys (snake_case, the
  eleven fields), or null while signed out or when it could not be read;
- ``capabilities``: ``{"audio": bool, "oauth": bool, "oauthProviders": [str],
  "clipboard": bool}``, ``audio`` exactly "the host's ``audio()`` returned a capture"
  (``AppState(..., audio=...)``, read once, never changes after), with ``oauth`` true
  when ``features.provider_sign_in`` is on and
  the engine offers at least one provider (``T2VAuth.desktop_sign_in_providers``, read on
  every refresh, signed in or not; a failed read keeps the last answer, which starts as
  none, and is tried again ``PROBE_RETRY_S`` later, at most ``PROBE_RETRIES`` times in a
  row; a 404, an engine without the route, is never asked again). ``oauthProviders``
  lists them in display order, ``[]`` when ``oauth`` is false;
- ``theme``: ``{"mode": "light" | "dark", "tokens": {...}}``;
- ``preferences``: TS ``UserPreferences`` (``model``, ``reasoningEffort``, ``sttModel``,
  ``sttLanguage``, ``fontSize``; a key is absent when unset);
- ``results``: the last 20 command results, oldest first (see ``wire.py``);
- ``options`` and ``tools``: the integrator's ``ChatOptions``, read-only (``options.py``);
- ``limits``: ``{"demoLimitReached": bool, "anonymousUnavailable": null | {"type"}}``, the
  two ways a guest is turned away (TS ``GuestLimits``, ``chat/auth-gate.tsx``): the demo
  allowance ran out (``ChatController.on("demoLimitReached")``), or the engine refused a
  guest session (``Talk2View.on_anonymous_unavailable``, or a refused ``startAnonymous``;
  ``type`` is the engine's). Both are lifted when another account comes on show and when
  a real account (not a guest) is signed in, after the ``auth`` slice shows it: the page's
  cue to send ``reload``, which sends the turn the limit stopped again.

Another account on show (the auth slice's ``(signed in, user id)`` changes: a sign-in, a
sign-out, a switch) empties ``results`` and makes the hub forget its replay history
(``EventHub.forget_replay``), so nothing of the previous end-user reaches a page that
resumes from before the change. The command that made the change records its result after.

Every value handed to the hub is plain JSON (``EventHub.set_slice`` refuses anything
else): a command result JSON cannot carry is refused, user metadata JSON cannot carry (a
token store's NaN) is left out, and a stored user whose id or email JSON cannot carry
reads as signed out, as an unreadable token store does.

Threads and locks. The auth and preferences listeners run on whatever SDK thread made the
change; each one reads the LIVE value and records it under ``_lock``, so the slice always
ends on the latest state however deliveries interleave. ``attach`` subscribes before it
seeds, so a change that lands in between is read by the seed or delivered after it. Under
``_lock`` this object takes the token store's lock (reading the auth state), the
preferences' lock and through it the data store's (reading the preferences), and the hub's
lock; nothing takes them the other way. The chat controller's ``demoLimitReached`` and
``modelRefused`` notices run inside its emit window, so its emit lock comes before
``_lock`` too; nothing under ``_lock`` reaches the controller. No lock is held across a
request or a call into ``Talk2View`` that notifies listeners (the refresh thread's own
``preferences.update``, when it forgets a stored effort, runs after it lets go of
``_lock``); ``set_chat_defaults`` (field writes only: no lock, no listener, no request) is
called under ``_lock``, so a pick and a refresh apply in the order they record. Requests
(config, models) run on this object's own ``t2v-ui-refresh`` thread, never in a
listener. A refresh records only what it fetched for the identity the auth slice
shows: one that an auth change overtook, or that ran on tokens this object has not been
told of yet, is dropped, and the change's own refresh follows. It never starts a guest
session: ``list_models()`` does, with ``anonymous_auto_start``, when nobody is signed in,
so it is asked only while this pass is current and someone is. The auth listener is an
ordinary one: ``Talk2View``'s own, an early one, has dropped the cached partner config
before this one wakes the refresh.

Lock order with provider sign-in: ``ProviderSignIn._lock`` -> ``AppState._lock`` -> the
hub's lock. Nothing takes them the other way.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from ..controller import DEMO_LIMIT_REACHED, MODEL_REFUSED
from ..errors import T2VError
from ..preferences import UserPreferences
from ..types import DEFAULT_MODEL, AudioModel, Model, User, _check_reasoning_effort
from .host import HostAdapter, Theme
from .hub import EventHub
from .model_pick import ResolvedPick, legacy_model_effort, resolve_pick
from .options import ChatOptions
from .wire import CommandFailure

if TYPE_CHECKING:
    from .. import Talk2View

__all__ = ["APP_KEYS", "MAX_RESULTS", "AppState", "UnknownModelError"]

logger = logging.getLogger("talk2view.ui")

APP_KEYS = (
    "auth", "models", "audioModels", "config", "capabilities", "theme", "preferences",
    "results", "options", "tools", "limits", "threads", "dictation",
)
MAX_RESULTS = 20
#: A failed providers probe is tried again this long after (at most PROBE_RETRIES times
#: in a row), without waiting for an auth change.
PROBE_RETRY_S = 30.0
PROBE_RETRIES = 3
#: The providers the page has buttons for, in display order.
_PAGE_PROVIDERS = ("apple", "google")
_SINGLE_USE = "an AppState is single use: it cannot attach again after detach()"


class UnknownModelError(LookupError):
    """``selectModel`` named a model the end-user's list does not have."""


def _plain(value: Any) -> Any:
    """A JSON copy of ``value``: ``TypeError`` for what JSON cannot carry, ``ValueError``
    for NaN and the infinities (``JSON.parse`` on the page refuses them too)."""
    return json.loads(json.dumps(value, allow_nan=False))


#: ``_identity`` of a signed-out token store.
_SIGNED_OUT: Tuple[bool, Any] = (False, None)
#: ``_identity`` when reading the store raised: equal to nothing ever shown.
_UNREADABLE = object()


def _identity(t2v: Optional["Talk2View"]) -> Any:
    """``(signed in, user id)`` as the token store has it now; ``_UNREADABLE`` when reading
    it raised."""
    if t2v is None:
        return _SIGNED_OUT
    try:
        if not t2v.auth.is_authenticated():
            return _SIGNED_OUT
        user = t2v.auth.get_user()
    except Exception:
        return _UNREADABLE
    return (True, user.id if user is not None else None)


def _real_account(t2v: Optional["Talk2View"]) -> bool:
    """Whether the token store reads as a signed-in account that is not a guest; False when
    reading it raised."""
    if t2v is None:
        return False
    try:
        return t2v.auth.is_authenticated() and not t2v.auth.is_anonymous()
    except Exception:
        return False


def _user(user: User) -> Dict[str, Any]:
    """The TS ``User`` as plain JSON. An id or email JSON cannot carry (a hand-edited
    store's NaN) raises; metadata JSON cannot carry is left out."""
    out: Dict[str, Any] = _plain({"id": user.id, "email": user.email})
    if user.user_metadata is not None:
        try:
            out["user_metadata"] = _plain(user.user_metadata)
        except Exception:
            logger.warning("the user's metadata is not plain JSON; the page gets the user "
                           "without it")
    return out


def _model(model: Model) -> Dict[str, Any]:
    return {
        "id": model.id,
        "object": model.object,
        "created": model.created,
        "owned_by": model.owned_by,
        "name": model.name,
        "reasoning_efforts": _copy(model.reasoning_efforts),
        "default_reasoning_effort": model.default_reasoning_effort,
        "gated_reasoning_efforts": _copy(model.gated_reasoning_efforts),
    }


def _copy(values: Optional[List[str]]) -> Optional[List[str]]:
    return list(values) if values is not None else None


def _stored_pick(t2v: Optional["Talk2View"]) -> Tuple[Optional[str], Optional[str]]:
    """The stored ``(model, reasoning effort)``; the effort trimmed and lower-cased, and a
    blank one read as none. ``(None, None)`` when there is none or it cannot be read."""
    if t2v is None:
        return None, None
    try:
        prefs = t2v.preferences.get()
        return prefs.model, _check_reasoning_effort(prefs.reasoning_effort)
    except Exception:
        return None, None


def _audio_model(model: AudioModel) -> Dict[str, Any]:
    languages = model.supported_languages
    return {
        "id": model.id,
        "name": model.name,
        "supported_languages": list(languages) if languages is not None else None,
        "description": model.description,
    }


class AppState:
    """The app slices of one ``UIServer``. Single use: once ``detach()`` has run (the
    server stopped, or its start failed), ``attach()`` raises ``RuntimeError``, so a
    server that needs one builds a new ``AppState``.

    ``options`` (default ``ChatOptions()``) is served read-only. ``allow_anonymous=False``
    with a ``Talk2View`` built with ``anonymous_auto_start=True`` raises ``ValueError``: a
    send, an upload or a warm-up would start a guest session behind the page's sign-in
    gate, which the option exists to keep shut."""

    def __init__(
        self,
        t2v: Optional["Talk2View"],
        host: HostAdapter,
        *,
        options: Optional[ChatOptions] = None,
        audio: bool = False,
        skill_import: bool = False,
    ) -> None:
        if options is None:
            options = ChatOptions()
        elif not isinstance(options, ChatOptions):
            raise TypeError("options must be a ChatOptions, not {}".format(type(options).__name__))
        if not options.allow_anonymous and t2v is not None and t2v.anonymous_auto_start:
            raise ValueError(
                "ChatOptions(allow_anonymous=False) needs a Talk2View built without "
                "anonymous_auto_start: it would start a guest session behind the sign-in gate"
            )
        self._options = options
        self._t2v = t2v
        self._host = host
        self._audio = audio
        self._skill_import = skill_import
        self._lock = threading.Lock()
        self._hub: Optional[EventHub] = None
        self._results: List[Dict[str, Any]] = []
        self._models: List[Dict[str, Any]] = []
        self._audio_models: List[Dict[str, Any]] = []
        #: "loading" until the first refresh answers, then "ready" (the list, maybe empty)
        #: or "error" (the call failed); reset to "loading" on every auth change, the same
        #: as the list itself (the page must not read an empty or failed catalogue
        #: as "still loading" forever).
        self._audio_models_state = "loading"
        self._selected: Optional[str] = None
        self._selected_effort: Optional[str] = None  # models.selectedEffort
        #: The (model, effort) this object last handed to Talk2View.
        self._applied: Optional[Tuple[str, Optional[str]]] = None
        #: The engine refused a turn for its model or effort: the next refresh pass drops
        #: the effort in force (an auth change clears it: it was the previous identity's).
        self._effort_refused = False
        self._generation = 0  # +1 per auth change: a refresh from before it is dropped
        self._shown: Any = _SIGNED_OUT  # the `_identity` the auth slice last recorded
        self._demo_limit = False  # the `limits` slice
        self._anonymous_unavailable: Optional[str] = None
        self._wake = threading.Event()
        self._stopping = False
        self._thread: Optional[threading.Thread] = None
        self._unsubscribe: List[Callable[[], None]] = []
        self._clipboard = False
        self._oauth_providers: List[str] = []  # the engine's answer; none until read
        self._oauth_pending: Optional[str] = None
        self._oauth_error: Optional[Dict[str, str]] = None
        self._auth_value: Dict[str, Any] = {
            "user": None, "isAuthenticated": False, "isAnonymous": False,
        }
        self._probe_failures = 0
        self._probe_timer: Optional[threading.Timer] = None
        self._probe_absent = False  # the engine has no providers route: never ask again

    @property
    def t2v(self) -> Optional["Talk2View"]:
        return self._t2v

    @property
    def options(self) -> ChatOptions:
        """The integrator's options: the ``options`` and ``tools`` slices (read-only)."""
        return self._options

    # ── Lifecycle ──

    def attach(self, hub: EventHub, controller: Any = None) -> None:
        """Follow ``t2v`` and ``controller``'s ``demoLimitReached`` and ``modelRefused``
        notices (a controller without ``on()`` has none), seed every slice into ``hub``,
        start the refresh thread.
        Raises ``RuntimeError`` once ``detach()`` has run, also when it runs on another
        thread while this call is under way: nothing is left subscribed either way, nor when
        a subscription raises (a controller's ``on()``, say), which is raised."""
        with self._lock:
            if self._stopping:
                raise RuntimeError(_SINGLE_USE)
        theme = self._host.theme().to_wire()  # host code: never under our lock
        options, tools = self._options.to_wire(), self._options.tools_to_wire()
        clipboard = getattr(self._host, "supports_clipboard", False) is True
        t2v = self._t2v
        # Before the seed: a change from here on is either read by it or delivered.
        followed: List[Callable[[], None]] = []
        try:
            if t2v is not None:
                followed.append(t2v.auth.on_auth_state_change(self._on_auth))
                followed.append(t2v.preferences.on_change(self._on_preferences))
                followed.append(t2v.on_anonymous_unavailable(self.note_anonymous_unavailable))
            notices = getattr(controller, "on", None)
            if callable(notices):
                followed.append(notices(DEMO_LIMIT_REACHED, self._on_demo_limit))
                followed.append(notices(MODEL_REFUSED, self._on_model_refused))
        except BaseException:
            for unsubscribe in followed:
                unsubscribe()
            raise
        if followed:
            with self._lock:
                detached = self._stopping
                if not detached:
                    self._unsubscribe.extend(followed)
            if detached:  # detach() has run meanwhile, and could not see these
                for unsubscribe in followed:
                    unsubscribe()
                raise RuntimeError(_SINGLE_USE)
        try:
            with self._lock:
                if self._stopping:
                    raise RuntimeError(_SINGLE_USE)
                self._hub = hub
                self._clipboard = clipboard
                hub.set_slice("theme", theme)
                self._record_capabilities_locked()
                hub.set_slice("results", [])
                hub.set_slice("config", None)
                hub.set_slice("options", options)
                hub.set_slice("tools", tools)
                self._record_limits_locked()
                self._record_models_locked()
                self._record_audio_models_locked()
                self._record_auth_locked()
                self._record_preferences_locked()
        except BaseException:
            self._unfollow()  # a failed attach leaves no listener behind
            with self._lock:
                self._hub = None
            raise
        self._thread = threading.Thread(target=self._refresh_loop, name="t2v-ui-refresh",
                                        daemon=True)
        self._thread.start()
        self.refresh()

    def detach(self, timeout: float = 2.0) -> None:
        """Stop following ``t2v`` and stop the refresh thread. Idempotent, and final: see
        the class docstring."""
        with self._lock:
            # First: an attach() on another thread records its subscriptions only while
            # this is unset, so every one of them is either unsubscribed below or by it.
            self._stopping = True
            self._hub = None
            timer, self._probe_timer = self._probe_timer, None
        if timer is not None:
            timer.cancel()
        self._unfollow()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _unfollow(self) -> None:
        """Unsubscribe from ``t2v``: the list is swapped under ``_lock``, and the
        unsubscribes (which take their own locks) run with no lock of ours held."""
        with self._lock:
            unsubscribes, self._unsubscribe = self._unsubscribe, []
        for unsubscribe in unsubscribes:
            unsubscribe()

    # ── Called by the UI server ──

    def set_theme(self, theme: Theme) -> None:
        wire = theme.to_wire()  # copies the host's token mapping: not under our lock
        with self._lock:
            if self._hub is not None:
                self._hub.set_slice("theme", wire)

    def record_result(self, result: Dict[str, Any]) -> None:
        """Keep a JSON copy of ``result``. One JSON cannot carry raises (``TypeError``, or
        ``ValueError`` for NaN) and is not kept, so it cannot block the results after it."""
        copied = _plain(result)
        with self._lock:
            self._results = (self._results + [copied])[-MAX_RESULTS:]
            if self._hub is not None:
                self._hub.set_slice("results", list(self._results))

    def refresh(self) -> None:
        """Re-read config and models on the refresh thread (coalesced; never blocks)."""
        self._wake.set()

    def select_model(self, model_id: str, reasoning_effort: Optional[str] = None) -> None:
        """The end-user's pick: must be in their list, and the page must show the account
        that is signed in (another one's tokens can be stored before this object is told
        of them). ``reasoning_effort`` must be one the model declares
        (``CommandFailure("reasoning_effort_not_supported")`` otherwise, before anything is
        stored); a gated one is taken like any other, since the engine decides at the turn
        whether this end-user may use it. None picks the model at its default effort, so
        another model's effort is never carried over. Remembered as a preference (the
        model and the effort together), then applied to the session defaults. An auth
        change that took the list away meanwhile refuses it too: nothing is applied, and
        the remembered pick is put back as it was. Router thread only (it notifies
        listeners)."""
        t2v = self._t2v
        effort = _check_reasoning_effort(reasoning_effort)
        with self._lock:
            entry = self._entry_locked(model_id) if self._shows_live_locked() else None
        if t2v is None or entry is None:
            raise UnknownModelError(model_id)
        if effort is not None and effort not in (entry.get("reasoning_efforts") or ()):
            raise CommandFailure(
                "reasoning_effort_not_supported",
                "That thinking level is not available for this model.",
            )
        stored = t2v.preferences.get()
        previous = (stored.model, stored.reasoning_effort)
        # Runs our listener: not under _lock.
        t2v.preferences.update(model=model_id, reasoning_effort=effort)
        with self._lock:
            taken = self._listed_locked(model_id) and self._shows_live_locked()
            if taken:
                t2v.set_chat_defaults(model=model_id, reasoning_effort=effort)
                self._applied = (model_id, effort)
                self._selected = model_id
                self._selected_effort = (
                    effort if effort is not None else entry.get("default_reasoning_effort")
                )
                self._record_models_locked()
        if not taken:
            self._forget_pick(t2v, (model_id, effort), previous)
            raise UnknownModelError(model_id)

    def select_default_model(self) -> None:
        """The end-user's "Default": their pick (model and effort) is forgotten, and a
        model this object applied goes too (``DEFAULT_MODEL``: the next turn starts on a
        fresh engine session, on the partner's default). So does the model in use when it
        is the pick being forgotten, or the model an older id like ``X-thinking`` stands
        for: another ``AppState`` over this ``Talk2View`` applied it (a server the host
        rebuilt), and it is this end-user's all the same. A model the host itself set,
        which no stored pick names, stays. Always allowed: it shows nothing of any
        account. Router thread only (it notifies listeners)."""
        t2v = self._t2v
        if t2v is None:
            return
        forgotten = t2v.preferences.get().model
        # Runs our listener: not under _lock.
        t2v.preferences.update(model=None, reasoning_effort=None)
        with self._lock:
            self._reset_applied_locked(t2v, forgotten)
            self._selected = self._selected_effort = None
            self._record_models_locked()

    def select_stt_model(self, model_id: Optional[str]) -> None:
        """The end-user's voice model (None: the partner's default). Not checked against
        the list: the engine refuses a model it does not offer at transcription time
        (``model_not_allowed``), and a stored pick outside the list is shown as an orphan
        by the page (TS settings.tsx). Router thread only (it notifies listeners)."""
        t2v = self._t2v
        if t2v is None:
            raise CommandFailure(
                "account_unavailable", "This chat is not connected to a Talk2View account."
            )
        t2v.preferences.update(stt_model=model_id)

    def select_stt_language(self, language: Optional[str]) -> None:
        """The spoken-language hint (None: detect automatically). Router thread only."""
        t2v = self._t2v
        if t2v is None:
            raise CommandFailure(
                "account_unavailable", "This chat is not connected to a Talk2View account."
            )
        t2v.preferences.update(stt_language=language)

    def note_anonymous_unavailable(self, error_type: str) -> None:
        """The engine refused a guest session (``type`` is its reason): the ``limits``
        slice says so. A refusal that a real account's sign-in overtook is moot and
        dropped; anything else records it, a token store that cannot be read included
        (fail closed: the auth slice shows that as signed out). An auth change lifts it.
        ``Talk2View.on_anonymous_unavailable`` calls it, on whichever thread made the
        request, and so does the router for a refused ``startAnonymous``."""
        with self._lock:
            if _real_account(self._t2v):
                return
            self._anonymous_unavailable = str(error_type)
            self._record_limits_locked()

    def oauth_providers(self) -> List[str]:
        """The providers ``oauthStart`` may use now: none while the integrator has the
        buttons off or the engine offers none."""
        with self._lock:
            return self._offered_locked()

    def set_oauth(self, *, pending: Optional[str], error: Optional[Dict[str, str]]) -> None:
        """``ProviderSignIn``'s report: the attempt waiting in the browser, and the last
        one's failure. Republishes the auth slice as last recorded; it never re-reads the
        token store, which only ``_on_auth`` does (it would skip that listener's work)."""
        with self._lock:
            self._oauth_pending = pending
            self._oauth_error = None if error is None else dict(error)
            self._publish_auth_locked()

    def dismiss_oauth_error(self) -> None:
        """``oauthDismissError``: the end-user closed the message. A waiting sign-in
        stays as it is."""
        with self._lock:
            self._oauth_error = None
            self._publish_auth_locked()

    def note_desktop_sign_in_disabled(self) -> None:
        """The engine refused a desktop sign-in because the partner switch is off: the
        buttons go until a refresh reads the switch on again."""
        with self._lock:
            self._oauth_providers = []
            self._record_capabilities_locked()

    @staticmethod
    def _forget_pick(
        t2v: "Talk2View",
        refused: Tuple[str, Optional[str]],
        previous: Tuple[Optional[str], Optional[str]],
    ) -> None:
        """Put the remembered pick (model and effort) back as it was before a refused pick
        — unless something changed it since — so a later refresh never restores what was
        refused. Notifies listeners: never under ``_lock``."""
        if previous == refused:
            return
        try:
            stored = t2v.preferences.get()
            if (stored.model, stored.reasoning_effort) == refused:
                t2v.preferences.update(model=previous[0], reasoning_effort=previous[1])
        except Exception:
            logger.warning("could not put the remembered model back after a refused pick",
                           exc_info=True)

    # ── Listeners (any SDK thread) ──

    def _on_auth(self, _given: Optional[User]) -> None:
        forget: Optional[EventHub] = None
        try:
            with self._lock:
                self._generation += 1
                # First, whatever the new state turns out to be: any change may be a new
                # identity (a switch signs no one out first), so nothing of the previous
                # one stays on show, or selectable, until the refresh lists the new one's.
                self._models, self._selected, self._selected_effort = [], None, None
                self._effort_refused = False
                self._record_models_locked()
                self._audio_models = []
                self._audio_models_state = "loading"
                self._record_audio_models_locked()
                if self._hub is not None:
                    self._hub.set_slice("config", None)
                shown = self._shown
                auth = self._record_auth_locked()
                if self._shown != shown or (auth["isAuthenticated"] and not auth["isAnonymous"]):
                    # Another account on show, or a real account signed in: what turned a
                    # guest away is lifted. After the auth op: the page's cue to `reload`.
                    self._demo_limit, self._anonymous_unavailable = False, None
                    self._record_limits_locked()
                if self._shown != shown:
                    # Another account on show: the previous one's command results go, and
                    # the hub forgets its replay history. The command that made the change
                    # records its own result after this listener returns.
                    self._results = []
                    if self._hub is not None:
                        self._hub.set_slice("results", [])
                    forget = self._hub
        finally:
            self._wake.set()
        if forget is not None:
            # Not under _lock: it asks the server for a rebase. The chat controller's auth
            # listener is an early one, so it has dropped the conversation by now, and the
            # batch that did is behind the floor, or (a change made from inside one of its
            # deliveries) is still to come and never kept for replay (see forget_replay).
            forget.forget_replay()

    def _on_preferences(self, _given: UserPreferences) -> None:
        with self._lock:
            self._record_preferences_locked()

    def _on_demo_limit(self) -> None:
        """``ChatController.on("demoLimitReached")``: the guest's allowance ran out. It runs
        on the controller's emitting thread, inside its delivery window, so it only records
        (``_lock`` and the hub's lock; neither reaches back into the controller)."""
        with self._lock:
            self._demo_limit = True
            self._record_limits_locked()

    def _on_model_refused(self) -> None:
        """``ChatController.on("modelRefused")``: the engine, which knows whether this
        end-user may use it, refused a turn for its model or effort. The next refresh pass
        forgets the effort in force, so the next turn runs at the model's default, and
        re-reads the list so Settings shows what is offered now. A refusal with no effort
        in force (the whole model is refused) changes nothing. Runs inside the
        controller's delivery window, so it only records and wakes the refresh."""
        with self._lock:
            self._effort_refused = True
        self.refresh()

    # ── Recording (``_lock`` held) ──

    def _record_auth_locked(self) -> Dict[str, Any]:
        """Record the live auth state, and return it. A store that cannot be read, or a
        stored user JSON cannot carry, reads as signed out (and ``_shown`` then matches no
        refresh)."""
        value: Dict[str, Any] = {"user": None, "isAuthenticated": False, "isAnonymous": False}
        shown: Any = _SIGNED_OUT
        if self._t2v is not None:
            try:
                authenticated = self._t2v.auth.is_authenticated()
                user = self._t2v.auth.get_user() if authenticated else None
                value = {
                    "user": _user(user) if user is not None else None,
                    "isAuthenticated": authenticated,
                    "isAnonymous": authenticated and self._t2v.auth.is_anonymous(),
                }
                shown = (authenticated, user.id if user is not None else None)
            except Exception:
                logger.warning("could not read the signed-in user; showing signed out",
                               exc_info=True)
                value = {"user": None, "isAuthenticated": False, "isAnonymous": False}
                shown = _SIGNED_OUT
        self._shown = shown
        self._auth_value = value
        self._publish_auth_locked()
        return value

    def _publish_auth_locked(self) -> None:
        """The auth slice: the state ``_record_auth_locked`` last read, and the provider
        sign-in's two keys."""
        if self._hub is not None:
            value = dict(self._auth_value)
            value["oauthPending"] = (
                None if self._oauth_pending is None else {"provider": self._oauth_pending}
            )
            value["oauthError"] = None if self._oauth_error is None else dict(self._oauth_error)
            self._hub.set_slice("auth", value)

    def _offered_locked(self) -> List[str]:
        return list(self._oauth_providers) if self._options.features.provider_sign_in else []

    def _record_capabilities_locked(self) -> None:
        offered = self._offered_locked()
        if self._hub is not None:
            self._hub.set_slice("capabilities", {
                "audio": self._audio,
                "oauth": bool(offered),
                "oauthProviders": offered,
                "clipboard": self._clipboard,
                "skillImport": self._skill_import,
            })

    def _record_limits_locked(self) -> None:
        refused = self._anonymous_unavailable
        if self._hub is not None:
            self._hub.set_slice("limits", {
                "demoLimitReached": self._demo_limit,
                "anonymousUnavailable": None if refused is None else {"type": refused},
            })

    def _reset_applied_locked(self, t2v: "Talk2View", forgotten: Optional[str] = None) -> None:
        """Take back a model this object applied, or ``forgotten`` (the end-user's pick that
        "Default" just cleared, or the model an older id like ``X-thinking`` stands for)
        while it is the model in use, so the partner's default applies again
        (``DEFAULT_MODEL``: field writes only, and it drops the effort too)."""
        in_use = False
        if forgotten is not None:
            legacy = legacy_model_effort(forgotten)
            in_use = t2v.model is not None and t2v.model in (
                forgotten, legacy[0] if legacy is not None else forgotten
            )
        if self._applied is not None or in_use:
            t2v.set_chat_defaults(model=DEFAULT_MODEL)
            self._applied = None

    def _record_models_locked(self) -> None:
        if self._hub is not None:
            self._hub.set_slice("models", {
                "list": list(self._models),
                "selected": self._selected,
                "selectedEffort": self._selected_effort,
            })

    def _record_audio_models_locked(self) -> None:
        if self._hub is not None:
            self._hub.set_slice(
                "audioModels",
                {"list": list(self._audio_models), "state": self._audio_models_state},
            )

    def _record_preferences_locked(self) -> None:
        wire: Dict[str, str] = {}
        if self._t2v is not None:
            try:
                prefs = self._t2v.preferences.get()
            except Exception:
                logger.warning("could not read the preferences", exc_info=True)
            else:
                for key, value in (
                    ("model", prefs.model),
                    ("reasoningEffort", prefs.reasoning_effort),
                    ("sttModel", prefs.stt_model),
                    ("sttLanguage", prefs.stt_language),
                    ("fontSize", prefs.font_size),
                ):
                    if value is not None:
                        wire[key] = value
        if self._hub is not None:
            self._hub.set_slice("preferences", wire)

    def _listed_locked(self, model_id: Optional[str]) -> bool:
        return self._entry_locked(model_id) is not None

    def _entry_locked(self, model_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """The ``models.list`` entry for ``model_id``; None when it is not listed."""
        if model_id is None:
            return None
        return next((m for m in self._models if m["id"] == model_id), None)

    def _shows_live_locked(self) -> bool:
        """Whether the auth slice shows who the token store has signed in now."""
        return bool(self._shown == _identity(self._t2v))

    # ── The refresh thread ──

    def _refresh_loop(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                if self._stopping:
                    return
                generation = self._generation
            try:
                self._refresh_once(generation)
            except Exception:
                logger.exception("refreshing the account slices failed")

    def _refresh_once(self, generation: int) -> None:
        t2v = self._t2v
        identity = _identity(t2v)  # whom these requests are for
        config: Optional[Dict[str, Any]] = None
        models: List[Dict[str, Any]] = []
        audio_models: List[Dict[str, Any]] = []
        listed = False  # the list was read: what it lacks is known to be gone
        audio_listed = False  # ditto, for audioModels.state
        signed_in_now = False  # set inside the same narrowed `if` as `t2v`'s real uses,
        # so mypy keeps narrowing `t2v` there instead of losing it behind this flag
        if t2v is not None and self._options.features.provider_sign_in and not self._probe_absent:
            self._probe_providers(t2v)
        if t2v is not None and self._signed_in(t2v):
            signed_in_now = True
            try:
                config = dataclasses.asdict(t2v.get_config())  # never starts a guest session
            except Exception as exc:
                logger.warning("could not read the partner config: %s", exc)
            # /v1/config may have met a rejected refresh token and signed out on this very
            # thread, or a sign-out landed meanwhile; signed out, list_models() would start
            # a guest session (anonymous_auto_start). Only a current, signed-in pass asks.
            with self._lock:
                current = generation == self._generation and not self._stopping
            if not (current and self._signed_in(t2v)):
                return
            try:
                models = [_model(m) for m in t2v.list_models().data]
                listed = True
            except Exception as exc:
                logger.warning("could not list the models: %s", exc)
            try:
                audio_models = [_audio_model(m) for m in t2v.voice.list_models()]
                audio_listed = True
            except Exception as exc:
                logger.warning("could not list the voice models: %s", exc)
        with self._lock:
            if self._stopping or generation != self._generation:
                return  # an auth change overtook this refresh; its own refresh follows
            if not (identity == self._shown == _identity(t2v)):
                # New tokens this object has not been told of yet (a listener ahead of
                # ours is still running): their delivery brings a refresh of its own.
                return
            self._models = models
            self._audio_models = audio_models
            if signed_in_now:
                self._audio_models_state = "ready" if audio_listed else "error"
            self._record_audio_models_locked()
            stored = _stored_pick(t2v)
            pick = resolve_pick(models, *stored)
            if self._effort_refused and listed:  # an unread list keeps it for the next pass
                self._effort_refused = False
                if pick is not None and pick.effort is not None:
                    # The engine refused this effort for this end-user: the model's
                    # default from now on, and stored so (an older id as its model), so
                    # the refusal cannot repeat on the next pass.
                    entry = next(m for m in models if m["id"] == pick.model)
                    pick = ResolvedPick(
                        pick.model, None, entry.get("default_reasoning_effort"), True
                    )
            self._selected = pick.model if pick is not None else None
            self._selected_effort = pick.shown_effort if pick is not None else None
            if t2v is not None and pick is not None:
                if (pick.model, pick.effort) != self._applied:
                    # Always the pair: a model named alone runs at its default effort.
                    t2v.set_chat_defaults(model=pick.model, reasoning_effort=pick.effort)
                    self._applied = (pick.model, pick.effort)
            elif t2v is not None and listed:
                # The list no longer has the end-user's model: one this object applied
                # goes, so the partner's default applies again. A
                # list that could not be read proves nothing, and changes nothing.
                self._reset_applied_locked(t2v)
            self._record_models_locked()
            if self._hub is not None:
                self._hub.set_slice("config", config)
        if t2v is not None and pick is not None and pick.drop_stored_effort:
            self._drop_stored_effort(t2v, stored, pick)

    @staticmethod
    def _drop_stored_effort(
        t2v: "Talk2View", stored: Tuple[Optional[str], Optional[str]], pick: ResolvedPick
    ) -> None:
        """The stored effort is no longer offered for its model, or the engine refused it:
        store the listed model alone, so the model's default applies from now on. Only
        while the preferences still hold the pick this pass resolved (a newer pick wins).
        Notifies listeners: never under ``_lock``."""
        try:
            if _stored_pick(t2v) == stored:
                t2v.preferences.update(model=pick.model, reasoning_effort=None)
        except Exception:
            logger.warning("could not forget a stored reasoning effort the model no longer "
                           "offers", exc_info=True)

    def _probe_providers(self, t2v: "Talk2View") -> None:
        """Partner-level, not the end-user's: read signed in or not (a signed-out form
        shows the buttons), and recorded whatever auth change overtakes this pass. It
        never starts a guest session (the partner key alone)."""
        try:
            offered = t2v.auth.desktop_sign_in_providers()
        except T2VError as exc:
            if exc.status_code == 404:
                # An engine older than this SDK: an answer, not a failure. The buttons
                # stay off for this AppState's life.
                logger.info("the engine does not offer desktop sign-in (no providers route)")
                with self._lock:
                    self._probe_absent = True
                return
            logger.warning("could not read the desktop sign-in providers: %s", exc)
            self._retry_probe_later()
            return
        except Exception as exc:
            logger.warning("could not read the desktop sign-in providers: %s", exc)
            self._retry_probe_later()
            return
        if isinstance(offered, list):
            with self._lock:
                self._probe_failures = 0
                if not self._stopping:
                    self._oauth_providers = [p for p in _PAGE_PROVIDERS if p in offered]
                    self._record_capabilities_locked()

    def _retry_probe_later(self) -> None:
        """A failed probe (the network not up yet at startup, a 5xx) is tried again
        without waiting for an auth change, at most ``PROBE_RETRIES`` times in a row, and
        then left until the next refresh. A 404 never comes here (``_probe_absent``)."""
        with self._lock:
            if self._stopping or self._probe_failures >= PROBE_RETRIES:
                return
            self._probe_failures += 1
            timer = threading.Timer(PROBE_RETRY_S, self.refresh)
            timer.daemon = True
            self._probe_timer = timer
            timer.start()

    @staticmethod
    def _signed_in(t2v: "Talk2View") -> bool:
        try:
            return t2v.auth.is_authenticated()
        except Exception:
            return False
