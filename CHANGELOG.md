# Changelog

All notable changes to the `talk2view` package are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

## [0.4.0] - 2026-10-22

### Added

- `T2VAuth.start_anonymous(..., warm=True)`, for a guest started because the end-user's
  message is on its way: the engine then starts minting the guest's key during the
  sign-in, and an engine without the field ignores it. `chat()`,
  `completions()`, `warm_up()` and the chat page's guest start send it; a guest started
  for a model list, an upload, the voice routes or `ensure_session()` does not.
- Turn timings (`talk2view.metrics`): `t2v.metrics.on_turn(callback)` hands every
  `ChatController` turn's `TurnTiming` to the host as it ends: request sent, response
  headers, first event and first text; per tool call the interrupt, the approval wait,
  the wait for the host's main thread, the handler's run and the `/resume`; outcome,
  error type, model and the stream's completion id. No message, reply, argument, result,
  token or URL is recorded. `ChatController.metrics` is the same hub. The chat page reports
  its own part (Send to POST, first text op to paint) with the new `metrics` command, for
  a `send` that carries `timing: true`.
- `RequestTiming.server_timing`: the engine's `Server-Timing` header on a chat or
  `/resume` request, parsed into `{metric name: dur ms}` (None when the engine sends
  none), next to `request_id` (its `X-Request-Id`); both in `as_dict()`.
  `talk2view.metrics.parse_server_timing()` is the parser.
- `ChatController.export_conversation()` / `restore_conversation(snapshot)` and
  `ConversationSnapshot` (TS `exportConversation`/`restoreConversation`): put a
  conversation aside and back; the next message replays its history into a fresh engine
  session. `Talk2View.detach_session()` forgets the session without deleting it;
  `Talk2View.partner_key` is read-only.
- `talk2view.conversations.ConversationStore`: earlier conversations on the device, the
  TS `localStorage` shape and caps (50 per identity, 8 identities, 512 KB) over
  `data_storage`, one bucket per user id.
- `talk2view.ui.threads.ThreadsState` and the page's Chats view: the `threads` slice and
  the commands `threadNew`, `threadSwitch`, `threadRename`, `threadDelete`
  (`ChatFeatures.thread_list`, on by default). New chat keeps the conversation in the
  list and detaches the engine session; `newSession` still deletes it.
- `ChatController.follow_identity(callback)` and `exchange_conversation(snapshot, *,
  epoch, carry=None)`, with `talk2view.IdentityChange`: a list of
  conversations hears each change of signed-in user with the conversation it took off
  the screen, exported in the same locked step, and swaps the conversation on screen
  without a gap in which a message could land unrecorded. `FakeController` has both.
- `ChatController.on("conversationReplaced", cb)`: a conversation was just put on
  screen, replacing whatever was there, whichever of `restore_conversation` or a
  snapshot-carrying `exchange_conversation` did it — not raised for an export-only
  `exchange_conversation(None, ...)` or one refused for a stale `epoch`. For a host
  that needs to react to any switch without overriding both methods itself.
  `FakeController` has it too.

### Changed

- `talk2view.ui` keeps a list of conversations by default (`ChatFeatures(thread_list=True)`,
  as the TypeScript `<Talk2ViewChat>` does). A host that upgrades without opting out: at
  `UIServer.start()` the controller gets the newest stored conversation back when it has
  none on screen; each reply is written, 500 ms after it settles, into the `Talk2View`'s
  `data_storage` under `t2v_chat_conversations_v1:<partner key>` (up to 512 KB per
  partner key, one bucket per signed-in user, guests included). With the default
  `data_storage` that is the token store, so a `FileStorage` there rewrites the token
  file too: give `Talk2View(data_storage=...)` a store of its own. Call
  `UIServer.stop()` before quitting; an interpreter exit without it still writes the
  last change. `ChatFeatures(thread_list=False)` turns all of this off. On read, a
  stored conversation's title is cut to `MAX_STORED_TITLE` (200, `threadRename`'s own
  cap) and its messages to the newest `MAX_STORED_MESSAGES` (5000) — a backstop for a
  hand-edited or corrupt file, not a shape this SDK's own writes approach.
- Once `POST /v1/sessions` has answered, a new session's tool schemas and skills are
  sent together instead of one after the other, so a first turn waits for two round
  trips instead of three: `setup_ms` went from about 2.1 s to about 1.4 s at the
  owner's latency. They still follow the create, so a session the engine dropped just
  before it (an eviction, another client's DELETE) cannot leave the new one without
  them. The skills' `on_change` listeners can now run on the `t2v-register-skills`
  thread.
- `talk2view.ui`: a guest started by the page (`startAnonymous`, sent just before the
  first `send`) has its key warmed at once (`warm_up()`), so the key's mint runs while
  the turn sets up its session instead of on the turn's first request.
- A turn's streams now share one connection to the engine. A stream that stopped at
  `[DONE]` or at a client tool's interrupt used to be closed before the end of its body
  was read, so it could not be pooled, and every `/resume` paid a new TCP + TLS
  handshake (about 0.5 s at the owner's latency). The rest of the body is now read
  (bounded: 1 s, 64 KiB) and the connection pooled only after the stream's cancel has
  let go of it. A cancelled stream's connection is still dropped, never pooled
  half-read.
- An idle connection is kept for 300 s instead of httpx's 5 s, so the first request
  after a user's think time reuses it too, and every connection sends TCP keepalives
  (first probe after 60 s, then every 10 s, 3 tries; the options a platform refuses
  are left out). A connection a NAT or firewall dropped silently is then discarded
  instead of reused.
- Chat streams ask for an uncompressed body (`Accept-Encoding: identity`).
- With a proxy configured (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, or the system's
  settings, as httpx reads them), the client keeps httpx's default transports so the
  proxy is still used: those connections get the 300 s expiry but no TCP keepalives.
- The chat page's theme tokens are now the TypeScript chat's names under `--t2v-`
  (`--t2v-background`, `--t2v-foreground`, `--t2v-primary`, `--t2v-muted`,
  `--t2v-muted-foreground`, `--t2v-border`, `--t2v-destructive`, `--t2v-radius`, the
  composer's three, and the brand hexes `--t2v-teal` … `--t2v-amber`), with the same
  values, light and dark. The page's earlier names (`--t2v-bg`, `--t2v-fg`,
  `--t2v-surface`, `--t2v-line`, `--t2v-danger`, …) are gone: a host `Theme` that sent
  them now sends nothing the page reads. `--t2v-muted` changed meaning: it is a
  surface (the user's bubble), and `--t2v-muted-foreground` is the quiet text colour.
- The chat page now uses the TypeScript chat's look: header, welcome heading, message
  bubbles, composer, tool rows, the approval and error cards, code blocks and the reply's
  copy/refresh bar all changed shape and spacing to match. A host embedding this page
  will see the restyle without any code change on their side.

### Fixed

- `FileStorage` re-reads its file when it has changed since this instance last looked
  (its `(mtime, size, inode)`), instead of trusting its first read for the rest of the
  process. A relaunched app's own new `FileStorage` on the same path — or another
  process's write, read after it finished — used to be invisible, and a write from the
  stale copy could erase what that other write added. Two processes writing at
  genuinely the same instant remain a lost-update race for a caller to serialise (a
  single-instance lock); this only fixes the *sequential* case. On a filesystem with
  second-level mtime and no usable inode (FAT/exFAT) a same-size rewrite within that
  window could still be missed — not reachable through this class's own atomic writes,
  only a hand edit made in place.

## [0.3.0] - 2026-10-21

The first release of the SDK on PyPI. The earlier `talk2view` uploads, 0.2.0b1 and
0.2.0b2, were placeholders that reserved the name, and 0.2.0 was never published to
PyPI, so everything below and under 0.2.0 is new to anyone installing from PyPI.

### Added

- **Errors carry `request_id` and `retry_after`.** `request_id` is the engine's id for
  the failed request, from the error body or the `X-Request-Id` header; quote it when
  you contact support. Stream `error` events (`ChatEvent`, `StreamError`) carry it too.
  `retry_after` is the number of seconds from a `Retry-After` header. Both are `None`
  when the engine sent nothing. `error_for_type` takes them as keyword arguments, and
  `parse_retry_after` reads the header.
- **Requests name the SDK.** Every request to the engine sends
  `X-T2V-Client: talk2view-python/<version>`, so the engine can tell which release a
  request came from.

- **`talk2view.ui`**, the localhost chat UI server (standard library only, opt-in):
  `UIServer`, `AppState`, `HostAdapter`, `GenericHost`, `Theme` and
  `StaticAssetsMissingError`. It serves the bundled chat page on `127.0.0.1` and streams
  the chat state plus the account slices (auth, models, config, capabilities, theme,
  preferences, command results) as SSE op batches with `Last-Event-ID` replay. It takes
  commands on `POST /command` and uploads on `POST /upload`, behind a loopback + `Host` +
  `Origin` + per-launch bearer-token gate.
- `talk2view.ui.testing.FakeController`, a scripted stand-in for `ChatController`, and
  `python -m talk2view.ui.testing`, which serves a built page against it with no engine.
- `ChatController.t2v`, the `Talk2View` a controller drives (read-only).
- Wheels and sdists carry the chat page (`talk2view/ui/static/`) through hatch
  `artifacts`; CI checks both with `scripts/check_wheel_assets.py`.
- The chat page itself, built from `embed/` for Chromium 83: the assistant-ui
  thread, composer, approval card, error card, Account and Settings views, and
  attachments, driven entirely by `UIServer`'s events and commands.
- `T2VAuth.get_valid_access_token(force_refresh=False)`: a token valid now, for a
  request the SDK does not make, refreshed first when it is about to expire and
  sharing the SDK's one refresh (TS `getValidAccessToken`).
- `DEFAULT_MODEL`: `Talk2View.set_chat_defaults(model=DEFAULT_MODEL)` returns to the
  partner's default model; the next turn starts a fresh engine session, because the
  engine keeps a session's model when a message names none.
- `Talk2View(request_timeout=30.0, upload_timeout=120.0)`, also on `T2VClient`, with
  read-only `request_timeout` and `upload_timeout`; `Talk2View.anonymous_auto_start`
  and `Talk2View.model`, the model new turns name (None after `DEFAULT_MODEL`), both
  read-only.
- `AudioModel`, a `Model` with `supported_languages` and `description`:
  `t2v.voice.list_models()` returns them.
- `talk2view.ui`: `ChatOptions`, `ChatWelcome`, `ChatFeatures` and `ToolPresentation`
  (`UIServer(options=...)`, `AppState(options=...)`), served as the `options` and
  `tools` slices, with attachments and guest sessions enforced when turned off
  (`attachments_disabled`, `anonymous_not_allowed`); the `limits` slice (demo limit
  reached, guest session refused); `selectModel{modelId: null}` for the default model;
  `FakeController.on()` and its `/limit` script.
- `T2VAuth.sign_in_with_provider()` and `desktop_sign_in_providers()`: sign in with Apple
  or Google through the system browser and a loopback hand-off;
  `ProviderSignInError` (`type` in `PROVIDER_SIGN_IN_ERROR_TYPES`, among them
  `oauth_expired`, `rate_limited` and `engine_url_mismatch`). A `login()`, `signup()` or
  `logout()` that changes the session while one waits wins: the provider session that
  comes back later is dropped.
- The chat page's "Continue with Apple / Google": `UIServer` commands `oauthStart`
  (`provider` is `apple` or `google`), `oauthCancel` and `oauthDismissError`; the `auth`
  slice's `oauthPending` and `oauthError`; `capabilities.oauth` (now live) and
  `capabilities.oauthProviders`; and `ChatFeatures(provider_sign_in=True)`, which the
  server enforces (`oauth_disabled`). `GenericHost.open_external` returns what its opener
  returned, so a `False` from `webbrowser.open` is reported.

### Changed

- Uploads (attachments and audio) time out after 120 s instead of 60 s, as in the
  TypeScript SDK.
- `t2v.voice.transcribe()` and `list_models()` start a guest session first when
  `anonymous_auto_start` is on, as every other authenticated call does.
  `list_models()` leaves out an entry that is not an object or has no id.
- `talk2view.ui` takes back a model it applied once the end-user's model list no
  longer has it. The model used to stay applied until the next pick.

### Fixed

- A token refresh in flight while a new session is stored (a `login()`, a sign-in with
  Apple or Google) no longer writes the old session's tokens over it, and a refresh the
  engine rejects no longer signs out a session stored after it began; a refresh racing a
  `logout()` no longer brings the session back. A refresh writes over, or clears, only
  the session it refreshed. `TokenStorage` methods must not wait on another thread.
- A refresh the engine refuses because of the **partner key** no longer signs the
  end-user out: `get_valid_access_token()` (and any refresh) raises `PartnerKeyError`
  and keeps the session. A normal request already did this; the refresh route's own
  `partner_key_error` 401 was read as a rejected refresh token.
- `ChatController.send()`, `retry()` and `edit()`: a failure while the turn is
  recorded (an attachment that is not an `Attachment`, say) ends the run, with
  `error` set, and is then raised. It used to leave the controller busy for
  good: `wait_idle()` never returned True and every later `send()` was refused.
- A model or capabilities change (`set_chat_defaults`, and the UI's `selectModel`)
  that lands while `chat()` is creating a session for the previous turn now applies
  starting the very next turn, instead of the turn after that: the stale session,
  built before the change, is no longer reused.
- `set_chat_defaults()` with a new model or capabilities no longer drops the
  session object, so `respond_to_approval()` still answers on it. A model picked
  while an approval was pending (in `talk2view.ui`, Settings stays open while the
  card waits) made the approve fail with "No active session", and the tool never
  ran. The next `chat()` still builds a session with the new settings;
  `get_session()` returns the old one until then.
- "Default" in `talk2view.ui` (`selectModel{modelId: null}`) takes back the
  end-user's pick even when an earlier `UIServer` over the same `Talk2View`
  applied it. A server rebuilt over it whose first model list failed showed
  Default while every turn stayed on the pick.
- A session create still in flight when the end-user switches accounts no longer
  leaves the next turn on the previous account's session, which the engine
  answered with a 404 before the turn was recovered and resent.

## 0.2.0 - not published to PyPI

First open-source release: the package is now **MIT** licensed (`LICENSE`;
copyright A2B Technology Corporation Pty Ltd).

### Breaking changes

- **`signup()` returns a `SignupOutcome`** (`user`, `confirmation_required`)
  instead of a `User`, because a signup may await email confirmation and,
  from an anonymous session, converts the account in place. Read
  `outcome.user` where you read the return value before.

- **`ChatEvent` fields.** `approval_required` events carry `kind`,
  `description` and `details`; `approval_result` events carry `tool_call_id`
  and `decision`; `tool_event` events carry `tool_event`; `error` events carry
  `error_type` and `detail`. Code that built `ChatEvent` objects positionally
  or compared them field-for-field must use keywords.

- **Error constructors.** Every error class takes
  `(message, type=None, status_code=None, code=None, detail=None)`; see the
  `T2VError` note below. New: `AnonymousUnavailableError` (raised by
  `start_anonymous()` only) and the `ANONYMOUS_UNAVAILABLE_TYPES` set.

- **`upload_attachment()` rejects unsupported MIME types** before any request
  (`T2VError`, type `unsupported_attachment_type`): PNG, JPEG, WebP, GIF and
  PDF only, the engine's own allow-list — checked before the file is read.

- **`get_attachment_content()` validates the attachment id.** Anything that
  is not a UUID raises `T2VError` (type `invalid_attachment_id`) before any
  request, and a download is capped at 25 MB.

- **`T2VClient.request(headers=)` refuses the auth headers.** Passing
  `Authorization` or `X-T2V-Partner-Key` (any case) raises `ValueError`; only
  the client itself sets those.

- **`T2VSkills.set_enabled(name, True)` can raise `SkillValidationError`**
  when enabling the skill would push the register body over the engine's
  8 MB cap. Disabling is never refused.

- **`is_authenticated()` is False for an empty stored access token**, not
  just a missing one.

- **`login()` raises `AuthenticationError` when the answer lacks the user or
  either token**, and stores nothing. `signup()` does the same for an answer
  carrying only one of the two tokens (the account exists: sign in); an answer
  with neither is still "confirmation pending".

- **`logout()` and every auth clear** now also delete `user_api_key` and
  `is_anonymous` from the token store.

- **A rejected refresh token now fires `on_auth_state_change(None)`.** The
  stored session was already cleared (the request still raises
  `AuthenticationError("Session expired…")`); now the listeners hear the
  sign-out too, on that request's thread, instead of at the next auth event.
  Nothing fires when nobody was signed in.

- **An `on_auth_state_change` listener that raises no longer propagates** out
  of `login()`, `logout()`, `signup()` or `start_anonymous()`: it is logged,
  and the listeners behind it still run.

- **`ChatController` follows the signed-in identity.** Any change of user id
  resets approval consent (`alwaysAllowedTools`); a sign-out, or a switch to a
  different account (anonymous to another anonymous, account to anonymous,
  account to another account), also drops the local conversation — messages,
  replay history, a pending approval, `threadId` — and abandons the run in
  flight, with no engine call. A sign-in from signed out and an anonymous demo
  becoming an account keep the conversation. `isAnonymous` follows the auth
  state without waiting for a send. The controller (and `Talk2View`) hear each
  auth change before any `on_auth_state_change` listener a host adds.

- **`ChatController.send()` can raise `ControllerBusyError` right after a
  skills edit**: a session recycle started by `skills.remove()` (on an engine
  that merges, or after a failed replace) is a run, as `clear()`'s is.

- **`FileStorage` never treats an unreadable file as empty.** A file that
  exists but cannot be read (permissions, a Windows sharing violation) makes
  `get`/`set`/`delete` raise that `OSError`, and the next call reads it again.
  A file that is not a JSON object of strings starts the store empty (string
  values kept) and is moved aside to `<name>.corrupt-<timestamp>` before the
  next write replaces it. `Talk2View(...)` itself still starts while its token
  store cannot be read (a warning is logged).

- **`preferences.update()` checks types.** A value that is neither a string
  nor `None` raises `TypeError` before anything is stored.

- **`skills.load()` always sends the saved set** with the next register call,
  even an empty one, so a session the engine kept from an earlier run drops
  skills removed since. A host that stops using skills calls `skills.clear()`
  once.

- **`T2VError` constructor.** The second parameter is now `type=`, not
  `error_type=`, and a new `code` (the engine's machine-readable sub-code) sits
  **fourth**, before `detail`:

  ```python
  # 0.1.0
  T2VError(message, error_type="rate_limit_error", status_code=429, detail="...")
  # 0.2
  T2VError(message, type="rate_limit_error", status_code=429, code="...", detail="...")
  ```

  Positional callers are affected: a 4th positional argument used to be
  `detail` and is now `code`. `err.type`, `err.status_code` and `err.detail`
  are unchanged, and `err.code` is new. The same shape applies to
  `AuthenticationError`, `PartnerKeyError` and `SessionError`.

- **A 401 is only an `AuthenticationError` when the engine says so.** The error
  class is chosen from the engine's `error.type` (as in the TypeScript SDK),
  never from the status code alone, so a 401 caused by the partner key raises
  `PartnerKeyError` and a 401 with some other type raises `T2VError`. Catch
  `T2VError` if you were relying on the status code.

- **`"integer"` argument validation rejects non-integral numbers.** `3` and
  `3.0` are accepted (JSON has one number type); `1.5`, `NaN` and `Infinity`
  are returned to the model as an error result instead of reaching the handler.
  The TypeScript SDK accepts any number here — a deliberate divergence.

- **An invalid tool `permission=` raises `TypeError` at registration.** It must
  be `None`, a `bool`, or a callable.

- **`Capabilities.surface`** is `Literal["keyboard", "app"]`; the engine
  rejects any other value.

### Added

- **Skills** — `t2v.skills` (`T2VSkills`): add skills from code (`Skill`) or
  from directories of `SKILL.md` files (`load_directory`, parsed exactly as the
  engine parses them), enable/disable/remove, `save()`/`load()` in
  `data_storage` under the TypeScript SDK's key, and `list_platform()` for the
  partner catalogue (`GET /v1/skills`; `None` on an older engine). Edits are
  batched into one debounced register call that always sends `replace: true`;
  skills are re-sent on every new session, including a recovered one. On an
  engine that honours `replace` a removed or disabled skill leaves with the
  next call; on an older engine (or when a replace fails) it sets
  `needsSessionReset` and the session is recycled when idle. A failed
  register on a new session the engine may have kept (`POST /v1/sessions` is
  get-or-create) sets it too, and `chat()` recycles once before the turn.
  Validation
  mirrors the engine's units (name in code points, body in bytes):
  `SkillValidationError`. The skills slice of `ChatSnapshot` is now live:
  `ChatController.set_skills_state()` records it on the `seq` line.
- **`FileStorage`** — a `TokenStorage` backed by a `0600` JSON file with
  atomic writes; `Talk2View(..., data_storage=)` for skills and preferences.
- **Anonymous demo sessions** — `t2v.auth.start_anonymous(captcha_token=None)`,
  `is_anonymous()`, convert-on-`signup()` with the 409 → login fallback,
  `Talk2View(anonymous_auto_start=)`, `ensure_session()` (single-flight) and
  `on_anonymous_unavailable()`. Requires partner configuration (captcha).
- `get_config()` (single-flight, cached, invalidated on auth change; the
  engine's nine-field `PartnerConfig`), `list_models()` with display names,
  `completions()`, `warm_up()`, `get_attachment_content()`, and the attachment
  constants (`ATTACHMENT_ACCEPT`, `is_allowed_attachment_type`).
- **Preferences** — `t2v.preferences` (`T2VPreferences`, `UserPreferences`)
  on `data_storage`, same JSON shape as the TypeScript SDK.
- `__version__`.
- **`ChatController`** — the headless chat state machine, one per `Talk2View`.
  Python owns the conversation and a UI is a view over it: read the whole
  state with `snapshot()`, follow it with `subscribe()`, and drive it with
  `send()`, `approve()`, `cancel()`, `retry()`, `edit()` and `clear()`.
  `snapshot()` returns the state with the `seq` of the batch it was cut on
  and each subscriber then receives `{"seq", "ops"}` batches to apply in
  order, so a late joiner replays from a snapshot and a gap
  (`seq != last + 1`) just means taking a fresh one. `deliver_on_main=True`
  posts a subscriber's batches through the dispatcher so they arrive on the
  host's main thread; the default runs the callback on the emitting thread,
  which must not block waiting on the main thread or call `wait_idle()` —
  `subscribe()` states both rules. Every method is safe to call from any
  thread and none blocks on the run; an approval parks nothing — the worker
  ends the paused run in the same locked section that sets `pendingApproval`,
  and `approve()` starts the resume as a new run, so a pending approval and an
  active run never coexist and a host answering from a delivery callback never
  has to "try again". `cancel()` denies a
  pending approval (`CANCELLED_APPROVAL_FEEDBACK`) rather than cancelling a
  run that is already over. Busy and closed states raise
  `ControllerBusyError` / `ControllerClosedError`.
- `talk2view.state` — the wire types the page consumes (`ChatSnapshot`,
  `DisplayMessage`, `ToolStep`, `PendingApproval`, `Op`, `OpBatch`) plus
  `apply_batch(snapshot, batch)`, which returns a new snapshot and raises
  `SeqGapError` (a subclass of `OpError`) when the batch does not follow.
- `Talk2View.set_chat_defaults(model=..., capabilities=...)` — bind the model
  and capabilities to the session rather than passing them per `chat()` call.
- Unknown values from the engine fail closed throughout the controller: an
  approval `kind` this SDK does not know is never auto-approved from the
  "always allow" memory (which holds client tool names only), a decision
  action outside `once`/`always` is a denial, and a `tool_end` status other
  than `ok` is never rendered as a call that succeeded.
- `CancelToken` / `Cancelled` and `Talk2View.stop()` — end an in-flight turn
  from any thread, including one blocked on a stalled read.
- Dispatchers (`qt_dispatcher`, `bpy_dispatcher`, `QueueDispatcher`,
  `TimerPumpDispatcher`, `run_on_main`) so tool handlers run on the host's
  main thread while the stream is consumed on a worker thread.
- Human-in-the-loop approvals: `approval_required` / `approval_result` events,
  `Decision`, `Talk2View.respond_to_approval()`, and per-tool
  `permission=True` / `permission=callback`.
- `Capabilities` — opt-in engine features (engine approvals, tool events,
  reasoning, image generation, timezone, surface). Everything is off by
  default and nothing is added to the request body for a flag that is off.
- New chat events: `reasoning`, `tool_event`, `error`.
- Session recovery: a chat session the engine has lost is recreated and the
  turn resent once, before any event has been yielded.
  Subscribe with `Talk2View.on_session_recovered()`.
- `T2VError.code`, and structured `detail` for FastAPI 422 validation errors.

### Fixed

- A `stop()` during the token refresh triggered by a 401 no longer aborts that
  refresh. The refresh reuses the stream's keep-alive connection, and aborting
  it after the server had rotated the refresh token left the client holding a
  consumed pair.
- A `CancelToken` closer can no longer run after its `unregister()` returned.
- Permission checks fail closed: an unrecognised action, a callback that
  raises, and a callback that returns the wrong shape all deny.
- Tool arguments that are not an object are reported to the model instead of
  raising out of `chat()`.
- `run_on_main()` honours an already-cancelled token before running a handler
  on the main thread.
- A token refresh is skipped when another thread has already rotated it, even
  when the request that got the 401 carried no `Authorization` header.
- A raising `on_session_recovered` listener no longer kills the recovered turn.

## [0.2.0b2] - 2026-10-12 (placeholder)

### Changed

- **The licence is MIT**, the same as the Talk2View TypeScript SDK. 0.2.0b1
  shipped with a proprietary licence.
- **The status is Pre-Alpha** (`Development Status :: 2 - Pre-Alpha`; it was
  Beta), and the `Typing :: Typed` classifier is gone: this is a placeholder with
  no API. The package summary now says so.
- **The documentation link** points to https://docs.talk2view.com instead of the
  engine's API reference.
- **The README** says what this package is, where to go until the Python SDK is
  released, and Talk2View's intended use and data handling.

## [0.2.0b1] - 2026-10-07 (placeholder, yanked)

### Added

- Reserve the `talk2view` name on PyPI with a placeholder that exports only
  `talk2view.__version__`.
