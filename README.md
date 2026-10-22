# Talk2View Python SDK

Python SDK for the [Talk2View Platform](https://talk2view.com) — add AI-powered natural language control to any Python application.

Built for desktop apps like [3D Slicer](https://www.slicer.org/), scientific tools, and any Python environment where you want users to interact with your app through natural language.

## Install

```bash
pip install talk2view
```

Install from PyPI: its wheels include the chat page that `talk2view.ui` serves. A
checkout or a `git+https://` install has to build that page first (see
[Development](#development)); the build refuses to package the SDK without it.

You need a partner key. Talk2View is in private beta:
**[request a partner key](https://talk2view.com/resources/contact?subject=partner-key)**.
You get one key, `pk_live_…`, a public identifier that is safe to ship inside your app;
the docs are at [docs.talk2view.com](https://docs.talk2view.com).

## Intended use and data handling

Talk2View is for education, research and general information. It is not a medical
device: it is not regulated or approved as one, and it must not be used to diagnose or
treat any condition or to make clinical decisions. Its answers come from AI models and
can be wrong.

Do not send protected health information (PHI), or any other data that can identify a
patient, through the SDK. What end-users type, attach or say, and what your tools return,
is passed to third-party AI providers, and Talk2View has no safeguard that detects,
redacts or blocks such data.

The [Terms of Use](https://talk2view.com/legal/terms-of-use) and the
[Privacy Policy](https://talk2view.com/legal/privacy-policy) say this in full.

## Quick Start

```python
from talk2view import Talk2View, tool

# 1. Initialize with your partner key
t2v = Talk2View(partner_key="pk_live_abc123")

# 2. Authenticate a user
user = t2v.auth.login("user@example.com", "password")

# 3. Define tools the AI can call (runs locally in your app)
@tool
def create_shape(shape: str, size: int) -> str:
    """Create a 3D shape in the viewport.

    Args:
        shape: The shape type (cube, sphere, cylinder).
        size: The size in units.
    """
    # Your app logic here
    return f"Created {shape} with size {size}"

# 4. Register tools
t2v.tools.register_from_functions([create_shape])

# 5. Chat — tool calls are handled automatically
for event in t2v.chat("Make a large red cylinder"):
    if event.type == "text":
        print(event.content, end="")
    elif event.type == "tool_call":
        print(f"\n[Tool called: {event.tool_name}]")
    elif event.type == "done":
        print("\n--- Done ---")
```

## How It Works

```
Your App                    Talk2View Platform
────────                    ──────────────────
t2v.chat("message")  ───►  AI Agent processes message
                      ◄───  SSE stream: text chunks
                      ◄───  SSE stream: tool_call interrupt
@tool handler runs    ───►  Tool result sent to /resume
locally in your app   ◄───  SSE stream: more text
                      ◄───  SSE stream: done
```

The SDK handles the full interrupt/resume loop automatically. When the AI decides to call one of your tools:

1. The stream pauses with an interrupt
2. Your `@tool` function executes **locally in your process** (important for desktop apps — your tools have full access to the app's state)
3. The result is sent back to the server
4. The AI continues with the tool's output

## API Reference

### `Talk2View`

Main entry point.

```python
t2v = Talk2View(
    partner_key="pk_live_...",          # Required — your partner API key
    base_url="https://engine.talk2view.com",  # Optional — API endpoint
    storage=MemoryStorage(),            # Optional — token persistence backend
    model="gpt-4.1-mini",              # Optional — default model
    dispatcher=None,                    # Optional — where handlers run (see below)
    capabilities=None,                  # Optional — opt-in engine features
    data_storage=None,                  # Optional — skills + preferences (default: storage)
    anonymous_auto_start=False,         # Optional — start a demo session when signed out
    request_timeout=30.0,               # Optional — seconds per request (and to open a stream)
    upload_timeout=120.0,               # Optional — seconds per upload (attachments, audio)
    memory_path=None,                   # Optional — a directory for on-device memory (see below)
)
```

Both timeouts match the TypeScript SDK's defaults and must be positive numbers
of seconds (anything else raises `ValueError`). An open chat stream is not
bound by them: it may go 300 s between bytes, and the engine's heartbeats
arrive well inside that (the TypeScript SDK sets no read limit once a stream is
open).

#### `t2v.chat(message, system_prompt=None, history=None, attachments=None, cancel=None)`

Send a message and stream the response. Auto-creates a session if needed. Returns an `Iterator[ChatEvent]`.

```python
for event in t2v.chat("What can you see in the scene?"):
    match event.type:
        case "text":       print(event.content, end="")
        case "reasoning":  print(f"[thinking] {event.content}")
        case "tool_call":  print(f"Calling {event.tool_name}({event.arguments})")
        case "status":     print(f"[{event.status}: {event.message}]")
        case "todos":      print(f"Plan: {event.todos}")
        case "error":      print(f"[{event.error_type}] {event.message}")
        case "done":       print(f"\nThread: {event.thread_id}")
```

This is a generator: nothing is sent until the first `next()`, and failures
surface from the iteration rather than from the call.

If the engine has lost the chat session (a deploy, a restart, an eviction) and
nothing of the reply has arrived yet, a new session is created and the turn is
resent once — subscribe with `t2v.on_session_recovered(callback)` to learn the
new session id. Once the reply has started nothing is resent: an `error` event
with `error_type="session_lost"` is yielded instead, because a tool handler may
already have run on your side.

#### `t2v.stop()`

Stop the in-flight `chat()` / `respond_to_approval()` stream. Safe to call from
any thread — it unblocks a read that is stalled mid-stream. Events already
yielded are kept and the iteration then ends quietly, without raising.

```python
threading.Thread(target=consume_chat, daemon=True).start()
t2v.stop()  # e.g. from a Cancel button on the UI thread
```

`chat()` registers its token on the consumer's first `next()`, so a `stop()`
that lands before then is lost. Pass your own token when you need to cancel
earlier, or to cancel one specific turn:

```python
from talk2view import CancelToken, Cancelled

token = CancelToken()
for event in t2v.chat("Segment the liver", cancel=token):
    ...
token.cancel()  # from any thread
```

A cancelled turn ends the `chat()` iteration quietly. The lower-level
`T2VSession` / `T2VClient` iterators raise `Cancelled` instead.

#### `t2v.create_session(model=None)`

Explicitly create a new session. Returns a `T2VSession`.

#### `t2v.clear_session()`

Delete the current session and reset conversation history. With `memory_path`, it also
starts a new memory conversation and queues the one that ended for extraction (see
[On-device memory](#on-device-memory-memory_path)).

#### `t2v.upload_attachment(file, filename=None, mime_type=None)`

Upload an image or PDF and pass the returned `Attachment` to
`chat(..., attachments=[attachment])`.

#### `t2v.close()`

Release the pooled HTTP connections. Long-lived hosts should call this on
shutdown, or use `Talk2View` as a context manager.

### `t2v.auth`

Authentication methods.

```python
user = t2v.auth.login("user@example.com", "password")
outcome = t2v.auth.signup("user@example.com", "password")   # a SignupOutcome
t2v.auth.logout()

t2v.auth.is_authenticated()  # bool
t2v.auth.get_user()          # User | None
t2v.auth.get_valid_access_token()   # str | None, refreshed first if about to expire

# Subscribe to auth state changes
unsubscribe = t2v.auth.on_auth_state_change(lambda user: print(user))
unsubscribe()  # Stop listening
```

`signup()` returns a `SignupOutcome`: `user` is set when a session exists now, and
`confirmation_required` is True while the emailed confirmation link is unclicked
(the partner decides whether confirmation is required). `login()` raises
`AuthenticationError` when the engine's answer lacks the user or either token, and
`signup()` does when it carries only one of the two (the account exists: sign in);
nothing is stored in either case.

`on_auth_state_change` hears `None` for every sign-out: `logout()`, and a refresh
token the engine rejects. In that case the request that met the rejection raises
`AuthenticationError("Session expired…")`, the stored session is already cleared,
and the listeners run on that request's thread. A listener that raises is logged
and the others still run. The SDK's own listeners (`Talk2View`'s, a
`ChatController`'s) always run before any added here.

`get_valid_access_token(force_refresh=False)` is for a request the SDK does not
make for you, such as a direct call to another service (the TypeScript SDK's
`getValidAccessToken()`). It refreshes first when the stored token is missing,
expired, or within 30 s of expiring; `force_refresh=True` refreshes anyway (use
it after a direct request still got a 401). It shares the one refresh the SDK's
own requests use, so the rotating refresh token is never spent twice. It returns
`None` only when the session is dead: the session is then cleared and the
listeners hear `None`. With nothing stored at all it returns `None` and the
listeners hear nothing, since nobody was signed in. The TypeScript SDK notifies
its listeners with `null` in that case. On a transient failure (the network, a
409, a 429) it returns the stored token unchanged. A token whose expiry cannot
be read is returned as it is.

### Sign in with Apple or Google (desktop)

```python
providers = t2v.auth.desktop_sign_in_providers()   # [] until the partner turns it on
if "apple" in providers:
    user = t2v.auth.sign_in_with_provider(
        "apple",
        open_browser=webbrowser.open,   # a Qt host: lambda url: host.post(lambda: host.open_external(url))
        timeout=300,
        cancel=token,                   # a talk2view.cancel.CancelToken, optional
    )
```

The sign-in runs in the end-user's own browser, which first shows a Talk2View screen
asking them to confirm that an app on their computer is signing them in. It blocks until
the browser comes back, so call it off your UI thread. Failures raise
`ProviderSignInError`, whose message is fit to show the end-user and whose `type` is one
of `oauth_cancelled`, `oauth_timeout`, `oauth_denied`, `oauth_failed`,
`oauth_browser_unavailable`, `desktop_sign_in_disabled` (also when the partner turns it
off while the end-user is on the Talk2View screen), `oauth_expired` (Continue came after
the start link expired), `rate_limited` (too many sign-ins started from this network) or
`engine_url_mismatch` (below). The partner turns it on in the Talk2View dashboard
(Settings → Desktop apps). It needs a browser on the same computer as the app: over
remote desktop or SSH, sign in with email and password.

The SDK opens only a start page on the engine it talks to (same scheme, host and port as
`base_url`). The engine builds that page's address from its `oauth_login_redirect_base`
setting, so the two must match: a staging or local engine left on the default
(`https://engine.talk2view.com`) fails every attempt with `engine_url_mismatch`, and the
SDK logs both addresses.

Another sign-in wins over a waiting one. If `login()`, `signup()` or `logout()` changes
the stored session while the browser is out, the provider session that comes back later
is dropped (`oauth_cancelled`), even one the engine has already issued. A guest session
started meanwhile does not count: the provider sign-in replaces it. To stop waiting,
cancel the token (the UI server cancels before it signs in with a password or signs
out). A cancel, `login()` or `logout()` that comes while the provider session is being
stored waits for that store and its `on_auth_state_change` listeners to finish, then
takes effect. So never call `cancel()`, `login()` or `logout()` while holding a lock one
of your listeners takes, and never cancel another provider sign-in's token from an auth
listener (with two sign-ins running, each would wait for the other). A token refresh
that is under way while a new session is stored never overwrites it or, when the engine
rejects that refresh, signs it out: it only ever writes over, or clears, the session it
refreshed.

### Anonymous demo sessions

An end-user can try the app before creating an account, if the partner has enabled
anonymous access for the partner key. **This requires partner configuration**: by
default the engine also requires a Cloudflare Turnstile captcha token, which a native
desktop app cannot obtain by itself — the partner either turns the captcha off for the
desktop partner key or the host obtains a token some other way and passes it in.

```python
from talk2view import AnonymousUnavailableError

try:
    user = t2v.auth.start_anonymous()                 # or start_anonymous(captcha_token=...)
except AnonymousUnavailableError as err:
    print(err.type)  # captcha_required | captcha_invalid | captcha_unavailable |
                     # anonymous_access_disabled | anonymous_daily_cap_reached

t2v.auth.is_anonymous()                               # True for a demo session

# Later: create the real account. From an anonymous session this converts it in
# place (same user id, history kept); the engine emails a confirmation link.
outcome = t2v.auth.signup("me@example.com", "password")
if outcome.confirmation_required:
    print("Check your email")
```

Or let the SDK do it: `Talk2View(..., anonymous_auto_start=True)` makes `chat()`,
`upload_attachment()`, `list_models()`, `completions()`, `voice.transcribe()` and
`voice.list_models()` start the demo session when nobody is signed in (raising
`AuthenticationError(type="sign_in_required")` when the engine refuses). `t2v.ensure_session()` does the same on demand — concurrent callers
share one request — and `t2v.on_anonymous_unavailable(cb)` tells the UI to show a
sign-in form instead. When the anonymous demo budget runs out mid-chat,
`ChatController.on("demoLimitReached", cb)` fires instead of an error.

`sign_in_required` is raised as an `AuthenticationError` here, where the
TypeScript SDK throws a plain `T2VError` with the same `type`. In code that
serves both, match on `err.type`, not on the class.

### `t2v.skills`

Skills are markdown knowledge documents the agent discovers (`list_skills`) and loads
(`load_skill`) on demand. Partner skills uploaded in the dashboard need no SDK code.
`t2v.skills` manages the **user** skills a host adds — from code or from folders of
`SKILL.md` files — and sends them to the engine on every session.

```python
from talk2view import Skill

t2v.skills.add(Skill(
    name="measure-volume",
    description="How to measure a segment's volume",
    content="# Measuring\n1. ...",
    allowed_tools=["measure_segment_volumes"],   # optional: gate the client tools
))
t2v.skills.load_directory("~/Talk2View/skills", label="user")  # SKILL.md files
t2v.skills.set_enabled("measure-volume", False)
t2v.skills.remove("measure-volume")
t2v.skills.get_all()          # every local Skill, enabled or not
t2v.skills.state()            # the SkillsState slice (also in ChatController snapshots)
t2v.skills.save(); t2v.skills.load()   # persist code-added skills in data_storage
t2v.skills.list_platform()    # partner/built-in catalogue, or None on an older engine
```

`load()` always sends the saved set with the next register call, even an empty one; a
host that stops using skills calls `skills.clear()` once.

Local edits are sent as ONE `POST /v1/skills/register` after 500 ms of quiet (the
route allows 10 calls a minute per signed-in user); `t2v.skills.register()` sends now,
and `chat()` sends a pending edit before each turn. Every call carries `replace: true`:
an engine that honours it makes the session's user skills exactly the enabled set, so a
removed or disabled skill is gone after the next call. An older engine ignores the flag
and merges, never removing a skill; there, removing or disabling a registered skill sets
`needsSessionReset`: a `ChatController` recycles the session as soon as it is idle
(history is kept), and a plain `chat()` does so before its next turn. On an engine that
honours `replace`, a replace that fails falls back to the same session recycle (see
**Compatibility** below for what an older engine cannot guarantee). A failed register
on a new session that may be one the engine kept falls back to it too
(`POST /v1/sessions` is get-or-create, so after a restart the SDK cannot know what
that session holds): `chat()` recycles once before the turn. A session created right
after this client's own confirmed `DELETE` is known to be empty, and a failed register
there only leaves the edit pending. `t2v.skills.replace_honoured()` tells which kind of
engine answered (None until the first call).

`replace: true` is last-writer-wins for the whole (partner key, account) session: two
apps signed in to the same account with the same partner key overwrite each other's user
skills, exactly as they do each other's client tools.

`list_platform()` (`GET /v1/skills`, 60 calls a minute) returns the partner and built-in
skills the session can see, one entry per name, with the engine's `source`; a local
skill with the same name is marked `overridesPlatform` in `state()`. It returns `None`
when the engine is older than that route (a 404: the catalogue is unavailable) or skills
are disabled for the session.

Limits mirror the engine's units: a name is 1..255 Unicode code points, a body is
non-empty, and the whole request is under 8 MB of JSON **bytes** —
`SkillValidationError` is raised before any request. A skill over ~100k characters is
accepted with a warning.

**On-disk format.** `load_directory(dir)` reads `<dir>/<skill>/SKILL.md` and
`<dir>/<skill>.md`, parsed exactly as the engine and the dashboard parse them:

```markdown
---
name: measure-volume
description: How to measure a segment's volume
allowed-tools: measure_segment_volumes set_segment_display
---
# Measuring a volume
...
```

The frontmatter is line-wise `key: value` (no YAML lists or multi-line values — they
are warned about and ignored); `allowed-tools` is space- or comma-separated; `name`
defaults to the folder or file stem and must be a safe file name; only `SKILL.md` is
sent (sibling files are ignored with a warning). By name, a skill added from code wins
over a directory, and a directory loaded later wins over one loaded earlier. Calling
`load_directory` again with the same `label` rescans it.

A symlink inside a skills directory that resolves outside it, and a skill file over the
8 MB request cap, are both skipped with a warning rather than sent to the engine.

**Compatibility.** Removing or disabling a skill without a session recycle needs an
engine that honours `replace` (the engine's skills routes). On an older engine the SDK
falls back to recycling the session, and a skill removed while the app was closed can
survive a restart until the next recycle — so reliable skill removal with this SDK
version requires a Talk2View engine with skills `replace` support.

`load_directory()` trusts the people who can write to the directory: a symlink that
resolves outside it is refused, but each file is checked and then read, so a file
swapped in between is read as it is then. Load directories the end-user controls.

### `t2v.tools`

Tool registration.

```python
# Register @tool-decorated functions (extracts schema + registers handler)
t2v.tools.register_from_functions([my_tool, another_tool])

# Or register schema and handler separately
from talk2view import ClientToolSchema
t2v.tools.register([ClientToolSchema(name="my_tool", description="...")])
t2v.tools.handle("my_tool", my_handler_function)
```

Tools are session-scoped on the engine. `create_session()` re-sends the
registered schemas, so a new session — including one created by recovery —
keeps your tools.

Arguments are validated against the registered schema before a handler runs:
`None` and empty-object placeholders are stripped, string enums are matched
case-insensitively and normalised to the schema's casing, and a type mismatch
is returned to the model as an error result rather than reaching your code.

#### Tool permissions (fail closed)

Gate a tool on the end-user, or decide in code:

```python
from talk2view import PermissionResult, tool

@tool(permission=True)           # every call pauses for the end-user
def delete_volume(name: str) -> str:
    """Delete a volume from the scene."""
    ...

def policy(tool_name: str, args: dict) -> PermissionResult:
    if args["path"].startswith("/etc"):
        return PermissionResult.deny("system files are off limits")
    return PermissionResult.allow({**args, "read_only": True})  # may rewrite the input

t2v.tools.handle("open_file", open_file, permission=policy)
```

Permission checks **fail closed**: a callback that raises, that returns
anything other than a `PermissionResult`, or that yields an action the SDK does
not recognise, denies the call and tells the model — the handler does not run.
A `permission=` that is not `None`, a `bool`, or a callable raises `TypeError`
at registration, and re-registering a tool without `permission=` drops the
permission it had (logged as a warning).

### Human-in-the-loop approvals

`permission=True` (a client tool) and the engine's own approval of a platform
tool both pause the stream with an `approval_required` event. Answer it with
`respond_to_approval()`, which continues the turn:

```python
from talk2view import Decision

for event in t2v.chat("Delete the CT volume"):
    if event.type == "approval_required":
        print(event.description)          # the tool's description, or the engine's summary
        for row in event.details or []:   # engine approvals only
            print(f"  {row['label']}: {row['value']}")

        decision = Decision("once")       # "once" | "always" | "deny"
        # Decision("deny", feedback="Hide it instead.")
        # Decision("once", updated_input={"name": "CT_copy"})
        for resumed in t2v.respond_to_approval(event, decision):
            ...
        break
```

`event.kind` is `"client"` (your handler runs locally on approval) or
`"engine"` (the platform runs the tool; `updated_input` and `feedback` do not
apply). Anything other than `"once"`/`"always"` is treated as a denial.
Engine approvals need `Capabilities(approval=True)`.

### `Capabilities`

Opt-in engine features. All off by default, and nothing is added to the request
body for a flag that is off:

```python
from talk2view import Capabilities, Talk2View

t2v = Talk2View(
    partner_key="pk_live_...",
    capabilities=Capabilities(
        approval=True,           # engine-enforced approval of platform tools
        tool_events=True,        # platform-tool lifecycle (tool_event events)
        reasoning=True,          # the model's reasoning (reasoning events)
        image_generation=True,   # generated-image tool events
        timezone="Australia/Perth",
        surface="app",           # "keyboard" | "app" — the engine rejects anything else
    ),
)
```

`memory` and `memory_search` (the user's on-device memory, and the agent's `search_memory`
tool) are set for you by `Talk2View(memory_path=...)`; see
[On-device memory](#on-device-memory-memory_path).

### Running tool handlers on the host's main thread

Desktop hosts (3D Slicer, FreeCAD, Blender) only allow scene and UI access from
their main thread, while you will want to stream on a worker thread so the UI
stays responsive. A **dispatcher** bridges the two: handlers and permission
callbacks are posted to the host's main thread and the streaming thread waits
for the result. The SDK never imports a UI toolkit — you hand in the host's
timer class.

```python
import qt                                  # 3D Slicer; PySide6.QtCore in FreeCAD
from talk2view import Talk2View, qt_dispatcher

t2v = Talk2View(partner_key="pk_live_...", dispatcher=qt_dispatcher(qt.QTimer))
```

```python
import bpy
from talk2view import bpy_dispatcher

t2v = Talk2View(partner_key="pk_live_...", dispatcher=bpy_dispatcher(bpy.app.timers))
```

Construct the dispatcher **on the host's main thread**. For any other host, use
`QueueDispatcher` and drain it from your own main loop with `pump()`, or
`TimerPumpDispatcher(start_timer)` with a timer of your own. The default is
`InlineDispatcher`: handlers run on the calling thread, exactly as they always
have.

A `stop()` while a handler is still queued raises `Cancelled` and the handler
never runs. A handler that has already started is never interrupted — the SDK
waits for it and uses its result.

**Listener callbacks are not auto-marshalled.** `on_auth_state_change`,
`on_anonymous_unavailable` and `t2v.skills.on_change` can run on any SDK-owned
thread — a `ChatController` worker, the `t2v-warm-up` daemon, a skills debounce
timer, the `talk2view-memory` worker — not just the thread that registered them. Unlike tool handlers and
`deliver_on_main` subscribers, these do not go through the dispatcher
automatically: do any UI work from inside them with `run_on_main(dispatcher, fn)`.

### `ChatController`

`t2v.chat()` gives you a stream of events and leaves the conversation to you.
`ChatController` owns the conversation instead: the display messages, the
streaming assistant reply with its tool steps and plan, the pending approval,
the error, the LLM history that is replayed on the next turn, and the one run
in flight. A UI is a view over it and never a second source of truth — it reads
the state, follows the changes, and drives the controller with commands.

```python
import qt                                  # 3D Slicer's Qt module
from talk2view import Capabilities, ChatController, Talk2View, qt_dispatcher

t2v = Talk2View(partner_key="pk_live_...", dispatcher=qt_dispatcher(qt.QTimer))
t2v.tools.register_from_functions([get_scene_summary, load_sample])

controller = ChatController(
    t2v,
    system_prompt="You are a 3D Slicer assistant.",   # sent with every turn
    model="gpt-4.1-mini",                              # bound to the session
    capabilities=Capabilities(approval=True),          # bound to the session
)
```

**One controller per `Talk2View`.** It holds that client's chat session, its
LLM history and the single run in flight, so a second controller on the same
client would fight it for all three. `model=` and `capabilities=` are handed to
`t2v.set_chat_defaults()`; `dispatcher=` defaults to the one the tools use
(`t2v.tools.dispatcher`), so tool handlers and main-thread callbacks share a
thread; `executor=` is where a turn streams — by default a daemon thread named
`t2v-chat` per run.

Close the `ChatController` before closing the `Talk2View`: the controller
subscribes to `t2v.skills` and `t2v.auth`, and one dropped without `close()`
stays subscribed — it keeps receiving skills changes and may still recycle the
session.

**The conversation belongs to one end-user.** The controller follows
`t2v.auth` (its `isAnonymous` too), hearing every change before any host
listener, so a listener of yours that blocks or signs in again cannot get ahead
of it:

| Auth change | Approval consent (`alwaysAllowedTools`) | Conversation |
|---|---|---|
| Same user id (a convert, a repeated sign-in) | kept | kept |
| Signed out → signed in (including the anonymous start a `send()` makes) | reset | kept |
| Anonymous demo → account (`signup()`'s convert, or its sign-in to an existing account) | reset | kept |
| Sign-out: `logout()`, or a refresh token the engine rejected | reset | dropped |
| One anonymous session → another, account → anonymous, account → another account | reset | dropped |

"Dropped" is local, like `clear()` but with no engine call (the old token is
gone): the run in flight is abandoned, and the messages, the history the next
turn would replay, a pending approval and `threadId` are emptied. A kept
conversation is replayed into the new identity's session on its next turn —
the demo-to-signup path keeps the demo's chat. No identity ever inherits
another's "always allow". The send that triggers an anonymous start carries on.

#### State: `snapshot()` and `subscribe()`

State reaches a view in two halves of one protocol. `snapshot()` returns the
whole state as a `ChatSnapshot` dict (camelCase keys, the same shape as the
TypeScript SDK's `DisplayMessage`), stamped with the `seq` of the batch it was
cut on. `subscribe(on_batch)` then delivers every change as an `OpBatch` —
`{"seq": n, "ops": [...]}` — in `seq` order, and `apply_batch()` folds one onto
a snapshot:

```python
from talk2view import SeqGapError, apply_batch

dispatcher = t2v.tools.dispatcher   # the qt_dispatcher above; the controller posts through it
state = None                        # the view's copy; None while it waits for a snapshot

def resync():
    global state
    state = controller.snapshot()   # called outside any delivery, so a replay base
    render(state)

def on_batch(batch):                # deliver_on_main=True: runs on the host's main thread
    global state
    if state is None or batch["seq"] <= state["seq"]:
        return                      # waiting for a resync, or older than the snapshot
    try:
        state = apply_batch(state, batch)
    except SeqGapError:
        state = None                # a gap: resync from a fresh snapshot, but not in here
        dispatcher.post(resync)     # queued: runs on main once this callback has returned
        return
    render(state)

unsubscribe = controller.subscribe(on_batch, deliver_on_main=True)
resync()                            # the first snapshot, taken AFTER subscribing
```

The joining rules, in full: subscribe first, then take the snapshot; drop
batches whose `seq` is not greater than the snapshot's; on a gap
(`seq != last + 1`, which `apply_batch` raises as `SeqGapError`) take a fresh
snapshot **from outside the callback**. `snapshot()` is cut on a batch
boundary, so replaying every batch with a greater `seq` onto it reproduces the
live state exactly — except when it is called from inside a delivery on the
emitting thread, where it is a plain copy (see the callback rules below).
Rebasing on that copy can apply a streaming delta twice, and the view never
heals. So the gap branch above only drops the view and posts `resync`: with
`qt_dispatcher`, `bpy_dispatcher` or a `QueueDispatcher` the post is queued
and runs on the host's main thread once the callback has returned. Under the
default `InlineDispatcher` a post runs at once, on the emitting thread, and
defers nothing. A host with no timer-driven dispatcher gives the controller a
queue of its own instead: create a `QueueDispatcher()` on the host's thread
(only the thread that creates it may `pump()` it), pass it as
`ChatController(t2v, dispatcher=queue)`, use it as `dispatcher` in the example
above, and call `queue.pump()` from the host's loop. Batches and the resync
then run on that one thread, in order, and the example works as written.

Batches are shared between subscribers: treat them as read-only.
`apply_batch` never mutates its arguments and returns a new snapshot.

The op format is two ops: `set` at a path (a `set` at index `len` of a list
appends), and `append-text` for a streaming delta. Any structural shrink
(retry, edit, clear, dropping an empty segment) is one `set` of the whole
`messages` list. A page derives `isRunning = isLoading || pendingApproval !== null`.

#### Commands

Every method is safe to call from any thread and none of them blocks on the
run.

| Call | What it does | Raises |
|---|---|---|
| `send(text, attachments=None)` | Starts a turn; returns as soon as the job is on the executor. | `ControllerBusyError` (a run is active or an approval is pending), `ControllerClosedError`, and whatever the executor or the dispatcher raises while the first batch is handed over — re-raised after the run is undone, so the controller is usable, not busy for good. |
| `approve(decision, *, tool_call_id=None)` | Answers the pending approval and resumes on a new run. A no-op when nothing is pending, or when `tool_call_id` names a different call (a stale click). `Decision("always")` is remembered for that client tool (`alwaysAllowedTools`) — never for an engine approval. | `ControllerClosedError`, and — like `send()` — whatever the executor or the dispatcher raises while the first batch is handed over, re-raised after the run is undone. |
| `cancel()` | Stops the stream, keeping the partial reply and setting no error. With an approval pending there is nothing to stop, so the call is denied with `CANCELLED_APPROVAL_FEEDBACK` (`"Cancelled by the user"`) — only the card pending when Stop was pressed, never a newer one. | Never raises. A denial the executor or the dispatcher refuses to launch is logged and surfaces as `error`, and the controller is left idle. |
| `retry()` | Removes the last user turn and everything after it, then sends it again. A no-op with nothing to retry. | `ControllerBusyError`, `ControllerClosedError` |
| `edit(message_id, text)` | Drops that user message and everything after it, then sends `text`. | `ValueError` (no *user* message has that id), `ControllerBusyError`, `ControllerClosedError` |
| `clear()` | Resets the conversation and deletes the engine session on the executor. Allowed while busy: a run in flight is abandoned and a pending approval dropped. `isLoading` stays `True` until the DELETE returns. | `ControllerClosedError` |
| `clear_error()` | Sets `error` back to `None`. | — |
| `wait_idle(timeout=None)` | Blocks until no run is active. A pending approval counts as idle: no thread waits on it. Idle can be reported while that approval's paused response is still being closed — the run is over, but a `t2v-chat` thread may take a moment more to unwind. | — |
| `close()` | Abandons any run (its cancel token is cancelled), drops subscribers and listeners. Does not close the `Talk2View`. | — |
| `on("demoLimitReached", cb)` | A one-shot notice, not chat state: the anonymous demo budget ran out (`DEMO_LIMIT_REACHED`). Returns an unsubscribe. | — |

An approval never parks a thread: the worker returns with `pendingApproval`
set, `isLoading` `False` and the assistant message still `isStreaming` (so the
card renders on it), and `approve()` starts the resume as a new run. A turn
that produced no assistant text because of an `error` event, a stream that
raised, or a cancel is rolled out of the LLM history, so the next `send` does
not replay `[..., user, user]`.

#### Callback rules

`subscribe(on_batch, *, deliver_on_main=False)`:

- **`deliver_on_main=False` (the default)** runs the callback on the emitting
  thread — usually the worker — inside the delivery window every other emitter
  waits on. It must not block waiting on the host's main thread (`run_on_main`,
  a blocking Qt connection, a queue only the UI thread drains): that thread may
  be inside `send()` waiting for this very delivery, and neither side could then
  move. Enqueue and return.
- **`deliver_on_main=True`** posts the callback through the dispatcher and never
  waits on it, so it runs on the host's main thread — the right place for
  anything that touches the UI. A host that stops pumping stalls its own
  batches and nothing else; a batch may still be queued when `wait_idle()`
  returns. Under the default `InlineDispatcher` a post runs right there on the
  emitting thread, so the rules above apply to it as well.
- **No callback may call `wait_idle()`.** On the emitting thread the run can
  only go idle once the callback returns; on the main thread it stalls the pump
  the worker's tool handlers need.
- A callback may read `snapshot()` and call back in (`approve()`,
  `clear_error()`, …): the ops that produces arrive in a later batch, never out
  of order. A `snapshot()` taken from inside an emitting-thread callback is a
  plain copy, not a replay base: never rebase a view on it (the State example
  above resyncs from outside the callback).
- `on(...)` listeners always run on the emitting thread, just before the batch
  reaches the subscribers, so the same rules apply to them.
- A subscriber that raises is logged and the run continues; the other
  subscribers still get the batch.

#### Wiring it to a host

The dispatcher is the bridge to the host's main thread for both tool handlers
and `deliver_on_main` subscribers. Construct it on that thread:

```python
# 3D Slicer / FreeCAD (Qt)
import qt                                  # PySide6.QtCore in FreeCAD
from talk2view import ChatController, Talk2View, qt_dispatcher

t2v = Talk2View(partner_key="pk_live_...", dispatcher=qt_dispatcher(qt.QTimer))
controller = ChatController(t2v)
controller.subscribe(update_chat_widget, deliver_on_main=True)   # runs on Qt's thread
# `text` is a property in Slicer's PythonQt and a method in FreeCAD's PySide:
send_button.clicked.connect(lambda: controller.send(input_box.text))      # 3D Slicer
# send_button.clicked.connect(lambda: controller.send(input_box.text()))  # FreeCAD
stop_button.clicked.connect(controller.cancel)
```

Disable Send while `isLoading` is `True` or `pendingApproval` is set, or catch
`ControllerBusyError` — that is the state in which `send()` refuses.

```python
# Blender
import bpy
from talk2view import ChatController, Talk2View, bpy_dispatcher

t2v = Talk2View(partner_key="pk_live_...", dispatcher=bpy_dispatcher(bpy.app.timers))
controller = ChatController(t2v)
controller.subscribe(redraw_panel, deliver_on_main=True)
```

For any other host, use `QueueDispatcher` and drain it from your own loop with
`pump()`, or `TimerPumpDispatcher(start_timer)` with a timer of your own.

The controller is the model behind the SDK's own chat UI: the localhost UI
server (`talk2view.ui`, below) serves the bundled chat page and forwards the
controller's state over SSE, so a host that embeds that page never touches
this API directly. A host with its own widgets uses it as above.

### Chat UI server (`talk2view.ui`)

`talk2view.ui` serves the SDK's chat page to a web view (3D Slicer's
`qSlicerWebWidget`, a `QWebEngineView`, or the system browser) from
`http://127.0.0.1:<port>`, using only the standard library. Python owns every
piece of state; the page is a view that reads it as SSE op batches and sends
commands back. Nothing in `talk2view` imports it: it is opt-in.

```python
from talk2view import ChatController, Talk2View
from talk2view.ui import GenericHost, UIServer

host = GenericHost()                         # build it on the thread that will pump it
t2v = Talk2View(partner_key="pk_live_...", dispatcher=host)
controller = ChatController(t2v)
server = UIServer(controller, host)
server.start()                               # http://127.0.0.1:<port>/#t=<token>
server.show()                                # posted: the browser opens as the host pumps
host.run_forever()                           # tool handlers run here; Ctrl+C to quit
server.stop(); controller.close(); t2v.close()
```

A host with a toolkit writes its own `HostAdapter` (`post`, `is_main_thread`,
`show`, `open_external`, `clipboard_set`, `theme`, `storage`, `audio`, plus
`supports_clipboard = True` when its clipboard works): 3D Slicer's adapter
shows the URL in a `qSlicerWebWidget` and drains `post()` with a `qt.QTimer`.
`server.set_theme(Theme(mode="dark", tokens={"--t2v-bg": "#021C25"}))`
restyles the page live. `UIServer(..., on_skills_list=rescan)` lets the host
rescan its skill folders when the page opens the skills list.

**Options.** `ChatOptions` are the TypeScript `<Talk2ViewChat>` props as plain
data:

```python
from talk2view.ui import ChatFeatures, ChatOptions, ChatWelcome, ToolPresentation

options = ChatOptions(
    welcome=ChatWelcome(heading="Ask about your scene", suggestions=["Load MRHead"]),
    features=ChatFeatures(attachments=False),       # enforced: no uploads
    allow_anonymous=False,                          # enforced: no guest sessions
    reset_password_url="https://example.com/reset",
    footer="Answers can be wrong. Check them before you act.",
    tools={"load_file": ToolPresentation(activity="Loading a file", destructive=True)},
)
server = UIServer(controller, host, options=options)
```

They are checked when built (`TypeError`, `ValueError`) and served to the page
read-only. `ToolPresentation` gives a tool's running label, and marks the tools
whose calls change the end-user's work, which the approval card warns about.
The server enforces the options that fail closed. With attachments off it
refuses an upload, and a `send` that names one. With
`ChatFeatures(provider_sign_in=False)` it refuses `oauthStart`, and the page
hides "Continue with Apple / Google". With `allow_anonymous=False`
it refuses `startAnonymous`, and `UIServer` itself refuses to start at all: it
raises `ValueError` at construction if any `Talk2View` in play — the
controller's, or the one inside `app=` — was built with
`anonymous_auto_start=True`, since a `send` would otherwise start a guest
session behind the page's sign-in gate. `UIServer` also raises `ValueError` if
`app=` wraps a different `Talk2View` than the controller's own (a controller
with none, such as `FakeController`, plus an `app=` over a real `Talk2View` is
fine). A host that passes its own `app=AppState(...)` gives the options to
that `AppState` instead.

**Guests.** The page's `limits` slice says when a guest was turned away: the
demo allowance ran out, or the engine refused a guest session (with its
reason). The page then shows sign-in in place of the thread. The **engine's**
demo budget is the real limit — the SDK does not count anonymous turns itself,
it only relays what the engine decided — so both limits lift not just on a
real sign-in but on a sign-out, on a new guest session starting, and on any
other change of identity (the TypeScript SDK instead keeps the gate for the
rest of the page's life). The page then sends the stopped turn again with
`reload`. "Default" in the model picker returns to the partner's default
model.

The page's "new chat" (`newSession`) runs `controller.clear()`, which deletes
the engine session. The engine keeps one session per account and partner key,
so that is also the session of any other Talk2View app the user is signed in
to with the same partner key.

**Security.** The server binds `127.0.0.1` on an ephemeral port and admits
a request only from a loopback peer, with its own `Host`, and with its own
`Origin` when one is sent. Every API route (`/events`, `/command`,
`/upload`) needs the per-launch token, which travels in the URL fragment (the
browser never sends a fragment) and then in an `Authorization: Bearer`
header. A preflight is never answered and no `Access-Control-*` header is ever
sent, so no other page on the machine can drive it. The request log (logger
`talk2view.ui`, DEBUG) records paths and statuses only, never a query string.
`GenericHost` hands the tokened URL to the system browser, whose command line
other local users on a multi-user machine can read from the process list, so
a host that cares shows the page in an embedded web view instead.

**Threads.** HTTP threads never call the controller, `Talk2View` or host
code. Commands run in order on one router thread, uploads on one upload
thread, and account refreshes (config, models) on one refresh thread. The
controller's batches reach each page through a bounded queue: a page that
falls behind a streaming reply gets the backlog as one event, and one that
falls too far behind gets a fresh snapshot. Those threads (`t2v-ui-router`,
`t2v-ui-refresh`, `t2v-ui-upload`) sign in and out (a refresh token the
engine rejects signs out too), start guest sessions and change preferences,
so the host's own `t2v.auth.on_auth_state_change` and
`t2v.preferences.on_change` listeners can run on them too: a toolkit host
marshals their UI work to its main thread.

**The page.** The chat page is built from `embed/` and shipped in
the wheel. A git checkout does not have it until it is built: `UIServer`
then raises `StaticAssetsMissingError`, whose message says how to build it,
or pass `static_dir=` a built bundle. To try a built page without an engine,
run `python -m talk2view.ui.testing --static-dir <dist>`, which serves it
against a scripted `FakeController`.

It is the TS SDK's assistant-ui chat, rebuilt for Chromium 83 (3D Slicer's
web view on Windows and Linux): streamed Markdown replies, tool steps named
by `ToolPresentation.activity`, the approval card (allow once, always allow,
deny with a reason, edited arguments, the destructive warning; allow or deny
only for an engine approval), the error card with Try again, sign-in,
sign-up and sign-out behind the guest gate, the model picker with Default,
text size, skills, attachments, and New chat. Links open with the host's
`open_external`, and Copy uses `clipboard_set` when the host declares
`supports_clipboard`.

**Continue with Apple / Google.** The Account view offers them while the
partner has desktop sign-in turned on (the page asks the engine at startup and
on every sign-in change) and `ChatFeatures(provider_sign_in=True)`, the
default. The sign-in runs `t2v.auth.sign_in_with_provider` on its own thread,
and the browser opens through the host's `open_external` on its main thread;
a host whose `open_external` returns `False` (as `GenericHost` does when
`webbrowser.open` finds no browser) or raises gets "the browser could not be
opened" at once. While the browser is out the page shows a Cancel button, and
signing in with email, signing up or signing out cancels it too.

Not yet available in the chat page: dictation, anonymous sessions on a partner key
that requires a captcha, generated images (the page names the file; the
bytes need a route), editing a sent message, and a list of earlier chats.

### `@tool` Decorator

Generates a JSON Schema from your function's type hints and Google-style docstring.

```python
from talk2view import tool

@tool
def search_nodes(query: str, node_type: str = "vtkMRMLVolumeNode") -> str:
    """Search the scene for matching nodes.

    Args:
        query: Search term to match against node names.
        node_type: MRML node class to filter by.
    """
    # Implementation
    return json.dumps(results)
```

The decorator:
- Maps Python types to JSON Schema (`str` -> `"string"`, `int` -> `"integer"`, `float` -> `"number"`, `bool` -> `"boolean"`, `list[str]` -> `{"type": "array", "items": {"type": "string"}}`)
- Parses the `Args:` section for parameter descriptions
- Uses the first paragraph of the docstring as the tool description
- Parameters without defaults are marked as `required`
- Works with both regular functions and bound methods (`tool(self.method)`)

### `TokenStorage`

Protocol for persisting auth tokens. Implement this to keep users logged in across app restarts.

```python
from talk2view import TokenStorage

class MyStorage:
    """Must implement get, set, delete."""

    def get(self, key: str) -> str | None:
        ...

    def set(self, key: str, value: str) -> None:
        ...

    def delete(self, key: str) -> None:
        ...

t2v = Talk2View(partner_key="...", storage=MyStorage())
```

### `FileStorage`

A `TokenStorage` that keeps one JSON object in a file only the user can read
(`0600`; a directory `FileStorage` has to create is made `0700`, and an existing one is
left as it is; on Windows the permission calls are skipped and the file inherits the
profile's ACL). Writes are atomic — temp file, `fsync`, `os.replace` — and
thread-safe. Two `FileStorage` instances on the same path in one process share the same
lock and cache; two separate *processes* writing the same file still overwrite each
other's keys, so give each process its own path.

**One process per token file.** A `FileStorage` reads its file once and then
trusts its memory. So a sign-out in one process is not seen by another process
using the same file, a later write there can bring the cleared tokens back, and
both would spend the same rotating refresh token. The TypeScript SDK's
cross-tab sign-out has no desktop counterpart yet.

A file that exists but cannot be read right now (a permission error, a Windows sharing
violation while an antivirus scans it) makes `get`/`set`/`delete` raise that `OSError`,
and the next call reads the file again: it is never taken for an empty store, so a
write can never replace data that could not be read. A file that is read but is not a
JSON object of strings (invalid JSON, not UTF-8, another JSON type, non-string values)
starts the store empty — keeping any string values — and is moved aside to
`<name>.corrupt-<timestamp>` before the next write replaces it (best effort; the new
name is logged).

```python
from talk2view import FileStorage

t2v = Talk2View(
    partner_key="...",
    storage=FileStorage("~/.config/myapp/talk2view/auth.json"),
    data_storage=FileStorage("~/.config/myapp/talk2view/data.json"),
)
```

`data_storage` holds skills and preferences and defaults to `storage`. Give a
keyring-backed `storage` its own `data_storage`: a keyring must never hold skill bodies.

### `t2v.preferences`

The end-user's UI choices (`model`, `stt_model`, `stt_language`, `font_size`), stored
in `data_storage` under the same key and JSON shape as the TypeScript SDK's
`useUserPreferences`.

```python
prefs = t2v.preferences.get()                      # UserPreferences (None = not set)
t2v.preferences.update(model="gemini-3.8-flash", font_size="large")
t2v.preferences.update(model=None)                 # unset one
t2v.preferences.on_change(lambda p: print(p))
```

Every value is a string or `None`: anything else raises `TypeError`, and a `font_size`
other than `small`/`medium`/`large` raises `ValueError`, before anything is stored.

### On-device memory (`memory_path`)

With `memory_path`, the assistant remembers the end-user across conversations: who they
are, what they prefer, what they are working on, and what was said in earlier chats. The
memory lives only on the user's device, in a SQLite file under `memory_path`. Talk2View's
engine keeps no memory content: it reads what a call carries while the call runs, and
answers with changes for your app to apply. There is no sync in this version, so another
device, or another `memory_path`, has a memory of its own. Memory must also be turned on
for your partner. Until it is, the engine answers the memory calls with
`403 memory_disabled`, ignores each turn's `memory` field and gives the agent no memory
tools, and the SDK goes on recording the log of the chats. Without `memory_path` (the
default) memory is off and nothing changes.

```python
from pathlib import Path
from talk2view import Talk2View

app_dir = Path.home() / ".config" / "myapp"        # a directory your app has made already
t2v = Talk2View(partner_key="pk_live_...", memory_path=app_dir / "talk2view-memory")
t2v.auth.login("user@example.com", "password")

for event in t2v.chat("Remember that I prefer metric units"):
    ...                      # memory is wired into chat(): there is nothing else to call

memory = t2v.memory          # the signed-in user's T2VMemory
```

`memory_path` is a directory (a `str` or any path-like). Each user, a guest included, keeps
a file of their own in it, so accounts that share a device never see each other's memory.
`t2v.memory` is the signed-in user's memory, a `T2VMemory`, opened on first use. It is
`None` without `memory_path`, with nobody signed in, and after `close()`. Signing out, or
in as someone else, closes the previous user's memory at once: its queued engine calls are
dropped, and one already in flight is not waited for. Reading `t2v.memory` raises when the
file cannot be opened; `chat()` then logs one line and goes on without memory for that turn.

The memory types are in `talk2view.memory`, not at the top level: `T2VMemory`
(`t2v.memory`), `MemoryStore` (`t2v.memory.store`, the user's file), and the records they
return, `MemoryEntry`, `Fact`, `SearchHit` and `AppliedBatch`.

#### What it keeps

The file holds three things. Every character count in this section, caps and limits
included, is in Unicode code points, not bytes or graphemes.

- **Entries**: short statements about the user, each with a topic (`identity`, `work`,
  `people`, `preferences`, `ongoing` or `instructions`) and, optionally, a `valid_until`
  date. Every turn sends the entries not past their date, up to 40 of them and 6,000
  characters in all. The agent adds and changes entries when the user says "remember..."
  or "forget...", and extraction does too.
- **Facts**: dated statements that extraction draws from conversations. A fact that
  changes is superseded, not deleted. Search finds facts; a turn never sends them all.
- **The log of the chats**: each round (what the user said, and the reply) of every
  conversation that is not incognito, with a summary once extraction has read it. Search
  runs over the facts and these rounds.

#### Each turn, with no code from you

`memory_path` sets the `memory` and `memory_search` flags of `Capabilities` on top of the
`capabilities` you pass, and keeps them across `set_chat_defaults`. Then every `chat()`:

1. calls `run_due()`, which only queues work (see Triggers);
2. sends the turn's `memory` field with the message: `status: "on"`, the user's `entries`,
   the `recent` conversations and the `recalled` passages from older ones that match the
   message (Privacy, below, lists what goes). The same field rides every `/resume` of the
   turn, and the resend after a lost session;
3. answers the agent's `search_memory` calls from the store (the tool is the engine's own,
   so you register nothing); a malformed argument gets a short `{"error": ...}` result;
4. applies each `memory` event, the agent's changes to the user's entries (`add`,
   `update`, `remove` or `forget`), before passing the event on to you, so a UI only has to
   show it (see "Showing a change, and undoing it");
5. records the round in the store's log when the turn is `done`. A turn that ended in an
   error, was stopped, or applied a `forget` or `remove` records none: a forget's own words
   would name what was forgotten.

Set `Capabilities(timezone=...)`: memory reads the user's day from it (when an entry
expires, the date shown on a fact) and falls back to the system's zone.

#### Showing a change, and undoing it

A `memory` event reaches you after the SDK has applied it, and `t2v.memory.last_applied`
is that event's `AppliedBatch`: `applied` lists the changes that took effect, `dropped`
names the rules that refused the others. Read it as the event arrives, because the next
`memory` event replaces it and each turn starts with `None`. The batch can be taken back
for 30 seconds:

```python
for event in t2v.chat("Remember that I prefer metric units"):
    if event.type == "memory":
        batch = t2v.memory.last_applied                    # None: nothing was applied
        if batch is not None and batch.applied:
            show_memory_updated(batch.id, batch.applied)   # your UI, with an Undo button

# When the user taps Undo: True if taken back. False, and never an exception, after the
# 30 seconds, for a batch undone already or overtaken by a later change, and once closed.
undone = t2v.memory.undo(batch_id)
```

#### Triggers: when memory learns

The engine turns conversations into facts, entry changes and a summary (extraction,
`POST /v1/memory/extract`) and tidies the entries (consolidation,
`POST /v1/memory/consolidate`). The SDK makes both calls in the background (you can also
call them directly, see below), and nothing runs on a timer unless you start one:

| Trigger | What it queues |
|---|---|
| `chat()`, before every turn | `run_due()`: the purge of removed or forgotten items past their 30 s undo window, extraction of each conversation with unread rounds that has been idle for 10 minutes, and consolidation when it is due |
| The first `run_due()` after the memory opens | Extraction of every conversation with unread rounds, idle or not, so a chat cut short by quitting is not lost |
| `t2v.clear_session()` | A new conversation (`t2v.memory.conversation_id` changes; `ChatController.clear()` calls it) and extraction of the one that ended |
| `t2v.memory.start_background(interval_s=60.0)` | `run_due()` every `interval_s` seconds on a daemon thread, until `t2v.memory.stop_background()` or the memory closes (a sign-out, or another user signing in: start it again for the next user). For a host that can sit idle after the last message, where no `chat()` would extract that conversation |

Consolidation is due when the entries are over their caps (more than 40, or more than
6,000 characters), when 10 changes have built up and 7 days have passed since the last
one (or since the file was created), when the last one could not take every entry, and
right after an import. One that has been applied is not repeated until memory has
changed. `run_due()` never waits and never raises; it returns the keys it queued
(`"extract:<conversation id>"`, `"consolidate"`). `t2v.memory.wait_idle(timeout)` waits
for the queue, for tests and shutdown. A conversation still unextracted when the app
quits is extracted at the next launch.

#### Incognito, paused, and guests

An **incognito** conversation uses no memory and adds none. Mark it before its first
message:

```python
t2v.clear_session()              # a new conversation...
t2v.memory.incognito = True      # ...marked incognito before its first message
```

`t2v.memory.new_conversation(incognito=True)` starts the memory's conversation incognito in
one step (it does not clear the engine session, as `clear_session()` does). An incognito
turn sends only `{"status": "incognito"}`, applies no `memory` events, records no rounds
and finds nothing when the agent searches, and extraction never reads the conversation.
`clear_session()` ends it: the next conversation is not incognito. Setting `incognito`
after the first message deletes the rounds recorded so far.

**Paused** is the user's switch for all memory, kept in the file
(`t2v.memory.store.paused = True`). A paused turn sends only `{"status": "paused"}`, applies
and records nothing and finds nothing, and no extraction, consolidation or import goes out;
only the purge still runs.

A **guest** (an anonymous demo session) has a memory of their own, and the turn field,
`memory` events, search and rounds all work. Automatic memory needs an account (the engine
answers a guest's memory calls with `403 memory_requires_account`), so the SDK makes no
extraction, consolidation or import call for a guest. A guest who converts to an account
keeps the user id, so the file carries over and their conversations are extracted then.

#### What the user controls

`t2v.memory.store` is the user's `MemoryStore`. A memory screen is built over it:

```python
store = t2v.memory.store

entries = store.entries()                  # MemoryEntry: id, topic, text, valid_until, source, locked, ...
facts = store.facts()                      # Fact (include_superseded=True adds the replaced ones)
hits = store.search("kayaking", limit=5)   # SearchHit: kind, date, conversation, text, score, ref

entry_id = entries[0].id
store.edit_entry(entry_id, text="Prefers metric units")   # locks it: the engine's changes never touch it
batch_id = store.delete_entry(entry_id)    # hidden for 30 seconds...
store.undo(batch_id)                       # ...so the user can take it back
store.forget("Sam")                        # hides every unlocked entry and every fact that holds the phrase

store.paused = True                        # the pause switch
store.include_sensitive = True             # lets an import keep sensitive topics (off by default)

markdown = store.export_markdown(timezone="Australia/Perth")   # or store.export_json()
ok = t2v.memory.import_text(pasted_text)   # memory the user exported from another assistant
store.reset()                              # forget everything
```

- **Edit and delete.** `edit_entry` raises `KeyError` for an entry that is not there, and
  `ValueError` naming the rule for text over 300 characters, a topic outside the six, or a
  date that is not `YYYY-MM-DD`. A locked entry is passed by `forget` too, and by the
  agent's `update` and `remove`; `delete_entry`, the user's own delete, removes it all the
  same. It returns the batch id for `undo`, or `None` for an entry that is not there.
- **Forget.** `forget(phrase)` matches whole words ("Sam" finds "Sam's", never "Samsung"),
  also clears the phrase from the log of the chats, and returns an `AppliedBatch`; its
  `id` is for `undo`. A removed or forgotten item stays hidden for 30 seconds, so `undo`
  can bring it back, then the next purge (every turn runs one) deletes it.
  `store.delete_conversation(conversation_id)` deletes one chat's log; the facts and
  entries drawn from it stay until the user deletes them.
- **Export** holds every entry and fact (replaced facts too, marked), not the log of the
  chats, and nothing removed or forgotten.
- **Import.** `import_text` sends the text to extraction in import mode and keeps what comes
  back as imported, then queues a consolidation. It returns `True` when applied, and `False`
  when the call failed or was refused (a guest, memory off for the partner, memory paused),
  for text that is blank or over 200,000 characters, and when something was forgotten,
  deleted, undone or reset while the call ran: its result could bring that back, so it is
  discarded, and the import can be tried again. `import_text`, `extract` and `consolidate`
  wait for the engine, up to 180 seconds, in the calling thread: call them from a worker
  thread, never a UI thread.
- **Reset** deletes every entry, fact, conversation record, round and undo batch, and
  scrubs the file. The switches (`paused`, `include_sensitive`) stay.

#### Failures, and never waiting

Memory never makes `chat()` raise, and a failed engine call is silent: it returns `False`
(a direct call) and logs one WARNING line on the `talk2view.memory` logger, with the HTTP
status and the engine's error type or the exception's class, never any memory text. The
next trigger tries again once the cooldown has run:

- After a failed call, the calls the SDK queues itself (from `run_due()`, a new
  conversation or an import) make no engine call for 60 seconds, twice as long after each
  further failure in a row, at most an hour. Each checks again as it starts, so a backlog
  meeting a failing engine makes one call, not one each. A `Retry-After` from the engine
  makes the wait at least that long, still at most an hour. A call that succeeds ends the
  cooldown. Direct calls (`extract()`, `consolidate()`, `import_text()`) ignore it, and
  their outcome still updates it. The cooldown is not saved: after a relaunch the first
  `run_due()` tries again at once.
- Extraction, consolidation and imports are charged to the end-user's own budget, as a
  chat turn is. The engine refuses them with `402 memory_budget_low` while the user has
  less than a floor left (currently 10% of their budget or $0.10, whichever is more),
  which the SDK treats as any other failed call: silent, logged once, retried after the
  cooldown.
- A `403 memory_disabled` (memory is not turned on for the partner) turns the engine calls
  off for the rest of the process: `t2v.memory.enabled`, a read-only property, becomes
  `False` and stays that way for this `Talk2View`. It is also `False` once the memory is
  closed. Chat is unaffected.

The calls `run_due()` and a new conversation queue run one at a time on one daemon thread,
`talk2view-memory`, so a chat never waits for one. That thread can also meet a rejected
refresh token, so an `on_auth_state_change` listener can run on it (see the note on
listener callbacks above). `T2VMemory(client, store, inline=True)` runs the calls in the
caller, in order, for tests and evals that build their own `T2VMemory`; `Talk2View` never
does.

`chat()` and `respond_to_approval()` pass the turn's events through
`t2v.memory.observe_turn` and `t2v.memory.observe_approval` for you, which is how `memory`
events are applied and rounds recorded. Only a host that drives `T2VSession` itself calls
them, together with `run_due`, `turn_payload` and `handle_search`.

#### The file, its permissions and the SQLite backend

- **One file per user**: `<memory_path>/<user id>.sqlite3`. An id of lowercase letters,
  digits, `_` and `-` (1 to 128 of them; an engine UUID is one) names its file as it is.
  Any other id is named by the first 32 hex characters of its SHA-256, so no id makes an
  unsafe file name, and two ids that differ only in case never share a file.
- **Permissions.** Nothing is created until the memory is first used. When the SDK has to
  create the `memory_path` directory it makes it `0700`; a parent it has to create too
  gets the default mode, and an existing directory is left as found, so point
  `memory_path` at a directory under one that already exists. The file is created `0600`
  before SQLite opens it, and SQLite gives its `-wal` and `-shm` files the same mode. On
  Windows the permission calls are skipped, as `FileStorage`'s are: keep `memory_path` in
  a folder only the user can read.
- **Deleted text is scrubbed** from the file once its undo window has passed (at once for
  a reset or a deleted chat).
- **Search** runs on SQLite's FTS5 where it can, and the store picks a backend when it
  opens: the standard library's `sqlite3` when it was built with FTS5; else `apsw`, which
  bundles a SQLite with FTS5; else `scan`, which needs no FTS5, keeps the index in plain
  tables and scores it in Python, so a very large memory searches more slowly. Memory works
  on all three, `t2v.memory.store.backend` says which is in use, and a file can move between
  them (the store rebuilds the index when it opens a file indexed the other way). To have
  `apsw` (3.54 or later), install the `[memory]` extra; apsw needs Python 3.11, so on 3.10
  the extra adds nothing:

```bash
pip install 'talk2view[memory]'
```

#### Privacy

Memory is kept only on the device: one file per user, readable only by that user where the
platform allows, and the engine keeps no memory content. There is no sync in this version.
What leaves the device is what the engine needs to use it, and it reaches the AI providers
as a message does, so [Intended use and data handling](#intended-use-and-data-handling)
applies to it too:

- with every turn: the entries, the titles and summaries of recent conversations, and up
  to four recalled passages;
- when the agent searches: the results of that search;
- extraction: a conversation's new rounds with up to six messages of context before them,
  the entries and the facts related to what the user said. Consolidation: the entries, the
  facts and the conversation summaries. An import: the text the user gives it, with the
  entries and related facts as an extraction has;
- an incognito or paused turn sends only its status.

Tell your users that memory lives on their device and is sent to the AI with each message,
and how to pause it. A sign-out closes the memory and leaves the file: to make the app
forget a user, call `t2v.memory.store.reset()` first, or delete their file.

### `t2v.voice`

```python
models = t2v.voice.list_models()            # [AudioModel]; show `m.name or m.id`
result = t2v.voice.transcribe("note.webm", model=models[0].id, language="en")
print(result.text)
```

Each `AudioModel` is a `Model` that also carries `supported_languages` (ISO
639-1 codes; `None` means any language) and a short `description`, as in the
TypeScript SDK. With `anonymous_auto_start` both calls start a guest session
first, as every other authenticated call does. The audio is read before that,
so a missing file never costs a guest session. A transcription is an upload,
bounded by `upload_timeout`.

### Partner config, models, completions, warm-up

```python
config = t2v.get_config()      # PartnerConfig: default/allowed/gated models, flags; cached
models = t2v.list_models()     # ModelsResponse; show `m.name or m.id`
result = t2v.completions("medgemma-27b", [{"role": "user", "content": "..."}])
t2v.warm_up()                  # get the end-user's key minted early; never raises
content = t2v.get_attachment_content(attachment.id)   # AttachmentContent(data, mime_type)
t2v.set_chat_defaults(model="gemini-3.8-flash")   # the model for the next turns
t2v.set_chat_defaults(model=DEFAULT_MODEL)        # back to the partner's default
```

`completions()` needs a signed-in end-user: for a guest the engine answers
`sign_in_required`. It asks for one reply (`max_tokens` up to 16384).

`set_chat_defaults(model=None)` leaves the model as it is. `DEFAULT_MODEL`
forgets it, so chat sends none and the engine uses the partner's default. The
engine keeps the model a session last used, so after `DEFAULT_MODEL` replaces a
model the next turn first deletes the engine session and starts a fresh one. The
conversation carries on through the history the caller replays
(`ChatController` replays its own).

`get_config()` is fetched once per login (concurrent callers share the request, and on
a failure all of them get the same exception object) and invalidated on any auth
change. `warm_up()` runs once per signed-in identity and
swallows every error (an engine without `POST /v1/account/warm` is a no-op). Uploads
are checked against the engine's MIME allow-list before any request
(`ATTACHMENT_ACCEPT`, `is_allowed_attachment_type`); the allow-list is checked before
the file is even read. `get_attachment_content()` requires `attachment_id` to be a
UUID (anything else raises before any request) and caps a download at 25 MB.

### `ChatEvent`

Events yielded by `chat()` / `send_message()`.

| `event.type` | Fields | Description |
|---|---|---|
| `"text"` | `content` | A chunk of the AI's response text |
| `"reasoning"` | `content` | A chunk of the model's reasoning (needs `Capabilities(reasoning=True)`) |
| `"tool_call"` | `tool_name`, `tool_call_id`, `arguments` | The AI is calling one of your tools (auto-handled if registered) |
| `"tool_event"` | `tool_name`, `tool_call_id`, `tool_event` | Lifecycle of a *platform* tool call (needs `Capabilities(tool_events=True)`) |
| `"approval_required"` | `kind`, `tool_name`, `tool_call_id`, `arguments`, `description`, `details` | The turn is paused for the end-user — answer with `respond_to_approval()` |
| `"approval_result"` | `tool_name`, `tool_call_id`, `decision` | The decision that was applied (`"once"`, `"always"` or `"deny"`) |
| `"status"` | `status`, `message` | Agent status update (e.g., "thinking", "planning") |
| `"todos"` | `todos` | Agent's task plan |
| `"memory"` | `tool_call_id`, `memory_ops` | Changes the agent asked to make to the user's memory (needs `Capabilities(memory=True)`, which `memory_path` sets). The SDK has applied them before you see the event, unless memory is paused, the chat incognito or `memory_path` is unset; `t2v.memory.last_applied` is what it applied |
| `"error"` | `message`, `error_type`, `detail` | A failure reported inside the stream (e.g. `"session_lost"`, a budget error). Never rendered as text |
| `"done"` | `thread_id` | Stream complete |

An `error` event is a report, not an exception: the iteration continues (and
usually ends) normally. `chat()` raises for failures that end the turn outright.

### Errors

```python
from talk2view import T2VError, AuthenticationError, PartnerKeyError, NetworkError

try:
    t2v.auth.login(email, password)
except AuthenticationError:
    print("Bad credentials")
except PartnerKeyError:
    print("Invalid partner key")
except NetworkError:
    print("Connection failed")
except T2VError as e:
    print(f"{e.type}: {e} (HTTP {e.status_code}, request {e.request_id})")
```

The class is chosen from the engine's `error.type`, not from the HTTP status:
**a 401 is an `AuthenticationError` only when the engine says so.** A 401
caused by the partner key raises `PartnerKeyError` (refreshing cannot fix it,
and the user's tokens are left alone), and a 401 with any other type raises a
plain `T2VError`. An expired user token is refreshed and the request retried
before you ever see an error; `AuthenticationError("Session expired…")` means
the refresh token itself was rejected and the stored tokens have been cleared —
a sign-out, so the `on_auth_state_change` listeners have already heard `None`.

`T2VError` carries a user-safe `message`, the engine's `type`, the
`status_code`, an optional machine-readable `code`, and a separate `detail` for
debugging. Server stack traces, DB errors and opaque non-JSON bodies are kept
out of `message` so they cannot be shown to an end-user.

Errors from the engine also carry `request_id`, the engine's id for the failed
request (also its `X-Request-Id` header; quote it when you contact support), and
`retry_after`, the seconds to wait when the engine sent `Retry-After` (rate
limits and some temporary outages). The SDK does not retry for you. A stream
`error` event carries `request_id` too. Every error type the engine sends is
listed at [docs.talk2view.com/errors](https://docs.talk2view.com/errors).

> **Upgrading from 0.1.0:** `T2VError`'s second parameter is now `type=` (was
> `error_type=`) and `code` is the fourth positional parameter, before
> `detail`. See [CHANGELOG.md](CHANGELOG.md).

## 3D Slicer Integration

Here's how to integrate the SDK into a 3D Slicer extension.

### Token Storage with QSettings

Persist login across Slicer restarts:

```python
import qt

class SlicerSettingsStorage:
    """Store Talk2View tokens in Slicer's QSettings."""

    PREFIX = "Talk2View/"

    def get(self, key):
        settings = qt.QSettings()
        value = settings.value(self.PREFIX + key)
        return value if value else None

    def set(self, key, value):
        settings = qt.QSettings()
        settings.setValue(self.PREFIX + key, value)

    def delete(self, key):
        settings = qt.QSettings()
        settings.remove(self.PREFIX + key)
```

### Defining Slicer Tools

Tools run locally in Slicer's Python — they have full access to the MRML scene, VTK, and all Slicer modules:

```python
import slicer
from talk2view import tool

@tool
def get_scene_summary() -> str:
    """Get a summary of all nodes in the current 3D Slicer scene."""
    nodes = []
    for i in range(slicer.mrmlScene.GetNumberOfNodes()):
        node = slicer.mrmlScene.GetNthNode(i)
        nodes.append(f"{node.GetClassName()}: {node.GetName()}")
    return "\n".join(nodes)

@tool
def load_volume(file_path: str) -> str:
    """Load a medical image volume into the scene.

    Args:
        file_path: Path to the volume file (NRRD, NIfTI, DICOM dir).
    """
    node = slicer.util.loadVolume(file_path)
    return f"Loaded volume: {node.GetName()}"

@tool
def set_slice_offset(view: str, offset: float) -> str:
    """Set the slice offset for a slice view.

    Args:
        view: The slice view name (Red, Green, Yellow).
        offset: The offset position in mm.
    """
    slice_node = slicer.mrmlScene.GetNodeByID(f"vtkMRMLSliceNode{view}")
    slice_node.SetSliceOffset(offset)
    slicer.util.forceRenderAllViews()
    return f"Set {view} slice offset to {offset}mm"
```

Works with bound methods too — the pattern used by SlicerTools:

```python
class SlicerTools:
    def __init__(self):
        self.tools = [
            tool(self.get_scene_summary),
            tool(self.load_volume),
            tool(self.set_slice_offset),
        ]

    def get_scene_summary(self) -> str:
        """Get a summary of all nodes in the current scene."""
        # self is bound, `self` param is excluded from the schema
        ...

    def get_tools(self):
        return self.tools
```

### Streaming in a Qt App

Slicer runs on the Qt event loop. Run the stream in a thread to avoid blocking
the UI, and pass `qt_dispatcher(qt.QTimer)` so your tool handlers still run on
Slicer's main thread, where MRML access is legal:

```python
import threading
import qt
import slicer
from talk2view import qt_dispatcher

class Talk2ViewWidget:
    def __init__(self):
        # Construct on the main thread — the dispatcher starts a QTimer.
        self.t2v = Talk2View(
            partner_key="pk_live_...",
            storage=SlicerSettingsStorage(),
            model="gpt-4.1-mini",
            dispatcher=qt_dispatcher(qt.QTimer),
        )

        # Register tools
        slicer_tools = SlicerTools()
        self.t2v.tools.register_from_functions(slicer_tools.get_tools())

    def on_cancel_clicked(self):
        self.t2v.stop()  # ends the in-flight turn, from the UI thread

    def on_send_clicked(self, message):
        """Called when the user hits Send in the chat UI."""
        self.response_text = ""
        thread = threading.Thread(
            target=self._stream_chat,
            args=(message,),
            daemon=True,
        )
        thread.start()

    def _stream_chat(self, message):
        """Runs in a background thread."""
        try:
            for event in self.t2v.chat(message):
                if event.type == "text":
                    self.response_text += event.content
                    # Update UI on the main thread
                    slicer.util.mainWindow().statusBar().showMessage(
                        self.response_text[:100]
                    )
                elif event.type == "done":
                    slicer.util.mainWindow().statusBar().showMessage("Done")
        except Exception as e:
            slicer.util.errorDisplay(f"Talk2View error: {e}")
```

> **Important:** without a dispatcher, `@tool` handlers execute on the
> **calling thread** — stream from a background thread and your handlers run
> there too, which MRML does not allow. `dispatcher=qt_dispatcher(qt.QTimer)`
> (above) posts every handler and permission callback to Slicer's main thread
> and blocks the streaming thread until it has run, so handlers can touch the
> scene directly. Construct the dispatcher on the main thread.

### Full Integration Example

```python
import qt
import slicer
from talk2view import Talk2View, qt_dispatcher, tool

PARTNER_KEY = "pk_live_..."

# Storage
class SlicerSettingsStorage:
    PREFIX = "Talk2View/"
    def get(self, key):
        return qt.QSettings().value(self.PREFIX + key) or None
    def set(self, key, value):
        qt.QSettings().setValue(self.PREFIX + key, value)
    def delete(self, key):
        qt.QSettings().remove(self.PREFIX + key)

# Tools
@tool
def get_scene_summary() -> str:
    """Get a summary of all nodes in the current 3D Slicer scene."""
    nodes = []
    for i in range(slicer.mrmlScene.GetNumberOfNodes()):
        node = slicer.mrmlScene.GetNthNode(i)
        nodes.append(f"{node.GetClassName()}: {node.GetName()}")
    return "\n".join(nodes) if nodes else "Scene is empty"

@tool
def load_sample(name: str) -> str:
    """Download and load a sample dataset.

    Args:
        name: Sample name (e.g., "MRHead", "CTChest", "CTA-cardio").
    """
    import SampleData
    node = SampleData.downloadSample(name)
    return f"Loaded sample: {node.GetName()}"

# Initialize (on Slicer's main thread)
t2v = Talk2View(
    partner_key=PARTNER_KEY,
    storage=SlicerSettingsStorage(),
    model="gpt-4.1-mini",
    dispatcher=qt_dispatcher(qt.QTimer),
)
t2v.auth.login("user@example.com", "password")
t2v.tools.register_from_functions([get_scene_summary, load_sample])

# Chat
for event in t2v.chat("Load the MRHead sample and tell me about it"):
    if event.type == "text":
        print(event.content, end="")
    elif event.type == "tool_call":
        print(f"\n> Calling {event.tool_name}...")
    elif event.type == "done":
        print()
```

## Development

```bash
uv sync --all-extras
uv run pytest                                    # Run tests
uv run ruff check src/ tests/ scripts/           # Lint
uv run mypy src/                                 # Type check
T2V_STRESS=1 uv run pytest tests/test_controller_stress.py   # Opt-in concurrency probe (~7 s)
```

The chat page that `talk2view.ui` serves is built from `embed/` (Node 22) into
`src/talk2view/ui/static/`, which git ignores. Build it before running the UI from a
checkout or building a wheel:

```bash
(cd embed && npm ci && npm run build)
uv build                                         # sdist and wheel, both with the page
python scripts/check_wheel_assets.py dist/*.whl dist/*.tar.gz
```

`contract/` holds the engine's stream and error fixtures, which the tests replay against
the real client. See [`embed/README.md`](embed/README.md) and
[`contract/README.md`](contract/README.md).

## Requirements

- Python >= 3.10
- Single runtime dependency: `httpx >= 0.27`
- Optional: `apsw >= 3.54` on Python 3.11+, through the `[memory]` extra
  (`pip install 'talk2view[memory]'`), for on-device memory on a host whose `sqlite3` lacks FTS5

## License

MIT — see [LICENSE](LICENSE). Copyright A2B Technology Corporation Pty Ltd.
