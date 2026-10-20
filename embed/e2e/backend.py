"""The real UI server behind the page's end-to-end specs (e2e/fixtures.ts starts one per test).

    uv run --project .. python e2e/backend.py --static-dir DIR [--mode fake|account]
        [--chunk-delay S] [--dark] [--no-attachments] [--no-anonymous] [--no-clipboard]
        [--no-account] [--no-settings] [--anonymous-refusal TYPE] [--destructive-tool [WARNING]]
        [--huge-skill] [--skills-disabled] [--slow-skills SECONDS] [--slow-models SECONDS]
        [--needs-session-reset] [--late-login-result SECONDS] [--register-signed-out]
        [--signup-auto-confirm] [--drop-model-on-relogin] [--hostile]
        [--desktop-sign-in] [--no-provider-sign-in] [--oauth-browser auto|hold|deny]
        [--slow-cancel SECONDS]

``--mode fake`` (the default) is ``UIServer`` + ``talk2view.ui.testing.FakeController``: the
scripted chat the runtime guard also uses (``/approve``, ``/fail``, ``/limit``), and no account.
``--mode account`` is ``UIServer`` + a real ``Talk2View`` + ``ChatController`` against the
Python SDK's own fake engine (``tests/ui_engine.py``), with sign-up, a
wrong password, an engine approval, a skill added here, a small platform catalogue on ``GET
/v1/skills`` (or, with ``--skills-disabled``, a ``skills_disabled`` refusal on that route
and on ``POST /v1/skills/register``, as a real engine refuses both), and an oversized skill
with ``--huge-skill``. Both modes take the same ``ChatOptions``.

``--desktop-sign-in`` (account mode) has the fake engine offer Apple and Google desktop
sign-in, so the page shows "Continue with Apple / Google"; ``--no-provider-sign-in`` turns
``features.provider_sign_in`` off in the options. The end-user's browser is this process:
``--oauth-browser auto`` (the default) follows the start URL at once and signs in, ``deny``
follows it to an ``access_denied`` refusal, and ``hold`` opens the engine's check screen
and stays there until the ``oauth-finish`` control presses Continue (the sign-in then
completes). ``--slow-cancel`` holds a cancelled sign-in's thread open for a while, as a
slow one would: ``auth.oauthPending`` stays set and the server refuses a new start as
``busy`` until it ends.

It prints one JSON line, ``{"url": ...}``, once the server is up, then one line per host call
(``{"host": "open_external" | "clipboard_set" | "skills_rescan", "value": ...}``; a desktop
sign-in's start URL holds a one-time handle, so its value is ``"<desktop sign-in>"``, never
the URL) and, in fake mode, one per controller call (``{"controller": "send" | "retry" |
"cancel" | "clear"}``), and exits when stdin closes. A line on stdin is a control
(``controls`` in ``main``): ``attachments-off`` turns ``features.attachments`` off in the
options slice mid-session; ``oauth-finish`` presses Continue on a held desktop
sign-in's check screen (``--oauth-browser hold``); ``switch-off`` has the engine stop
offering desktop sign-in (a Continue after it hands ``desktop_sign_in_disabled`` to the
app's loopback, as the engine's /confirm does); ``pending-unknown`` puts a pending
sign-in for a provider the page does not know into the auth slice.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlsplit

SDK_PYTHON = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SDK_PYTHON))  # for tests.ui_engine, the SDK's own fake engine

from talk2view import ChatController, Talk2View, tool  # noqa: E402
from talk2view.controller import Capabilities  # noqa: E402
from talk2view.skills import Skill  # noqa: E402
from talk2view.types import Attachment  # noqa: E402
from talk2view.ui import GenericHost, Theme, UIServer  # noqa: E402
from talk2view.ui.options import (  # noqa: E402
    ChatFeatures,
    ChatOptions,
    ChatWelcome,
    ToolPresentation,
)
from talk2view.ui.testing import DEFAULT_REPLY, FakeController  # noqa: E402

#: FakeController's reply plus a blockquote and a link: every Markdown element the page's
#: specs and the runtime guard check. guard/slicer_guard_run.py serves the same reply.
REPLY = DEFAULT_REPLY + (
    "\n> Check the window/level preset before you threshold.\n\n"
    "More in [the Slicer docs](https://slicer.readthedocs.io/en/latest/).\n"
)


#: The one hostile string: a right-to-left override inside a file name, so "exe" reads
#: as "png" when drawn raw. Built with chr(), never a literal character or a source escape
#: (the pre-commit Cf/Cc scan). --hostile puts it in every untrusted plain-text surface.
HOSTILE = "report" + chr(0x202E) + "gnp.exe"
#: The host's own copy under --hostile: Persian "mi-khanam" (I read), whose ZWNJ (U+200C)
#: the host's text needs and the page must show as given, never escaped.
HOST_WORD = "".join(
    chr(c) for c in (0x0645, 0x06CC, 0x200C, 0x062E, 0x0648, 0x0627, 0x0646, 0x0645)
)


def emit(record: Any) -> None:
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _nest(depth: int) -> Dict[str, Any]:
    """A JSON value `depth` levels deep: the repro for an approval's own
    arguments, un-bounded, push the card's title and buttons off screen)."""
    obj: Dict[str, Any] = {"leaf": True}
    for _ in range(depth):
        obj = {"nested": obj}
    return obj


#: A few detail rows, the same shape an engine approval's own `details` take: the
#: 320×480 spec needs a card that is realistically full (title, details, arguments, the
#: "Always allow" hint), not just one oversized field in isolation, to tell a viewport-
#: relative bound apart from a fixed one that happens to still fit a single-field card.
_DETAILS = [
    {"label": "Volume", "value": "MRHead"},
    {"label": "Modality", "value": "MR"},
    {"label": "Series", "value": "T1-weighted"},
]


#: Where Continue goes on the check screen ``--oauth-browser hold`` shows, kept for
#: ``oauth-finish`` (one item).
HELD_SIGN_IN: List[str] = []
DESKTOP_GO = "/v1/auth/oauth/desktop/go/"


def follow_later(url: str) -> None:
    """The fake browser, on its own thread: the listener waits for it on the SDK's."""
    from tests.fake_engine import follow_in_browser

    threading.Thread(target=follow_in_browser, args=(url,), daemon=True).start()


class RecordingHost(GenericHost):
    """A GenericHost whose browser and clipboard are this process's stdout."""

    def __init__(self, *, theme: Theme, clipboard: bool, oauth_browser: str = "auto") -> None:
        super().__init__(theme=theme, open_url=lambda url: None)
        self.supports_clipboard = clipboard
        self.oauth_browser = oauth_browser

    def open_external(self, url: str) -> None:
        if urlsplit(url).path.startswith(DESKTOP_GO):
            if self.oauth_browser == "hold":
                from tests.fake_engine import open_check_screen

                # Before the open is reported: a control sent once the test sees the
                # report (switch-off) finds the end-user already on the check screen.
                HELD_SIGN_IN[:] = [open_check_screen(url)]
            # Never the URL itself: it holds a one-time handle.
            emit({"host": "open_external", "value": "<desktop sign-in>"})
            if self.oauth_browser != "hold":
                follow_later(url)
            return
        emit({"host": "open_external", "value": url})

    def clipboard_set(self, text: str) -> None:
        emit({"host": "clipboard_set", "value": text})


class RecordingController(FakeController):
    """A FakeController that says on stdout which calls the page's commands reached it.

    The thread looks the same whether or not a click did anything — "Try again" on a turn
    that always fails leaves the same card, and a second opener click that the controller
    refused as busy leaves the same message — so the specs read the calls instead. Each is
    emitted BEFORE the call, so one the controller then refuses is recorded too.
    """

    def send(self, text: str, attachments: Optional[List[Attachment]] = None) -> None:
        emit({"controller": "send"})
        super().send(text, attachments)

    def retry(self) -> None:
        emit({"controller": "retry"})
        super().retry()

    def cancel(self) -> None:
        emit({"controller": "cancel"})
        super().cancel()

    def clear(self) -> None:
        emit({"controller": "clear"})
        super().clear()

    def _stream_turn(self, run: int, index: int, text: str) -> None:
        # Two scripted approvals beyond testing.py's own "/approve", both proving the card
        # fails closed rather than trust a wire value it cannot faithfully show: testing.py
        # itself (read-only) always sends a well-formed {"name": "MRHead"}.
        if text.startswith("/hostdestructive"):
            # A destructive tool whose host presentation has an activity and no warning of
            # its own: the page's warning names it by that activity, as given (M-E).
            self._approve_with(run, index, {"name": "MRHead"}, tool_name="reset_scene")
            return
        if text.startswith("/hostileapprove"):
            # An approval whose tool NAME is hostile: the card's title shows it.
            self._approve_with(run, index, {"name": "MRHead"}, tool_name=HOSTILE)
            return
        if text.startswith("/hostile"):
            self._hostile_turn(run, index)
            return
        if text.startswith("/badargs"):
            self._approve_with(run, index, "rm -rf /")  # not an object: canShowArguments() false
            return
        if text.startswith("/deepargs"):
            self._approve_with(run, index, _nest(400), details=_DETAILS)  # the deep-arguments repro
            return
        if text.startswith("/spoofargs"):
            # "Edit arguments" once seeded the textarea with the RAW value,
            # putting the spoof straight back in front of the end-user. chr(), never a
            # literal character or a source escape, same reasoning as backend.py's other
            # hostile triggers.
            rlo = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE
            # A display string the tool never opens, not a real temp path.
            spoofed_path = "/tmp/report" + rlo + "gnp.exe"  # noqa: S108
            self._approve_with(run, index, {"path": spoofed_path})
            return
        if text.startswith("/bigargs"):
            # An integer too large for a JavaScript number. json.dumps writes every
            # digit; the page's JSON.parse rounds it, so the card can only show it rounded.
            self._approve_with(run, index, {"name": "MRHead", "volume_id": 12345678901234567891})
            return
        if text.startswith("/hugearg"):
            # The oversized-arguments repro, the OTHER shape: not deep, just long — a
            # single 1 MB string value, same as a real tool could legitimately be asked to
            # review a large document or a long piece of generated text.
            self._approve_with(run, index, {"text": "x" * (1024 * 1024)}, details=_DETAILS)
            return
        if text.startswith("/longname"):
            # The oversized-arguments repro, a third shape: the TOOL NAME itself, not the
            # arguments, is what is unbounded (a 300-character tool name is off-contract but not
            # impossible from a badly-behaved MCP server).
            self._approve_with(
                run, index, {"name": "MRHead"}, tool_name="x" * 300, details=_DETAILS
            )
            return
        super()._stream_turn(run, index, text)

    def _hostile_turn(self, run: int, index: int) -> None:
        """HOSTILE through every untrusted plain-text surface a turn has, a stage at a
        time so a spec can look at each: the agent's status line (an empty reply), then a
        running tool (whose activity is the host's own copy, shown as given), then a finished
        tool's name, arguments, result and image file name, an image that failed and why, and
        the error card."""
        stages: List[Dict[str, Any]] = [
            {"agentStatus": {"type": "thinking", "message": "Reading " + HOSTILE}},
            {"steps": [{"name": "load_sample_data", "status": "running", "args": {}}]},
        ]
        for stage in stages:
            with self._lock:
                if self._run != run:
                    return
                if "agentStatus" in stage:
                    self._set("agentStatus", stage["agentStatus"])
                if "steps" in stage:
                    self._rec.set(["messages", index, "steps"], stage["steps"])
            self._flush()
            time.sleep(1.5)
        with self._lock:
            if self._run != run:
                return
            self._rec.set(
                ["messages", index, "steps"],
                [
                    {
                        "name": HOSTILE,
                        "status": "used",
                        "args": {HOSTILE: HOSTILE},
                        "result": HOSTILE,
                        "display": {"kind": "image", "filename": HOSTILE},
                    },
                    {
                        "name": "generate_image",
                        "status": "used",
                        "args": {},
                        "result": "",
                        "display": {"kind": "image_failure", "reason": HOSTILE},
                    },
                ],
            )
            self._rec.set(["messages", index, "isStreaming"], False)
            self._set("error", "Could not open " + HOSTILE)
            self._set("isLoading", False)
            self._set("agentStatus", None)
            self._end_run(run)
        self._flush()

    def _approve_with(
        self,
        run: int,
        index: int,
        arguments: Any,
        *,
        tool_name: Optional[str] = None,
        details: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """The same shape testing.py's own "/approve" branch sets (`_stream_turn`), with
        the given `arguments` (and, optionally, `tool_name`/`details`) in place of its
        hardcoded well-formed `{"name": "MRHead"}` on `self._tool`. `details` renders
        exactly like an engine approval's (convert.ts keys the artifact off `details`
        being present at all, not off `kind`), so a client card can carry them too —
        useful for a realistically full card (the 320×480 spec)."""
        name = tool_name if tool_name is not None else self._tool
        streamed = self._stream(run, index, self._chunks)
        if not streamed:
            return
        with self._lock:
            if self._run != run:
                return
            self._ids += 1
            self._rec.set(
                ["messages", index, "steps"],
                [{"name": name, "status": "running", "args": arguments}],
            )
            pending: Dict[str, Any] = {
                "toolCallId": "call-{}".format(self._ids),
                "toolName": name,
                "arguments": arguments,
                "description": "Load the MRHead sample volume into the scene?",
            }
            if details is not None:
                pending["details"] = details
            self._set("pendingApproval", pending)
            self._set("isLoading", False)
            self._set("agentStatus", None)
            self._end_run(run)
        self._flush()


def options(args: argparse.Namespace) -> ChatOptions:
    if args.hostile:
        # The host's own copy is trusted and shown as given, ZWNJ and all. (No
        # presentation can be keyed by HOSTILE: ChatOptions accepts only engine tool names.)
        return ChatOptions(
            welcome=ChatWelcome(heading="Ask about " + HOST_WORD, suggestions=[HOST_WORD]),
            footer="Check " + HOST_WORD,
            tools={
                "load_sample_data": ToolPresentation(
                    activity="Loading " + HOST_WORD,
                    destructive=True,
                    warning="Deletes " + HOST_WORD,
                ),
                "reset_scene": ToolPresentation(
                    activity="Resetting " + HOST_WORD, destructive=True
                ),
            },
        )
    return ChatOptions(
        welcome=ChatWelcome(
            heading="Ask about your scene", suggestions=["Load MRHead", "Show the axial view"]
        ),
        features=ChatFeatures(
            attachments=not args.no_attachments,
            account=not args.no_account,
            settings=not args.no_settings,
            provider_sign_in=not args.no_provider_sign_in,
        ),
        allow_anonymous=not args.no_anonymous,
        reset_password_url="https://example.com/reset",  # noqa: S106 (a link, not a password)
        footer="Answers can be wrong. Check them before you act.",
        tools={
            # FakeController always asks approval for load_sample_data, so its presentation
            # is what --destructive-tool switches: marked destructive (with the host's own
            # warning when the flag carries text), the card offers no "Always allow".
            "load_sample_data": ToolPresentation(
                activity="Loading sample data",
                destructive=args.destructive_tool is not None,
                warning=args.destructive_tool or None,
            ),
            # A platform tool that runs WITHOUT asking approval (Capabilities.tool_events,
            # account mode's "search the web" trigger): RunningCall's activity text and
            # elapsed ticker have no other scripted path, since testing.py's own
            # FakeController gates every tool call on approval.
            "web_search": ToolPresentation(
                activity="Searching the web", destructive=False, warning=None
            ),
        },
    )


def account_controller(args: argparse.Namespace) -> "tuple[ChatController, Any]":
    from tests.fake_engine import _STOP, _data, _error, _json, _text
    from tests.ui_engine import AccountEngine
    from werkzeug.wrappers import Request, Response

    class EmbedEngine(AccountEngine):
        """AccountEngine plus what the page's account views need: a sign-up that waits for
        its confirmation email, a wrong password, an engine approval, one whose
        text is hostile, a platform tool that runs without approval
        at all (tool_event, the running-row spec), and ``GET /v1/skills``
        (a small platform catalogue by default, or a
        ``skills_disabled`` refusal when ``skills_disabled`` is set on this instance)."""

        skills_disabled = False
        #: `skillsList` (GET /v1/skills) runs on the same single
        #: router thread as every other command, so delaying its response here reliably
        #: queues a `selectModel`/`setFontSize` sent right after Settings opens behind it —
        #: the same window a review probe used to catch the picker showing an
        #: unsettled value.
        slow_skills_s = 0.0
        #: `GET /v1/models` populates `models.list` on its own
        #: background refresh, independent of the router thread above, so this is the one
        #: that actually creates a "not loaded yet" window for the picker's `disabled`.
        slow_models_s = 0.0
        #: The `needsSessionReset` repro. FakeEngine's own
        #: `/v1/skills/register` always reports `"replaced": true`, so `T2VSkills._post`
        #: (skills.py) resets `_registered` to exactly the current enabled set on every
        #: call and `_needs_reset` never sticks. An engine that does NOT honour `replace`
        #: (some do not) leaves the old name in `_registered` instead, so `_holds_unwanted()`
        #: stays true and `_needs_reset` is set — the genuine trigger, not a value poked
        #: onto the skills object by hand. Even so, `ChatController.set_skills_state`
        #: (controller.py) recycles the session THE INSTANT it sees the flag, self-healing
        #: it back to false within one round trip — UNLESS a run is active or an approval
        #: is pending, when the recycle is deferred. Observing the hint line therefore
        #: needs a skill disabled WHILE something is pending, not merely disabled.
        replace_not_honoured = False
        #: A partner that does not require email confirmation. The engine answers a
        #: sign-up with a session, as it answers a login, so the SDK signs the end-user in
        #: at once (auth.py's `signup`: tokens in the answer, `confirmation_required` False).
        signup_auto_confirm = False
        #: The engine stops offering "m-deep" from the second sign-in on. Every
        #: auth event makes the SDK re-read the model list (app_state.py's `_on_auth`),
        #: so signing the same account in again drops it from `models.list` on cue.
        drop_model_on_relogin = False
        _logins = 0
        #: HOSTILE in a refused sign-in's reason, the signed-in email, the model
        #: names, the platform catalogue and a provider sign-in's failure (the engine's
        #: own message from /desktop/token, which the SDK passes on as ``oauthError``).
        hostile = False

        def _user_of(self, request: Request) -> str:
            auth = request.headers.get("Authorization", "")
            with self.lock:
                return self.tokens.get(auth[len("Bearer ") :], "") if auth else ""

        def _handle(self, request: Request) -> Response:
            body = request.get_json(silent=True) or {}
            if self.hostile and request.path == "/v1/auth/oauth/desktop/token":
                return _error(400, "server_error", "Sign-in failed for " + HOSTILE)
            if (
                request.path == "/v1/skills/register"
                and request.method == "POST"
                and self.skills_disabled
            ):
                # A real engine with skills off refuses the register too (skills.py's
                # module docstring: non-fatal on BOTH routes), not only the listing: a
                # register that succeeded here would flip `available` back to true.
                if not self._user_of(request):
                    return _error(401, "authentication_error", "Invalid token")
                return _error(400, "skills_disabled", "Skills are off for this session.")
            if (
                request.path == "/v1/skills/register"
                and request.method == "POST"
                and self.replace_not_honoured
            ):
                response = super()._handle(request)
                try:
                    payload = json.loads(response.get_data(as_text=True))
                except ValueError:
                    return response
                if isinstance(payload, dict):
                    payload["replaced"] = False
                return _json(payload, response.status_code)
            if request.path == "/v1/models" and self.slow_models_s:
                time.sleep(self.slow_models_s)
            if request.path == "/v1/skills" and request.method == "GET":
                if self.slow_skills_s:
                    time.sleep(self.slow_skills_s)
                if not self._user_of(request):
                    return _error(401, "authentication_error", "Invalid token")
                if self.skills_disabled:
                    return _error(400, "skills_disabled", "Skills are off for this session.")
                return _json(
                    {
                        "skills": [
                            {
                                "name": "team-brief" + (HOSTILE if self.hostile else ""),
                                "description": "Summarise findings for the team."
                                + (" " + HOSTILE if self.hostile else ""),
                                "source": "partner",
                            }
                        ]
                    }
                )
            if request.path == "/v1/auth/signup":
                email = str(body.get("email", ""))
                if self.signup_auto_confirm:
                    with self.lock:
                        return self._issue("user-new", email, False)
                return _json(
                    {
                        "user": {"id": "user-new", "email": email},
                        "access_token": "",
                        "refresh_token": "",
                    }
                )
            if request.path == "/v1/auth/login" and body.get("password") == "wrong":
                if self.hostile:
                    return _error(401, "authentication_error", "No account named " + HOSTILE)
                return _error(401, "authentication_error", "Invalid email or password")
            if request.path == "/v1/auth/login" and self.hostile:
                with self.lock:
                    return self._issue("user-ana", "ana." + HOSTILE + "@example.com", False)
            if request.path == "/v1/auth/login" and self.drop_model_on_relogin:
                with self.lock:
                    self._logins += 1
                    if self._logins >= 2:
                        self.models = [m for m in self.models if m.get("id") != "m-deep"]
            if request.path.endswith("/messages"):
                messages = body.get("messages") or [{}]
                last = str(messages[-1].get("content", ""))
                if "email" in last and body.get("supports_approval"):
                    chunk = {
                        "id": "c",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                        "approval": {
                            "type": "approval_request",
                            "tool_name": "gmail_send_email",
                            "tool_call_id": "call-email",
                            "arguments": {"to": "alex@example.com", "subject": "Scan results"},
                            "summary": "Send an email to alex@example.com",
                            "details": [
                                {"label": "To", "value": "alex@example.com"},
                                {"label": "Subject", "value": "Scan results"},
                            ],
                        },
                    }
                    return self._sse(iter([_data(chunk)]))
                if "spoof" in last and body.get("supports_approval"):
                    # The spoof repro, in the tool name, the prompt, an argument's key AND
                    # value, and a detail's value: a right-to-left override and a
                    # zero-width space. Built with chr(), never pasted as a literal
                    # character and never written as a source-level escape sequence
                    # either, so this SOURCE FILE stays free of invisible characters (the
                    # pre-commit Cf/Cc scan).
                    rlo = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE
                    zwsp = chr(0x200B)  # ZERO WIDTH SPACE
                    # A display string the tool never opens, not a real temp path.
                    spoofed_path = "/tmp/report" + rlo + "gnp.exe"  # noqa: S108
                    chunk = {
                        "id": "c",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                        "approval": {
                            "type": "approval_request",
                            "tool_name": "open" + rlo + "file",
                            "tool_call_id": "call-spoof",
                            "arguments": {"pa" + zwsp + "th": spoofed_path},
                            "summary": "Open " + rlo + "txt.exe",
                            "details": [{"label": "File", "value": "report" + rlo + "gnp.exe"}],
                        },
                    }
                    return self._sse(iter([_data(chunk)]))
                if "search the web" in last and body.get("supports_tool_events"):
                    return self._sse(self._running_tool())
                if "explain in detail" in last and body.get("supports_approval"):
                    # A prompt long enough that the 3-line clamp actually clamps it (the spoof
                    # repro's sibling concern: a long engine description with no way to read the
                    # tail). No hostile characters here: that is the "spoof" branch's job.
                    long_summary = "Careful review of every step before this tool runs. " * 20
                    chunk = {
                        "id": "c",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                        "approval": {
                            "type": "approval_request",
                            "tool_name": "explain_tool",
                            "tool_call_id": "call-long",
                            "arguments": {},
                            "summary": long_summary,
                            "details": [],
                        },
                    }
                    return self._sse(iter([_data(chunk)]))
            return super()._handle(request)

        @staticmethod
        def _running_tool() -> "Iterator[str]":
            """A platform tool (Capabilities.tool_events) that runs WITHOUT asking
            approval, start to finish: RunningCall's activity text and elapsed ticker have
            no other scripted path, since testing.py's own FakeController gates every tool
            call on approval, and FinishedCall's platform-tool half had none either.

            The pause between `tool_start` and `tool_end` is what the elapsed ticker
            counts, so the spec can watch it pass several whole seconds before the call
            finishes. It is a real sleep on this fake engine's own request thread, which
            is the point: nothing about the turn is instantaneous.

            The text chunk after `tool_end` lands in a NEW assistant message, not the one
            holding the tool row — text after a tool call opens a new segment by design
            (src/talk2view/_reducer.py's `_on_text`, mirroring the TS
            SDK). A spec that reaches for the tool row through "the last assistant
            message" therefore stops finding it the moment this text arrives; reach for
            the row itself.
            """
            yield _data(
                {
                    "id": "c",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                    "tool_event": {
                        "type": "tool_start",
                        "tool_name": "web_search",
                        "tool_call_id": "call-search",
                        "arguments": {"query": "segmentation preset"},
                    },
                }
            )
            time.sleep(5.0)
            yield _data(
                {
                    "id": "c",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                    "tool_event": {
                        "type": "tool_end",
                        "tool_name": "web_search",
                        "tool_call_id": "call-search",
                        "status": "ok",
                    },
                }
            )
            yield _data(_text("Here is what I found."))
            yield _data(_STOP)

    engine = EmbedEngine().start()
    if args.anonymous_refusal:
        engine.anonymous_refusal = (403, args.anonymous_refusal)
    if args.skills_disabled:
        engine.skills_disabled = True
    if args.slow_skills:
        engine.slow_skills_s = args.slow_skills
    if args.slow_models:
        engine.slow_models_s = args.slow_models
    if args.needs_session_reset:
        engine.replace_not_honoured = True
    if args.signup_auto_confirm:
        engine.signup_auto_confirm = True
    if args.drop_model_on_relogin:
        engine.drop_model_on_relogin = True
    if args.desktop_sign_in:
        engine.desktop_providers = ["apple", "google"]
    if args.oauth_browser == "deny":
        engine.desktop_answer = "access_denied"
    if args.hostile:
        engine.hostile = True
        for model in engine.models:
            model["name"] = str(model.get("name") or model["id"]) + " " + HOSTILE

    @tool(permission=True)
    def delete_volume(name: str) -> str:
        """Delete a volume from the scene.

        Args:
            name: The volume.
        """
        return "deleted " + name

    t2v = Talk2View("pk_test", base_url=engine.url())
    if args.slow_cancel:
        # The busy window, made wide: after Cancel, the UI server keeps
        # auth.oauthPending (and refuses a new oauthStart as busy) until the sign-in thread
        # has ended. Sleeping after the cancelled call, still on that thread, holds it open.
        from talk2view.errors import ProviderSignInError

        slow = args.slow_cancel
        _sign_in = t2v.auth.sign_in_with_provider

        def _slow_to_end(*a: Any, **kw: Any) -> Any:
            try:
                return _sign_in(*a, **kw)
            except ProviderSignInError as exc:
                if exc.type == "oauth_cancelled":
                    time.sleep(slow)
                raise

        t2v.auth.sign_in_with_provider = _slow_to_end  # type: ignore[method-assign]
    if args.late_login_result:
        # The late-login-result repro. auth.login/signup (auth.py, read-only) already
        # store the tokens and notify the auth-state listeners — which is what pushes the
        # auth-slice op to the page — BEFORE they return to the router, which records the
        # command's own result only once the call returns. On localhost the two land in
        # the same SSE frame; sleeping here, after the real call (and its notify) already
        # ran, delays only the RETURN, separating them into different frames without
        # touching SDK source — a monkey-patch on this fixture's own instance.
        delay = args.late_login_result
        for _name in ("login", "signup"):
            _original = getattr(t2v.auth, _name)

            def _delayed(*a: Any, _original: Any = _original, **kw: Any) -> Any:
                result = _original(*a, **kw)
                time.sleep(delay)
                return result

            setattr(t2v.auth, _name, _delayed)
    t2v.tools.register_from_functions([delete_volume])
    t2v.skills.add(
        Skill(name="scan-report", description="Write a scan report.", content="Report the scan.")
    )
    if args.register_signed_out:
        # The signed-out-register repro, made deterministic. The SDK registers the host's
        # skills 0.5 s after `add()` (its debounce); signed out, that register is refused
        # with a 401, and the edit waits for the next message's flush, since nothing re-sends it on
        # sign-in. Flushing here, before anyone could sign in, is that same refused
        # register without the race: `skills.available` stays false after sign-in.
        t2v.skills.flush()
    if args.hostile:
        t2v.skills.add(
            Skill(name="skill-" + HOSTILE, description="Writes " + HOSTILE, content="noop")
        )
    if args.huge_skill:
        # The huge-skill repro: a 255-char unbroken name (the SDK's own cap on the
        # `skillEnable`/`skillDisable` wire command) and a 100,000-char description, mostly
        # unbroken runs so ordinary word-wrap alone would not save the layout.
        long_name = "n" * 255
        long_description = ("segmentationreview" * 10 + " ") * 5000  # ~100,000 chars
        t2v.skills.add(Skill(name=long_name, description=long_description, content="noop"))
    controller = ChatController(t2v, capabilities=Capabilities(approval=True, tool_events=True))
    return controller, engine


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--static-dir", required=True)
    parser.add_argument("--mode", choices=("fake", "account"), default="fake")
    parser.add_argument(
        "--chunk-delay", type=float, default=0.01, help="fake mode: seconds per word"
    )
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--no-attachments", action="store_true")
    parser.add_argument("--no-anonymous", action="store_true")
    parser.add_argument("--no-clipboard", action="store_true")
    parser.add_argument("--no-account", action="store_true")
    parser.add_argument("--no-settings", action="store_true")
    parser.add_argument("--anonymous-refusal", default=None)
    parser.add_argument(
        "--destructive-tool",
        nargs="?",
        const="",
        default=None,
        metavar="WARNING",
        help="mark load_sample_data destructive; the value, if any, is the host's warning",
    )
    parser.add_argument(
        "--huge-skill",
        action="store_true",
        help="account mode: add a skill with a 255-char name and ~100,000-char description",
    )
    parser.add_argument(
        "--skills-disabled",
        action="store_true",
        help="account mode: GET /v1/skills and POST /v1/skills/register refuse with "
        "skills_disabled",
    )
    parser.add_argument(
        "--slow-skills",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="account mode: delay GET /v1/skills, queueing anything sent right after it "
        "behind it on the same router thread",
    )
    parser.add_argument(
        "--slow-models",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="account mode: delay GET /v1/models, leaving models.list unloaded for a while",
    )
    parser.add_argument(
        "--needs-session-reset",
        action="store_true",
        help="account mode: the fake engine does not honour skill-register's replace, "
        "so skills.needsSessionReset sticks once an already-registered skill is disabled",
    )
    parser.add_argument(
        "--late-login-result",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="account mode: auth.login/signup (auth.py) return this many seconds after "
        "their own real call and notify, separating the auth-slice op from the "
        "command's result into different SSE frames",
    )
    parser.add_argument(
        "--register-signed-out",
        action="store_true",
        help="account mode: the host's start-up skill register reaches the engine before "
        "anyone signs in (a 401), and waits for the next message",
    )
    parser.add_argument(
        "--signup-auto-confirm",
        action="store_true",
        help="account mode: a sign-up answers with a session, as for a partner that does "
        "not require email confirmation",
    )
    parser.add_argument(
        "--drop-model-on-relogin",
        action="store_true",
        help="account mode: the engine stops offering m-deep from the second sign-in on",
    )
    parser.add_argument(
        "--hostile",
        action="store_true",
        help="put one right-to-left override (HOSTILE) in every untrusted plain-text surface "
        "(a '/hostile' turn and '/hostileapprove' in fake mode, the account views in account "
        "mode) and a ZWNJ in the host's own copy",
    )
    parser.add_argument(
        "--desktop-sign-in",
        action="store_true",
        help="account mode: the fake engine offers Apple and Google desktop sign-in",
    )
    parser.add_argument(
        "--no-provider-sign-in",
        action="store_true",
        help="turn features.provider_sign_in off in the options",
    )
    parser.add_argument(
        "--slow-cancel",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="account mode: a cancelled provider sign-in's thread ends this many seconds "
        "late, keeping auth.oauthPending set (the busy window)",
    )
    parser.add_argument(
        "--oauth-browser",
        choices=("auto", "hold", "deny"),
        default="auto",
        help="account mode: what the fake browser does with a desktop sign-in's start URL: "
        "sign in at once, wait for the oauth-finish control, or refuse (access_denied)",
    )
    args = parser.parse_args(argv)

    host = RecordingHost(
        theme=Theme(mode="dark" if args.dark else "light"),
        clipboard=not args.no_clipboard,
        oauth_browser=args.oauth_browser,
    )
    engine = None
    if args.mode == "account":
        controller, engine = account_controller(args)
    else:
        controller = RecordingController(reply=REPLY, chunk_delay_s=args.chunk_delay)
    # `skills_rescan`: the host's own rescan of its skill folders (router.py's
    # `on_skills_list`), fired every time `skillsList` reaches an account (it
    # proves the command was actually sent, since the signed-out gate is the page's
    # own — nothing server-side distinguishes "not asked yet" from "asked, got nothing").
    server = UIServer(
        controller,
        host,
        static_dir=args.static_dir,
        options=options(args),
        on_skills_list=lambda: emit({"host": "skills_rescan", "value": ""}),
    )
    emit({"url": server.start()})

    def attachments_off() -> None:
        # The SDK fixes a UIServer's options for its lifetime, so nothing a host can call
        # turns a feature off mid-session. The page must still cope when the options
        # slice changes; this sets that slice on the test server's own hub.
        wire = options(args).to_wire()
        wire["features"]["attachments"] = False
        server._hub.set_slice("options", wire)

    def oauth_finish() -> None:
        # The end-user presses Continue on the check screen.
        if HELD_SIGN_IN:
            follow_later(HELD_SIGN_IN.pop())

    def switch_off() -> None:
        # The partner turns desktop sign-in off: a Continue after it is refused to the
        # app's loopback (the SDK then turns capabilities.oauth off and records the
        # failure).
        if engine is not None:
            engine.desktop_providers = []

    def pending_unknown() -> None:
        # A wire value the page cannot name: a pending sign-in for a provider it does not
        # know, set on the test server's own hub (as attachments_off sets the options).
        server._hub.set_slice(
            "auth",
            {
                "user": None,
                "isAuthenticated": False,
                "isAnonymous": False,
                "oauthPending": {"provider": "github"},
                "oauthError": None,
            },
        )

    controls = {
        "attachments-off": attachments_off,
        "oauth-finish": oauth_finish,
        "switch-off": switch_off,
        "pending-unknown": pending_unknown,
    }

    def wait_for_eof() -> None:
        for line in sys.stdin:
            control = controls.get(line.strip())
            if control is not None:
                control()
        host.stop()

    threading.Thread(target=wait_for_eof, daemon=True).start()
    try:
        host.run_forever()
    finally:
        server.stop()
        controller.close()
        if engine is not None:
            engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
