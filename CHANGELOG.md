# Changelog

All notable changes to the `talk2view` package are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- **On-device memory.** `Talk2View(memory_path=...)` gives the assistant a memory of the
  end-user that lives only on their device: one SQLite file per user (a guest too), `0600`.
  A `memory_path` directory the SDK has to create is made `0700`; a parent it has to create
  too gets the default mode, and an existing directory is left as found, so point
  `memory_path` at a directory under one that already exists. `t2v.memory` is the
  signed-in user's `T2VMemory`, and `t2v.memory.store` its `MemoryStore`; both are in
  `talk2view.memory`, with `MemoryEntry`, `Fact`, `SearchHit` and `AppliedBatch`. Each turn
  sends the user's entries, recent conversations and recalled passages as its `memory`
  field, the agent's `search_memory` calls are answered from the store, its `memory`
  events are applied to it, and each finished round is recorded in the store's log.
  `t2v.memory.last_applied` is the batch a `memory` event applied, and
  `t2v.memory.undo(batch_id)` takes it back within 30 seconds, for a "Memory updated"
  notice with an undo button. Extraction and consolidation run on a background thread
  (`run_due()` before every turn, a new conversation on `clear_session()`,
  `start_background()` for a host that can sit idle) and never hold up a chat. A failed
  call is silent: one log line, then a cooldown of 60 s that doubles up to an hour, which
  every queued call checks again as it starts. Incognito conversations and a pause switch
  send nothing, a guest's memory makes no engine call, and the user can export
  (`store.export_markdown()`, `store.export_json()`), import (`t2v.memory.import_text()`)
  and reset (`store.reset()`) their memory. The engine keeps no memory content, and there
  is no sync. See "On-device memory" in the README. Without `memory_path` nothing changes:
  no flag, no `memory` field, no handler.
- `Capabilities.memory` and `Capabilities.memory_search`, which `memory_path` sets; the
  `memory` chat event (`ChatEvent.type == "memory"`, with `tool_call_id` and
  `memory_ops`); and `T2VSession.send_message(memory=...)`, which sends the turn's memory
  field on `/messages` and on every `/resume`.
- The `memory` extra, `pip install 'talk2view[memory]'`: `apsw`, which bundles a SQLite
  with FTS5, on Python 3.11 and newer, for a host whose `sqlite3` lacks FTS5. Without
  either, the store keeps its search index in plain tables (`store.backend == "scan"`).

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
