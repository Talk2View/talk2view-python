"""A stateful fake of the engine routes the identity tests drive, on a threaded HTTP server.

One session per user, get-or-create like the engine's ``POST /v1/sessions``; ``DELETE``
removes it (204) or answers 404 for any other id; ``/v1/skills/register`` honours
``replace``. A message containing ``delete`` pauses on the ``delete_volume`` client tool;
one containing ``slow`` streams a first chunk and then holds until ``release_slow`` is set;
one containing ``engine-error`` streams a ``model_error`` error chunk.
``expire(user_id)`` makes that user's access token 401 and their refresh token rejected.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

TOOL = "delete_volume"


def _json(body: Any, status: int = 200) -> Response:
    return Response(json.dumps(body), status=status, content_type="application/json")


def _error(status: int, error_type: str, message: str = "nope") -> Response:
    return _json({"error": {"type": error_type, "message": message}}, status)


def _data(chunk: Dict[str, Any]) -> str:
    return "data: {}\n\n".format(json.dumps(chunk))


def _text(content: str) -> Dict[str, Any]:
    return {"id": "c", "choices": [{"index": 0, "delta": {"content": content}}]}


_STOP = {"id": "c", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}


class FakeEngine:
    """Serves on ``server`` (the ``engine`` fixture shares one across tests: stopping a
    threaded server waits out its 0.5 s poll), or on a server of its own."""

    def __init__(self, server: Optional[HTTPServer] = None) -> None:
        self.lock = threading.Lock()
        self._owns_server = server is None
        self.server = server if server is not None else HTTPServer(threaded=True)
        self.tokens: Dict[str, str] = {}  # access token -> user id
        self.refresh: Dict[str, str] = {}  # refresh token -> user id
        self.sessions: Dict[str, Dict[str, Any]] = {}  # user id -> session
        self.log: List[str] = []
        self.convert_status = 409
        self.release_slow = threading.Event()
        self.tool_arguments: Dict[str, Any] = {"name": "CT"}  # the delete_volume call's
        self.slow_started = threading.Event()
        self._anonymous = 0
        self._issued = 0
        # Desktop sign-in. None: the providers route 404s, as an
        # engine older than the SDK does.
        self.desktop_providers: Optional[List[str]] = []
        self.desktop_probe_status: Optional[int] = None  # the providers route fails with it
        self.desktop_answer = "code"  # or the `error` the engine sends the loopback
        self.desktop_start_url: Optional[str] = None  # replaces /begin's start_url
        self.desktop_open_window = 120.0  # seconds from /begin in which Continue works
        self.desktop_begin_refusal: Optional[Tuple[int, str]] = None  # /begin's error
        self.desktop_refuse_token = False
        # Replaces /token's answer to a valid code (a half session, say).
        self.desktop_token_body: Optional[Dict[str, Any]] = None
        self.desktop_token_gate: Optional[threading.Event] = None  # holds /token until set
        self.desktop_requests: List[Tuple[str, Dict[str, Any]]] = []
        self.desktop_probes = 0
        self.desktop_user_id = "u-desktop"
        self.desktop_email = "desktop@example.com"
        self._desktop_begun: Dict[str, Dict[str, Any]] = {}
        self._desktop_codes: Dict[str, str] = {}
        self._desktop_callbacks: Dict[str, str] = {}  # fake callback key -> loopback URL

    # ── lifecycle ──

    def start(self) -> "FakeEngine":
        if not self.server.is_running():
            self.server.start()
        self.server.clear()
        self.server.expect_request(re.compile(".*")).respond_with_handler(self._handle)
        return self

    def stop(self) -> None:
        self.release_slow.set()
        self.server.clear()
        if self._owns_server and self.server.is_running():
            self.server.stop()

    def url(self) -> str:
        return self.server.url_for("")

    # ── test controls and views ──

    def expire(self, user_id: str) -> None:
        """The user's access token now 401s and their refresh token is rejected."""
        with self.lock:
            for table in (self.tokens, self.refresh):
                for token in [t for t, u in table.items() if u == user_id]:
                    del table[token]

    def skills_of(self, user_id: str) -> Optional[List[str]]:
        with self.lock:
            session = self.sessions.get(user_id)
            return list(session["skills"]) if session is not None else None

    def histories_of(self, user_id: str) -> List[List[Any]]:
        with self.lock:
            session = self.sessions.get(user_id)
            return [list(h) for h in session["histories"]] if session is not None else []

    def paths(self) -> List[str]:
        with self.lock:
            return list(self.log)

    # ── routes ──

    def _issue(self, user_id: str, email: str, anonymous: bool) -> Response:
        self._issued += 1
        access = "tok-{}-{}".format(user_id, self._issued)
        refresh = "ref-{}-{}".format(user_id, self._issued)
        self.tokens[access] = user_id
        self.refresh[refresh] = user_id
        return _json(
            {
                "access_token": access,
                "refresh_token": refresh,
                "user": {"id": user_id, "email": email},
                "is_anonymous": anonymous,
            }
        )

    def _session(self, user_id: str) -> Dict[str, Any]:
        session = self.sessions.get(user_id)
        if session is None:
            session = self.sessions[user_id] = {
                "id": str(uuid.uuid4()),
                "skills": [],
                "histories": [],
            }
        return session

    def _handle(self, request: Request) -> Response:
        if request.path.startswith("/v1/auth/oauth/desktop/"):
            return self._desktop(request)
        path, method = request.path, request.method
        body = request.get_json(silent=True) or {}
        auth = request.headers.get("Authorization", "")
        with self.lock:
            user_id = self.tokens.get(auth[len("Bearer "):], "") if auth else ""
            self.log.append("{} {} {}".format(method, path, user_id or "-"))
            if path == "/v1/auth/anonymous":
                self._anonymous += 1
                return self._issue("anon-{}".format(self._anonymous), "", True)
            if path == "/v1/auth/login":
                email = str(body.get("email", ""))
                return self._issue("user-" + email.split("@")[0], email, False)
            if path == "/v1/auth/refresh":
                if body.get("refresh_token") not in self.refresh:
                    return _error(401, "authentication_error", "Invalid refresh token")
                return self._issue(self.refresh[body["refresh_token"]], "", False)
            if path == "/v1/tools/register" and not user_id:
                return _json({"registered": [], "count": 0})  # a host registering at startup
            if not user_id:
                return _error(401, "authentication_error", "Invalid token")
            if path == "/v1/auth/convert":
                return _error(self.convert_status, "convert_error", "Already registered")
            if path == "/v1/auth/logout":
                return Response(status=204)
            if path == "/v1/models":
                return _json({"object": "list", "data": []})
            if path == "/v1/sessions" and method == "POST":
                session = self._session(user_id)
                return _json({"session_id": session["id"], "thread_id": "th", "model": "m"})
            match = re.match(r"^/v1/sessions/([^/]+)$", path)
            if match and method == "DELETE":
                session = self.sessions.get(user_id)
                if session is None or session["id"] != match.group(1):
                    return _error(404, "not_found", "Session not found")
                del self.sessions[user_id]
                return Response(status=204)
            if path == "/v1/tools/register":
                self._session(user_id)
                return _json({"registered": [], "count": 0})
            if path == "/v1/skills/register":
                session = self._session(user_id)
                names = [s["name"] for s in body.get("skills", [])]
                if body.get("replace"):
                    session["skills"] = list(dict.fromkeys(names))
                else:
                    session["skills"] = list(dict.fromkeys(session["skills"] + names))
                return _json(
                    {
                        "registered": names,
                        "count": len(names),
                        "removed": [],
                        "replaced": bool(body.get("replace")),
                    }
                )
            match = re.match(r"^/v1/sessions/([^/]+)/(messages|resume)$", path)
            if not match:
                return _error(404, "not_found", path)
            session = self.sessions.get(user_id)
            if session is None or session["id"] != match.group(1):
                return _error(404, "not_found", "Session not found")
            if match.group(2) == "resume":
                return self._sse(iter([_data(_text("done")), _data(_STOP)]))
            history = [(m.get("role"), m.get("content")) for m in body.get("messages", [])]
            session["histories"].append(history)
            last = str(history[-1][1]) if history else ""
        if "delete" in last:
            interrupt = {
                "id": "c",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "interrupt": {
                    "type": "tool_call",
                    "tool_name": TOOL,
                    "tool_call_id": "tc-{}-{}".format(user_id, len(history)),
                    "arguments": dict(self.tool_arguments),
                },
            }
            return self._sse(iter([_data(interrupt)]))
        if "slow" in last:
            return self._sse(self._slow())
        if "engine-error" in last:
            error = {"id": "c", "error": {"type": "model_error", "message": "The model failed"}}
            return self._sse(iter([_data(error)]))
        return self._sse(iter([_data(_text("ok")), _data(_STOP), "data: [DONE]\n\n"]))

    def _desktop(self, request: Request) -> Response:
        """The engine's desktop sign-in routes, with the browser leg collapsed: GET /go
        redirects straight to the loopback listener (no confirmation screen, no provider)."""
        path = request.path
        body = request.get_json(silent=True) or {}
        with self.lock:
            providers = self.desktop_providers
            if path == "/v1/auth/oauth/desktop/providers":
                self.desktop_probes += 1
            elif request.method == "POST":
                self.desktop_requests.append((path, dict(body)))
        if providers is None:
            return _error(404, "not_found", "Not found")
        if path == "/v1/auth/oauth/desktop/providers":
            status = self.desktop_probe_status
            if status is not None:
                return _error(status, "server_error", "Try again")
            return _json({"providers": list(providers)})
        if path == "/v1/auth/oauth/desktop/begin":
            refusal = self.desktop_begin_refusal
            if refusal is not None:
                return _error(refusal[0], refusal[1], "Rate limit exceeded: 10 per 1 minute")
            if not providers:
                return _error(403, "desktop_sign_in_disabled",
                              "Sign-in with Apple or Google is not turned on for this app")
            handle = secrets.token_urlsafe(32)
            with self.lock:
                self._desktop_begun[handle] = dict(body, _at=time.monotonic())
                start_url = self.desktop_start_url
            if start_url is None:
                start_url = "{}/v1/auth/oauth/desktop/go/{}".format(self.url().rstrip("/"),
                                                                     handle)
            return _json({"start_url": start_url, "expires_in": 300})
        if path == "/v1/auth/oauth/desktop/done":
            # The engine's static page the loopback listener 303s the browser to.
            return Response("Signed in. You can close this tab.", status=200)
        if path.startswith("/v1/auth/oauth/desktop/go/"):
            # The engine's check screen, read-only as the engine's /go is: a switch that
            # is off, an unknown handle or a closed window get the plain page, and the
            # app is told nothing (only an authenticated Continue may reach its
            # loopback). Pressing Continue is collapsed into a redirect to /confirm.
            handle = path.rsplit("/", 1)[1]
            with self.lock:
                begun = self._desktop_begun.get(handle)
                window = self.desktop_open_window
            if (begun is None or not providers
                    or time.monotonic() - float(begun["_at"]) > window):
                return Response("That sign-in has expired", status=400)
            return Response(status=302, headers={
                "Location": "{}/v1/auth/oauth/desktop/confirm/{}".format(
                    self.url().rstrip("/"), handle)})
        if path.startswith("/v1/auth/oauth/desktop/confirm/"):
            # Continue (the engine's POST /confirm; a GET here). Spends the handle, then
            # refuses as the engine does, the switch first, then the window, handing the
            # refusal straight to the app's loopback. Otherwise on to the provider, which
            # is collapsed: its return to the engine's callback 302s to the loopback with
            # the code (or `desktop_answer`'s error). Both hops leave this page as the
            # engine's do: a 200 with a meta refresh, never a redirect (the check screen's
            # CSP `form-action 'self'` would block one after the form POST).
            with self.lock:
                begun = self._desktop_begun.pop(path.rsplit("/", 1)[1], None)
                answer = self.desktop_answer
                window = self.desktop_open_window
            if begun is None:
                return Response("That sign-in has expired", status=400)
            query = {"state": str(begun.get("state"))}
            refused = None
            if not providers:
                refused = "desktop_sign_in_disabled"
            elif time.monotonic() - float(begun["_at"]) > window:
                refused = "expired"
            if refused is not None:
                query["error"] = refused
                return _meta_refresh(_loopback(begun, query))
            if answer == "code":
                code = secrets.token_urlsafe(32)
                with self.lock:
                    self._desktop_codes[code] = str(begun.get("code_challenge"))
                query["code"] = code
            else:
                query["error"] = answer
            key = secrets.token_urlsafe(16)
            with self.lock:
                self._desktop_callbacks[key] = _loopback(begun, query)
            return _meta_refresh("{}/v1/auth/oauth/desktop/fake-callback/{}".format(
                self.url().rstrip("/"), key))
        if path.startswith("/v1/auth/oauth/desktop/fake-callback/"):
            with self.lock:
                location = self._desktop_callbacks.pop(path.rsplit("/", 1)[1], None)
            if location is None:
                return Response("You can close this tab.", status=200)
            return Response(status=302, headers={"Location": location})
        if path == "/v1/auth/oauth/desktop/token":
            gate = self.desktop_token_gate
            if gate is not None:
                gate.wait(10)  # a slow engine: lets a test act while /token is in flight
            with self.lock:
                providers = self.desktop_providers  # read again: it may have changed
            if not providers:
                return _error(403, "desktop_sign_in_disabled",
                              "Sign-in with Apple or Google is not turned on for this app")
            verifier = str(body.get("code_verifier", ""))
            digest = hashlib.sha256(verifier.encode("utf-8")).digest()
            expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            with self.lock:
                challenge = self._desktop_codes.pop(str(body.get("code", "")), None)
                if self.desktop_refuse_token or challenge is None or challenge != expected:
                    return _error(400, "invalid_grant", "Sign-in did not complete")
                if self.desktop_token_body is not None:
                    return _json(self.desktop_token_body)
                return self._issue(self.desktop_user_id, self.desktop_email, False)
        return _error(404, "not_found", "Not found")

    def _slow(self) -> Iterator[str]:
        yield _data(_text("partial"))
        self.slow_started.set()
        self.release_slow.wait(10)
        yield _data(_STOP)

    @staticmethod
    def _sse(chunks: Iterator[str]) -> Response:
        return Response(chunks, mimetype="text/event-stream")


def open_check_screen(url: str) -> str:
    """The browser opens a start URL and shows the check screen: returns where Continue
    goes, so a test can act before pressing it (``follow_in_browser`` on the result)."""
    with httpx.Client(follow_redirects=False, timeout=10) as browser:
        response = browser.get(url)
    assert response.status_code == 302, response.status_code
    return str(response.headers["Location"])


def _loopback(begun: Dict[str, Any], query: Dict[str, str]) -> str:
    return "http://127.0.0.1:{}/t2v/callback?{}".format(begun.get("port"), urlencode(query))


def _meta_refresh(target: str) -> Response:
    """The engine's hand-off page: a 200 that moves the browser on with a meta refresh."""
    quoted = html.escape(target, quote=True)
    return Response(
        "<!doctype html><meta http-equiv=\"refresh\" content=\"0;url={0}\">"
        "<p><a href=\"{0}\">Continue</a></p>".format(quoted),
        status=200, content_type="text/html",
    )


_META_REFRESH = re.compile(r'<meta http-equiv="refresh" content="0;url=([^"]+)">')


def browse(url: str) -> Tuple[int, List[str]]:
    """A browser: follows redirects and the engine's meta refreshes. Returns the last
    status and every URL visited, in order; (0, visited) when a server was gone (the
    listener, once the attempt had ended)."""
    visited: List[str] = []
    try:
        with httpx.Client(follow_redirects=True, timeout=10) as browser:
            for _ in range(5):
                response = browser.get(url)
                visited.extend(str(r.url) for r in response.history)
                visited.append(str(response.url))
                match = _META_REFRESH.search(response.text) if response.status_code == 200 else None
                if match is None:
                    return response.status_code, visited
                url = html.unescape(match.group(1))
            return response.status_code, visited
    except httpx.HTTPError:
        return 0, visited


def follow_in_browser(url: str) -> int:
    """What a browser does with a desktop sign-in's start URL: open it, press Continue,
    follow the engine's hand-off to the loopback listener, and the listener's 303 to the
    engine's `done` page. Returns the last status (200 at `done`), or 0 when the listener
    was gone (the attempt had ended)."""
    return browse(url)[0]
