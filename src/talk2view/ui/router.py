"""CommandRouter: the one thread that turns the page's commands into calls.

HTTP threads only validate and queue (``wire.parse_command``). This worker calls the chat
controller, ``Talk2View`` and the host, in the order the page sent the commands, so no HTTP
thread ever calls the controller, blocks on its locks, or runs host code. It holds no lock
while it calls out: ``_count_lock`` (the router's state and queued count) and
``UploadRegistry._lock`` are leaves. A command never raises out of the worker: it ends in one
result, which ``AppState`` records in the ``results`` slice. The worker also runs the
server's internal jobs (the hub's rebase), which is why a rebase can never run inside a
controller delivery.

Host code. ``copy`` and ``openLink`` are posted to the host's main thread and not waited on:
their result means "handed to the host". ``on_skills_list``, the host's rescan of its skill
folders, runs synchronously on this thread, as every ``Talk2View`` call does, and the
commands behind it wait for it. ``oauthStart`` starts a sign-in on its own thread
(``ProviderSignIn``) and does not wait for it: it waits for the browser for up to five
minutes, and ``oauthCancel`` must not queue behind it. ``login``, ``signup``, ``logout``
and ``startAnonymous`` cancel a waiting one first, so a browser that comes back late
cannot replace the account the end-user just chose. Every cancel runs here with no lock
held (``ProviderSignIn``).

Lifecycle. A router is new, then running, then stopped: ``start()`` works once, and
``stop()`` may be called any number of times (before ``start()`` it only makes the router
refuse everything). Commands run strictly in order, so ``stop()`` waits, up to its timeout,
behind a slow command, and commands queued before it still run, possibly after it returns.
A ``BaseException`` out of a command or a job (``SystemExit`` or ``KeyboardInterrupt``
raised by host code) fails that command with ``internal_error``, stops the router, and ends
every command still queued with ``closed``; nothing is re-raised on the worker. From then on
``submit`` refuses with ``STOPPED``, never ``QUEUE_FULL``: trying again cannot help.

One known gap, left open on purpose: a ``submit()`` that passed its check just as ``stop()``
ran can queue its command behind the worker's stop marker, and that command never runs or
records a result. It can only happen while the server shuts down, when no page is left to
read a result; closing it would take the queue's lock under ``_count_lock``, which must stay
a leaf.
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)

from ..controller import ControllerBusyError, ControllerClosedError
from ..errors import AnonymousUnavailableError, AuthenticationError, T2VError
from ..metrics import TurnMetrics
from ..skill_store import InvalidSkillError, SkillExistsError
from ..skills import SkillValidationError
from ..types import Attachment, Decision
from .app_state import AppState, UnknownModelError, _identity
from .dictation import Dictation
from .host import HostAdapter
from .hub import ChatSource
from .options import _openable
from .provider_sign_in import ProviderSignIn
from .threads import ThreadsState
from .wire import Command, CommandFailure, _echo, failure_result, success_result

if TYPE_CHECKING:
    from .. import Talk2View
    from ..skill_format import Problem
    from ..skills import T2VSkills

__all__ = ["CommandRouter", "ControllerLike", "UploadRegistry"]

logger = logging.getLogger("talk2view.ui")

MAX_QUEUED_COMMANDS = 256
#: Why ``submit`` refused a command.
QUEUE_FULL = "queue_full"
STOPPED = "stopped"
_STOP = object()
Job = Callable[[], None]
#: How long ``skillImport`` waits for the host's file picker (``_on_host``): a native modal
#: dialog is legitimately left open for minutes while someone finds the file.
PICKER_WAIT_S = 600
#: How long a ``_pending_import`` (a ``skill_exists`` conflict, or a file missing only its
#: description) stays answerable: past this, ``resolve``/``description`` gets
#: ``import_unavailable`` and the page must import again.
PENDING_IMPORT_S = 300

_NEW, _RUNNING, _STOPPED = "new", "running", "stopped"
_INTERNAL_ERROR = "Something went wrong."
_ATTACHMENTS_OFF = "Attachments are turned off in this app."
_GUESTS_OFF = "Guest chat is turned off in this app. Sign in to continue."
_PROVIDERS_OFF = "Sign-in with Apple or Google is turned off in this app."
_THREADS_OFF = "The chat list is turned off in this app."
_PROVIDER_NAMES = {"apple": "Apple", "google": "Google"}


class ControllerLike(ChatSource, Protocol):
    """The part of ``ChatController`` the UI server uses (``FakeController`` has it too)."""

    def send(self, text: str, attachments: Optional[List[Attachment]] = None) -> None: ...

    def cancel(self) -> None: ...

    def approve(self, decision: Decision, *, tool_call_id: Optional[str] = None) -> None: ...

    def retry(self) -> None: ...

    def edit(self, message_id: str, text: str) -> None: ...

    def clear(self) -> None: ...

    def clear_error(self) -> None: ...


def _auth_refusal(err: T2VError) -> bool:
    """A refusal only a sign-in fixes: a 401, or the SDK's own ``sign_in_required``."""
    return (
        isinstance(err, AuthenticationError)
        or err.status_code == 401
        or err.type in ("sign_in_required", "authentication_error")
    )


def _only_description(message: str) -> bool:
    """Whether an ``invalid_skill`` message (``_problems_message``) names only the
    description: the one problem an import can ask the end-user to fix in place."""
    lines = [line for line in message.split("\n") if line]
    return bool(lines) and all(line.startswith("description: ") for line in lines)


def _problems_message(problems: Sequence["Problem"]) -> str:
    """``invalid_skill``'s message: one ``"<field>: <message>"`` line per problem, joined
    with ``"\\n"`` (``_skill_save``'s locally-validated problems and
    ``skill_store.InvalidSkillError.problems`` both use this)."""
    return "\n".join("{}: {}".format(p.field, p.message) for p in problems)


def _can_own(identity: Any) -> bool:
    """Whether an ``app_state._identity`` may own uploads: only a signed-in one with a user
    id. Signed out, unreadable (``_UNREADABLE``), and signed in with no readable user
    (``(True, None)``: a stored token whose user is missing or garbled, which two different
    identities could share) own nothing."""
    return (
        isinstance(identity, tuple)
        and len(identity) == 2
        and identity[0] is True
        and identity[1] is not None
        and identity[1] != ""
    )


class UploadRegistry:
    """Attachments uploaded through ``POST /upload``, by id, so ``send`` can name them (the
    page only knows ids). Each is filed under its owner, the ``app_state._identity`` that
    uploaded it, and resolves for that owner only: the engine refuses one identity's
    attachment to another anyway, and this refuses it first. The oldest are forgotten past
    ``capacity``. Only ``Attachment`` objects get in: ``send`` hands them to the controller,
    which starts its run before it reads their fields, so anything else could leave it busy
    for good."""

    def __init__(self, capacity: int = 64) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, Tuple[Attachment, Any]]" = OrderedDict()

    def add(self, attachment: Attachment, *, owner: Any) -> None:
        if not isinstance(attachment, Attachment):
            raise TypeError(
                "UploadRegistry holds Attachment objects, not {}".format(
                    type(attachment).__name__
                )
            )
        with self._lock:
            self._items[attachment.id] = (attachment, owner)
            self._items.move_to_end(attachment.id)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def resolve(self, ids: Sequence[str], *, owner: Any) -> List[Attachment]:
        """``owner``'s attachments, in the order named. An id not filed under ``owner``
        (never uploaded here, expired, or another identity's) is refused exactly as a
        missing one is, so the page learns nothing of anyone else's; an ``owner`` that
        cannot own uploads (``_can_own``) resolves nothing."""
        with self._lock:
            found: List[Attachment] = []
            for attachment_id in ids:
                entry = self._items.get(attachment_id)
                if entry is None or not _can_own(owner) or entry[1] != owner:
                    raise CommandFailure(
                        "unknown_attachment",
                        "Attachment {} was not uploaded here (or has expired).".format(
                            _echo(attachment_id)
                        ),
                    )
                found.append(entry[0])
            return found


class CommandRouter:
    """Runs every command, and the server's internal jobs, on one ``t2v-ui-router`` thread.

    ``on_skills_list`` is the host's rescan of its skill folders: ``skillsList`` calls it
    synchronously on the router thread, before it reads the catalogue, and an exception
    from it fails that command (``internal_error``).
    """

    def __init__(
        self,
        controller: ControllerLike,
        app: AppState,
        host: HostAdapter,
        uploads: UploadRegistry,
        *,
        max_queued: int = MAX_QUEUED_COMMANDS,
        on_skills_list: Optional[Callable[[], None]] = None,
        threads: Optional[ThreadsState] = None,
        dictation: Optional[Dictation] = None,
        on_pick_skill_import: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self._controller = controller
        self._app = app
        self._host = host
        self._uploads = uploads
        self._max_queued = max_queued
        self._on_skills_list = on_skills_list
        self._threads = threads
        self._dictation = dictation
        self._pick_import = on_pick_skill_import
        # (path, time.monotonic() it was set, the description already given or None) for an
        # import waiting on the page: a `skill_exists` conflict waiting on `resolve`, or a
        # file with no description waiting on `description`; expires after
        # PENDING_IMPORT_S. Router-wide, not per page/tab: this is a single-user desktop
        # panel, so one waiting import at a time is accepted.
        self._pending_import: Optional[Tuple[str, float, Optional[str]]] = None
        self._queue: "queue.Queue[Union[Command, Job, object]]" = queue.Queue()
        self._count_lock = threading.Lock()
        self._queued = 0
        self._state = _NEW
        self._thread: Optional[threading.Thread] = None
        self._provider_sign_in = ProviderSignIn(app, host)
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = {
            "send": self._send,
            "cancel": lambda args: self._controller.cancel(),
            "approve": self._approve,
            "reload": lambda args: self._controller.retry(),
            "edit": self._edit,
            "newSession": lambda args: self._controller.clear(),
            "clearError": lambda args: self._controller.clear_error(),
            "threadNew": lambda args: self._threads_on().new_thread(),
            "threadSwitch": lambda args: self._thread_call(
                lambda t: t.switch(args["threadId"])
            ),
            "threadRename": lambda args: self._thread_call(
                lambda t: t.rename(args["threadId"], args["title"])
            ),
            "threadDelete": lambda args: self._thread_call(
                lambda t: t.delete(args["threadId"])
            ),
            "selectModel": self._select_model,
            "login": self._login,
            "signup": self._signup,
            "logout": self._logout,
            "startAnonymous": self._start_anonymous,
            "oauthStart": self._oauth_start,
            "oauthCancel": lambda args: self._provider_sign_in.cancel(),
            "oauthDismissError": lambda args: self._app.dismiss_oauth_error(),
            "warmUp": lambda args: self._account().warm_up(),
            "setFontSize": self._set_font_size,
            "selectSttModel": lambda args: self._app.select_stt_model(args["modelId"]),
            "selectSttLanguage": lambda args: self._app.select_stt_language(args["language"]),
            "copy": self._copy,
            "openLink": self._open_link,
            "dictationStart": lambda args: self._dictation_on().start(),
            "dictationStop": lambda args: self._dictation_on().stop(),
            "dictationCancel": lambda args: self._dictation_on().cancel(),
            "skillsList": self._skills_list,
            "skillEnable": lambda args: self._set_skill(args["name"], True),
            "skillDisable": lambda args: self._set_skill(args["name"], False),
            "skillSave": self._skill_save,
            "skillDelete": lambda args: self._skill_store_call(
                lambda s: s.delete_user(args["name"])
            ),
            "skillRestore": lambda args: self._skill_store_call(
                lambda s: s.restore_user(args["name"])
            ),
            "skillCustomize": lambda args: {
                "name": self._skill_store_call(lambda s: s.customize(args["name"]))
            },
            "skillImport": self._skill_import,
            "skillOpenFolder": self._skill_open_folder,
            "skillRead": self._skill_read,
            "metrics": self._metrics,
        }
        #: When the command running now was received (``Command.received``).
        self._received: Optional[float] = None

    # ── Lifecycle ──

    def start(self) -> None:
        """Start the worker. Once only: a second call, or one after ``stop()``, raises
        ``RuntimeError``."""
        thread = threading.Thread(target=self._loop, name="t2v-ui-router", daemon=True)
        with self._count_lock:
            if self._state != _NEW:
                raise RuntimeError("a CommandRouter starts once, and never after stop()")
            self._state = _RUNNING
            self._thread = thread
        thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Refuse commands from now on, and end the worker once it has run what is already
        queued, waiting for it at most ``timeout`` seconds, then cancel a provider sign-in
        that waits in the browser (waiting for it at most ``timeout`` seconds too).
        Idempotent; before ``start()`` it marks the router stopped and cancels that sign-in
        (``execute`` may have started one)."""
        with self._count_lock:
            running = self._state == _RUNNING
            self._state = _STOPPED
            thread = self._thread
        if running:
            self._queue.put(_STOP)
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout)
        self._provider_sign_in.close(timeout)

    def submit(self, command: Command) -> Optional[str]:
        """Queue a validated command. Returns None once it is queued, or why it was not:
        ``QUEUE_FULL`` (worth trying again) or ``STOPPED``, the router is not running
        (not started yet, stopped, or halted by a ``BaseException``) and never will be."""
        with self._count_lock:
            if self._state != _RUNNING:
                return STOPPED
            if self._queued >= self._max_queued:
                return QUEUE_FULL
            self._queued += 1
        self._queue.put(command)
        return None

    def submit_job(self, job: Job) -> None:
        """Queue internal work. Never refused and never blocks; a job queued once the
        router has stopped never runs."""
        self._queue.put(job)

    # ── The worker ──

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            if isinstance(item, Command):
                with self._count_lock:
                    self._queued -= 1
                try:
                    result = self.execute(item)
                except BaseException as exc:  # SystemExit or KeyboardInterrupt out of host code
                    self._record(failure_result(item, "internal_error", _INTERNAL_ERROR))
                    self._halt("command {}".format(item.type), exc)
                    return
                self._record(result)
            elif callable(item):
                try:
                    item()
                except Exception:
                    logger.exception("a UI server job failed")
                except BaseException as exc:
                    self._halt("a UI server job", exc)
                    return

    def _record(self, result: Dict[str, Any]) -> None:
        try:
            self._app.record_result(result)
        except Exception:
            logger.exception("could not record a command result")

    def _halt(self, what: str, exc: BaseException) -> None:
        """After a ``BaseException`` on the worker: log it, stop accepting, and end every
        command still queued with ``closed``. The worker returns next."""
        logger.error(
            "%s raised %s; the UI server's command router has stopped",
            what, type(exc).__name__, exc_info=exc,
        )
        with self._count_lock:
            self._state = _STOPPED
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, Command):
                with self._count_lock:
                    self._queued -= 1
                self._record(failure_result(item, "closed", "This chat has stopped."))

    def execute(self, command: Command) -> Dict[str, Any]:
        """Run one command on the calling thread and return its result (the worker's step;
        tests call it directly). Never raises an ``Exception``; a ``BaseException`` goes
        through, and the worker then fails the command and stops."""
        self._received = command.received
        try:
            data = self._handlers[command.type](command.args)
            return success_result(command, data if isinstance(data, dict) else None)
        except CommandFailure as failure:
            return failure_result(command, failure.type, failure.message)
        except ControllerBusyError as exc:
            return failure_result(command, "busy", str(exc))
        except ControllerClosedError as exc:
            return failure_result(command, "closed", str(exc))
        except UnknownModelError as exc:
            message = "Model {} is not available.".format(_echo(str(exc)))
            return failure_result(command, "unknown_model", message)
        except T2VError as exc:
            return failure_result(command, exc.type or "engine_error", str(exc))
        except Exception:
            logger.exception("command %s failed", command.type)
            return failure_result(command, "internal_error", _INTERNAL_ERROR)

    # ── Handlers (worker thread; no lock held) ──

    def _account(self) -> "Talk2View":
        t2v = self._app.t2v
        if t2v is None:
            raise CommandFailure(
                "account_unavailable", "This chat is not connected to a Talk2View account."
            )
        return t2v

    def _threads_on(self) -> ThreadsState:
        if self._threads is None:
            raise CommandFailure("thread_list_disabled", _THREADS_OFF)
        return self._threads

    def _thread_call(self, call: Callable[[ThreadsState], None]) -> None:
        try:
            call(self._threads_on())
        except KeyError as exc:
            raise CommandFailure(
                "unknown_thread", "No chat with id {}.".format(_echo(str(exc.args[0])))
            ) from None

    def _send(self, args: Dict[str, Any]) -> None:
        if args["attachmentIds"] and not self._app.options.features.attachments:
            raise CommandFailure("attachments_disabled", _ATTACHMENTS_OFF)
        # Resolved, or refused, before the controller is called: it only ever gets this
        # server's own Attachment objects, uploaded by whoever is signed in now, each once,
        # in the order the page named them.
        attachments = self._uploads.resolve(
            list(dict.fromkeys(args["attachmentIds"])), owner=_identity(self._app.t2v)
        )
        metrics = getattr(self._controller, "metrics", None)
        if not isinstance(metrics, TurnMetrics):
            self._controller.send(args["text"], attachments or None)
            return
        # The turn this starts is timed from the command's arrival; with `timing`, its
        # timing then waits (briefly) for the page's own `metrics` report.
        with metrics.page_send(received=self._received, reports=args["timing"]):
            self._controller.send(args["text"], attachments or None)

    def _metrics(self, args: Dict[str, Any]) -> None:
        metrics = getattr(self._controller, "metrics", None)
        if isinstance(metrics, TurnMetrics):
            metrics.attach_page(
                args["messageId"],
                send_to_post_ms=args["sendToPostMs"],
                first_op_to_paint_ms=args["firstOpToPaintMs"],
                first_op_at_ms=args["firstOpAtMs"],
            )

    def _approve(self, args: Dict[str, Any]) -> None:
        decision = Decision(
            action=args["action"], feedback=args["feedback"], updated_input=args["updatedInput"]
        )
        self._controller.approve(decision, tool_call_id=args["toolCallId"])

    def _select_model(self, args: Dict[str, Any]) -> None:
        if args["modelId"] is None:  # "Default": the partner's model
            self._account()
            self._app.select_default_model()
        else:
            self._app.select_model(args["modelId"])

    def _edit(self, args: Dict[str, Any]) -> None:
        try:
            self._controller.edit(args["messageId"], args["text"])
        except ValueError as exc:
            raise CommandFailure("not_found", str(exc)) from None

    # Credentials arrive as wire.Secret and are revealed inline, as the argument of the auth
    # call, so no local of these frames ever holds one in plain text.

    def _login(self, args: Dict[str, Any]) -> None:
        self._provider_sign_in.cancel()  # the end-user chose this account instead
        self._account().auth.login(args["email"], args["password"].reveal())

    def _signup(self, args: Dict[str, Any]) -> Dict[str, Any]:
        self._provider_sign_in.cancel()
        outcome = self._account().auth.signup(args["email"], args["password"].reveal())
        return {"confirmationRequired": outcome.confirmation_required}

    def _logout(self, args: Dict[str, Any]) -> None:
        self._provider_sign_in.cancel()
        self._account().auth.logout()

    def _start_anonymous(self, args: Dict[str, Any]) -> None:
        if not self._app.options.allow_anonymous:
            raise CommandFailure("anonymous_not_allowed", _GUESTS_OFF)
        token = args["captchaToken"]  # a Secret, or None
        self._provider_sign_in.cancel()  # the end-user chose to go on as a guest
        t2v = self._account()
        try:
            t2v.auth.start_anonymous(
                captcha_token=token.reveal() if token is not None else None, warm=True
            )
        except AnonymousUnavailableError as exc:
            # Only signing in can help: the page's gate opens with the reason.
            self._app.note_anonymous_unavailable(exc.type)
            raise
        # The page starts a guest for a send (or an upload) already on its way, and no
        # keystroke warmed this new identity's key: warm it now, on a thread of its own,
        # while the turn sets up its session, instead of on the turn's first request.
        t2v.warm_up()

    def _set_font_size(self, args: Dict[str, Any]) -> None:
        self._account().preferences.update(font_size=args["fontSize"])

    def _oauth_start(self, args: Dict[str, Any]) -> None:
        # Starts the sign-in on its own thread and returns: the result means "started".
        # What follows is auth.oauthPending, then the account or auth.oauthError.
        if not self._app.options.features.provider_sign_in:
            raise CommandFailure("oauth_disabled", _PROVIDERS_OFF)
        t2v = self._account()
        provider = args["provider"]
        if provider not in self._app.oauth_providers():
            raise CommandFailure(
                "oauth_unavailable",
                "Sign-in with {} is not available in this app.".format(_PROVIDER_NAMES[provider]),
            )
        if not self._provider_sign_in.start(t2v, provider):
            raise CommandFailure("busy", "A sign-in is already waiting in your browser.")

    def _copy(self, args: Dict[str, Any]) -> None:
        if getattr(self._host, "supports_clipboard", False) is not True:
            raise CommandFailure("clipboard_unavailable", "This app cannot copy to the clipboard.")
        text = args["text"]
        host = self._host
        host.post(lambda: host.clipboard_set(text))

    def _open_link(self, args: Dict[str, Any]) -> None:
        url = args["url"]
        if not _openable(url):
            raise CommandFailure("invalid_url", "Only http and https links can be opened.")
        host = self._host
        host.post(lambda: host.open_external(url))

    def _dictation_on(self) -> Dictation:
        if self._dictation is None:
            raise CommandFailure("dictation_unavailable", "Dictation is not available in this app.")
        return self._dictation

    def _skills_list(self, args: Dict[str, Any]) -> None:
        """Rescan the host's skill folders, then read the app's catalogue. Guests too: the
        folders are local, so a skill dropped in while signed out is listed. The catalogue
        needs a session; with none, or when the engine refuses for want of one, there is
        simply no catalogue this time -- an ok result, never a "Session expired". With no
        Talk2View at all there are no skills to list: ok, and nothing to say (the page asks
        on every visit, guests included)."""
        t2v = self._app.t2v
        if t2v is None:
            return
        if self._on_skills_list is not None:
            self._on_skills_list()  # the host rescans its skill folders, on this thread
        if not t2v.auth.is_authenticated():
            return
        try:
            t2v.skills.list_platform()
        except T2VError as err:
            if not _auth_refusal(err):
                raise

    def _set_skill(self, name: str, enabled: bool) -> None:
        try:
            self._account().skills.set_enabled(name, enabled)
        except KeyError:
            raise CommandFailure("unknown_skill", "No skill named {!r}.".format(name)) from None
        except SkillValidationError as exc:
            raise CommandFailure("skill_too_large", str(exc)) from None

    # ── Skills: the page's own store (talk2view.skill_store) ──

    def _skills_writable(self) -> "T2VSkills":
        skills = self._account().skills
        store = skills.store
        if store is None or not store.writable:
            raise CommandFailure("skills_unavailable", "This app keeps no skills of its own.")
        return skills

    def _skill_store_call(self, fn: Callable[["T2VSkills"], Any]) -> Any:
        skills = self._skills_writable()
        try:
            return fn(skills)
        except InvalidSkillError as exc:
            raise CommandFailure("invalid_skill", _problems_message(exc.problems)) from None
        except SkillExistsError as exc:
            raise CommandFailure("skill_exists", exc.name) from None
        except KeyError as exc:
            raise CommandFailure(
                "unknown_skill", "No skill named {!r}.".format(exc.args[0])
            ) from None
        except OSError as exc:
            raise CommandFailure(
                "io_error", "Could not change the skill: {}".format(exc.strerror or exc)
            ) from None

    def _skill_save(self, args: Dict[str, Any]) -> Dict[str, Any]:
        from ..skill_format import from_form, parse, validate

        if "markdown" in args and args["markdown"] is not None:
            doc, problems = parse(args["markdown"])
        else:
            name, description = args.get("name") or "", args.get("description") or ""
            doc, problems = from_form(name, description, args.get("instructions") or ""), []
        problems = problems + validate(doc)
        if problems:
            raise CommandFailure("invalid_skill", _problems_message(problems))
        name = self._skill_store_call(
            lambda s: s.save_doc(
                doc, replaces=args.get("replaces"), overwrite=bool(args.get("overwrite"))
            )
        )
        return {"name": name}

    def _skill_read(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """A listed user or bundled skill's ``SKILL.md``, verbatim (``T2VSkills.read_markdown``:
        looked up by name only; the page never names a path)."""
        try:
            text = self._skill_store_call(lambda s: s.read_markdown(args["name"]))
        except CommandFailure as failure:
            if failure.type == "io_error":
                raise CommandFailure("io_error", "Could not read the skill.") from None
            raise
        return {"markdown": text}

    def _skill_import(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Pick a file (a fresh import), or answer the import waiting on the page: ``resolve``
        a name clash, and/or give the ``description`` a picked file lacked. The
        page never names a path; an answer always applies to the path this router picked.
        A clash, or a failure whose only problem is the description, keeps that path
        waiting (with any description given so far) for the next answer."""
        self._skills_writable()
        resolve = args.get("resolve")
        answered = args.get("description")
        description: Optional[str] = None
        if resolve is not None or answered is not None:
            pending = self._take_pending_import()
            if pending is None:
                raise CommandFailure(
                    "import_unavailable", "There is no import waiting for an answer."
                )
            path, description = pending
            if answered is not None:
                description = answered
            on_conflict = {"replace": "replace", "keepBoth": "keep_both"}.get(resolve or "", "ask")
        else:
            # A fresh pick supersedes any earlier import still waiting on an answer.
            self._pending_import = None
            if self._pick_import is None:
                raise CommandFailure("import_unavailable", "This app cannot pick files to import.")
            path = self._on_host(self._pick_import)
            if path is None:
                return {"cancelled": True}
            on_conflict = "ask"
        try:
            name = self._skill_store_call(
                lambda s: s.import_path(path, on_conflict, description=description)
            )
        except CommandFailure as failure:
            if failure.type == "skill_exists" or (
                failure.type == "invalid_skill" and _only_description(failure.message)
            ):
                self._pending_import = (path, time.monotonic(), description)
            raise
        return {"name": name}

    def _take_pending_import(self) -> Optional[Tuple[str, Optional[str]]]:
        """The path (and description given so far) of a still-live waiting import,
        consuming it (a second answer finds nothing pending), or None when there is none
        or it has expired (``PENDING_IMPORT_S``)."""
        pending, self._pending_import = self._pending_import, None
        if pending is None:
            return None
        path, taken_at, description = pending
        if time.monotonic() - taken_at > PENDING_IMPORT_S:
            return None
        return path, description

    def _on_host(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` on the host's main thread and wait for it (a modal file dialog), for
        at most ``PICKER_WAIT_S``: a host that never runs the posted callable must not hang
        this router's one worker thread, and every command queued behind it."""
        done: "concurrent.futures.Future[Any]" = concurrent.futures.Future()

        def call() -> None:
            try:
                done.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - handed back to the router thread
                done.set_exception(exc)

        self._host.post(call)
        try:
            return done.result(timeout=PICKER_WAIT_S)
        except concurrent.futures.TimeoutError:
            raise CommandFailure(
                "import_unavailable", "The file picker did not answer."
            ) from None

    def _skill_open_folder(self, args: Dict[str, Any]) -> None:
        skills = self._skills_writable()
        folder = skills.store.folder if skills.store is not None else None
        if not folder:
            raise CommandFailure("skills_unavailable", "This app keeps no skills folder.")
        Path(folder).mkdir(parents=True, exist_ok=True)
        url = Path(folder).resolve().as_uri()
        host = self._host
        host.post(lambda: host.open_external(url))
