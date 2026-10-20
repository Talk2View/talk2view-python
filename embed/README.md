# @talk2view/sdk-embed

The chat page the Python SDK's `talk2view.ui.UIServer` serves to a desktop host's web view:
3D Slicer's `qSlicerWebWidget` (Chromium 83 on Windows and Linux, 87 on macOS), later
FreeCAD and Blender, or a system browser. Private: it ships only inside the `talk2view`
wheel, never on npm.

The page is a view. Python owns every piece of state: the page reads it from the SSE
`snapshot` and `ops` events and sends every action as `POST /command` (the UI server's wire
contract: `src/talk2view/ui/server.py` and `src/talk2view/ui/wire.py` at the repo root).

## Commands

Run from `embed/` (`cd embed`):

```bash
npm ci
npm run build          # the page -> ../src/talk2view/ui/static/ (git-ignored; the wheel carries it)
npm run build:guard    # the runtime guard's page -> dist-guard/
npm test               # unit tests (ops, SSE, transport, store, selectors, the dist guard)
npm run typecheck
npm run lint:css       # stylelint disallow-list + doiuse "chrome 83"
npm run test:e2e       # Playwright against the real UIServer (build AND build:guard first; needs `uv sync --all-extras` at the repo root)
(cd guard && python3 -m unittest test_guard_judge)   # the guard's judge, standard library only
```

Try it by hand against the scripted controller, no engine: `npm run build`, then
`cd .. && uv run python -m talk2view.ui.testing`.

## Layout

| Path | What |
|---|---|
| `src/wire.ts` | The state and command shapes |
| `src/ops.ts` | The op applier, a port of Python's `_apply_op` (shared cases: `../contract/ops/cases.json`) |
| `src/sse.ts`, `src/transport.ts` | Token bootstrap, `/events` over fetch + ReadableStream (EventSource `?t=` fallback), commands, uploads |
| `src/store.ts` | The page's copy of the state; the reactions no view owns (guest start, warm-up, re-send after a guest limit, notices) |
| `src/selectors.ts` | Pure derivations: the guest gate, the synthetic error message, copy for failures, whether an approval's arguments and "Always allow" can be shown |
| `src/runtime.ts` | assistant-ui's `useExternalStoreRuntime` over the store, through the vendored `convert.ts` |
| `src/vendor/sdk/` | The four TS SDK files the page imports, vendored (below) |
| `src/ui/` | The frame, thread, composer, approval card, Account and Settings views |
| `src/ui/displayText.ts` | Escapes bidi controls and other invisible characters in untrusted text to a visible `\uXXXX` form, for both the read-only views and the approval card's edit-arguments draft |
| `src/styles/app.css` | The only stylesheet: hand-written for Chromium 83 |
| `src/guard/` | The runtime guard's probes and scripted run (`build:guard` only) |
| `guard/` | The guard's Slicer driver and judge (`.github/workflows/embed-guard.yml`) |
| `e2e/` | Playwright specs and `backend.py`, the real UIServer they run against |

## Where the TS SDK code comes from

The page imports four files of the TypeScript SDK, never its React components (Tailwind v4
renders unstyled on Chromium 83). They are vendored under `src/vendor/sdk/` (the `@sdk/*`
alias) from the public [talk2view-typescript](https://github.com/Talk2View/talk2view-typescript)
repository, where the TS SDK lives and publishes; the build never reads a checkout of it. Each
file's header names the commit it was copied from and its local delta: none for
`ui/safeMarkdown.ts` and `constants.ts`, and this page's engine-approval and
tool-display additions for `assistant-ui/convert.ts` and `types.ts`, declared in full in the
`.delta.diff` beside each. CI runs `node scripts/check-vendored-sdk.mjs`: it fails when a
delta-free file differs from canonical `main`, a file is not its commit plus its delta, or
GitHub answers that a file is not there (moved, renamed or removed upstream), and warns
when canonical `main` has changed a file with a delta, or when GitHub cannot be reached.
To re-vendor a file, paste the canonical file below its header, set `vendored-commit` to
the commit you copied, re-apply and regenerate its `.delta.diff` if it has one (`patch`,
then `diff -u`), and run the check.

## Chromium 83

The stack was measured on Slicer's Chromium 83 before the page was built. What keeps it true:

- **Build.** `target`/`cssTarget` `chrome83`, `base: './'`, one JS chunk, no inline script,
  a self-hosted font, no source maps; `src/polyfills.ts` first.
- **CSS.** No flex `gap` (spacing is margins), no `:is`/`:where`/`:has`/`:focus-visible`
  (focus is plain `:focus`), no `@layer`, `inset`, `aspect-ratio`, container queries, `dvh`,
  `oklch` or `color-mix`. `npm run lint:css` checks the source; `scripts/check-dist.mjs`
  checks the built CSS (and `WeakRef`) on every build.
- **The runtime guard.** `.github/workflows/embed-guard.yml` runs the guard page in the
  official Slicer 5.12.4 builds for Linux and Windows, behind the real UIServer and
  `FakeController`, and fails on any probe, scripted step, uncaught error, or a userAgent
  that is no longer `Chrome/83`. CI's own `embed` job (`.github/workflows/ci.yml`) is a
  different check: it runs the page's unit and e2e suites in a normal Chromium, on every
  pull request into `main` and every push to `main`, and does not touch Slicer.

## Behaviour worth knowing

A few things a host developer should know that go beyond the wire contract:

- **Every untrusted plain-text surface is escaped; Markdown is not.** Text from the
  engine, a model, a tool, a file or a skill can mislead even as plain text: a
  right-to-left override can make a `.exe` read as a `.png`. Wherever such text is shown
  as plain text, in an element or an attribute, it goes through `displayText`
  (`src/ui/displayText.ts`), which turns bidi controls and other invisible or format
  characters into a visible `\uXXXX` escape. The approval card's "Edit arguments" draft
  gets the same through `escapeForJson`. Host-supplied copy (the `ChatOptions` text:
  welcome heading and suggestions, footer, a tool's activity and warning) is shown as
  given, since it comes from the party that serves the page and a Persian or Indic
  welcome needs its ZWNJ. **Among untrusted text, the one exception is Markdown: an
  assistant's reply and the agent's plan.** They render as sanitised HTML through
  `renderSafeMarkdown` (`src/vendor/sdk/ui/safeMarkdown.ts`), unescaped.
  Ordinary Persian, Indic, Hebrew and Arabic prose needs ZWNJ, joiners and direction
  marks, and escaping them would visibly break it. A reply can already make link text say
  anything, and it cannot act; the approval card, which escapes, is the security
  boundary. `e2e/plain-text.spec.ts` pushes one hostile string through the status line,
  the tool rows (name, arguments, result, image name and failure reason), the error card,
  the approval card's tool name, the sign-in refusal and email, the model labels and the
  skills, and fails if the raw character reaches the page; notices, the approval prompt and
  details, and attachment names have hostile-string specs of their own. It also checks
  that the host's copy keeps its ZWNJ.
- **The approval card fails closed.** Arguments it cannot faithfully show (not a plain
  object, or not JSON-serialisable) are never rendered as an empty `{}` with Allow still
  available — the card shows that the arguments could not be displayed and offers only
  Deny.
- **"Always allow" is offered only for a client tool the host has not marked
  `destructive`.** An engine approval, or any `kind` the page does not recognise, never
  gets the option at all (fails closed, not open).
- **Settings hides what a signed-out visitor cannot use.** Signed out, the model picker
  and the skills panel do not render at all, not merely disabled; text size stays
  available either way. A guest (an anonymous session with its own token) is authenticated
  and sees both.
- **The Account and Thread panes never unmount.** Visiting Settings, or the other pane,
  keeps a half-typed message and the email of a half-typed sign-in. The password is
  cleared whenever the Account pane is hidden or the identity changes. Only Settings
  itself unmounts on close, so its own effect can re-ask the skills list each time it
  opens.
- **A signed-out attach starts a guest first**, as a signed-out send does, when the
  integrator allows guests (otherwise the gate disables Attach). The upload waits for the
  guest, so the file is filed under the identity that sends it.
- **Attachments cap at 10 files per turn**, an upload aborts after 130 s
  (`transport.ts`'s `UPLOAD_MS`), and the size limit is 25 MiB by default, a `UIServer`
  constructor argument a host can change
  (`talk2view.ui.server.DEFAULT_MAX_UPLOAD_BYTES`, `max_upload_bytes=`) — the wire has no
  way for the page to state the limit in force, so a refusal is the first the end-user
  hears of it.
- **A guest's draft is reset on an identity change.** Switching identity — a guest
  replaced by a real sign-in, a sign-out, a new guest — clears a typed message and any
  chosen-but-unsent attachment, since neither belongs to the conversation that follows.
- **The page ends on a 401, an unexempted 403, or `stopped`.** Any of these from
  `/events`, `/command` or `/upload` means the chat is over for good; the page shows so and
  never retries. (A 403 `attachments_disabled` is exempt: the host simply does not take
  files, and the rest of the chat keeps working.)
