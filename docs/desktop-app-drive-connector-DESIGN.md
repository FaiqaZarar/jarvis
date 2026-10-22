# Google Drive connector for the desktop app: design

**Status:** design only, per the project lead's direction. Nothing in
this doc is implemented yet.

**Source:** the project lead's direct instructions (this doc), plus
`jarvis/cmd/drive_ingest.py`, `jarvis/server/jarvis_server/connectors/drive/`
(`auth.py`, `listing.py`, `_constants.py`) and
`jarvis/modules/desktop_app/views/connectors_view.py`,
`settings_dialog.py`, `jarvis_server/internal/env_config.py` as they
exist today (read in full for this doc, not recalled from memory), plus
`docs/desktop-app-DESIGN.md` §1c/§2c, which already scoped this gap and
is extended, not repeated, here.

**Why this doc exists:** `ConnectorsView`'s "Connect Google Drive…"
button has been present-but-disabled since the desktop app's first
pass, with its own docstring flagging exactly this as deferred, not
dropped. The CLI's Drive ingest (`drive_ingest.py`) already works and
is proven; the goal here is reusing it from inside the GUI, not
rebuilding OAuth or Drive listing from scratch.

---

## 1. Verified current state

### 1a. What the CLI actually does, and which parts are daemon-routed

`drive_ingest.py main()`, in order:

1. `drive.get_drive_service()` - OAuth sign-in. **Not daemon-routed.**
   Reads a cached token (`.google_drive_token.json`) or, if missing/
   expired/unrefreshable, runs an interactive browser consent flow.
2. `pick_folder(service)` - an `input()`/`print()` loop over
   `drive.list_drive_folders()` (list a folder's direct sub-folders)
   and, once the user picks one, `drive.list_drive_files_recursive()`
   (a preview scan: "N items found, M directly ingestable"). **Not
   daemon-routed** - real, live Drive API calls made directly by the
   CLI process.
3. `daemon_client.run_ingest_drive(folder_id, ...)` - **the only
   daemon-routed step.** Everything above already decided *which*
   folder; this just hands the id to the daemon, exactly like
   `ConnectorsView._on_add_folder` already does for a filesystem path.

`drive_ingest.py`'s own module docstring states the reasoning
explicitly, and it holds for the GUI exactly as written: OAuth consent
has to open a browser and block in the *foreground* of whatever process
is running it, which a background daemon has no way to do on a
caller's behalf; pushing it behind a daemon endpoint would mean the
daemon owns Google OAuth token storage/refresh - scope creep in the
wrong direction. **The desktop app talks to `jarvis_server.connectors.drive`
directly, the same way the CLI does, for the same reason** - only the
final `run_ingest_drive` call goes through the daemon.

### 1b. The OAuth mechanism itself (`auth.py::get_drive_service`)

- Cached-token path: `Credentials.from_authorized_user_file(token_path,
  SCOPES)`, refreshed via `creds.refresh(Request())` if expired but a
  refresh token exists. Silent, no browser, no blocking beyond a normal
  HTTPS call - safe to call from anywhere.
- Fresh-consent path (no cache, or refresh failed): builds an
  `InstalledAppFlow` from `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`
  (env vars) and calls `flow.run_local_server(host="127.0.0.1", port=0,
  timeout_seconds=300)`. **This call is genuinely, synchronously
  blocking**: it starts a real local HTTP server on an OS-assigned port
  to catch the OAuth redirect, opens the system's default browser via
  `webbrowser.open()`, and does not return until the browser redirects
  back with an auth code (or 300s elapse). `allow_interactive_auth`
  exists specifically to fail fast instead of hanging a non-interactive
  caller - currently defaults to `sys.stdin.isatty()`, which is `False`
  for a GUI process with no attached console (confirmed reasoning, not
  yet confirmed empirically against a real frozen build - flagged in
  §7). The GUI must pass `allow_interactive_auth=True` explicitly,
  since "is a real terminal attached" is the wrong question for a
  windowed app that has its own, different way of being interactive.

### 1c. Two real gaps for a packaged build, not yet hit by the CLI

- **Token cache path.** `DEFAULT_TOKEN_PATH = Path(__file__).resolve().
  parents[5] / ".google_drive_token.json"` - a repo-relative path.
  Exactly the same class of bug `env_config.py`'s own docstring already
  documents and fixes for `.env`/`EMBEDDING_API_KEY` ("`_spawn_daemon()`'s
  subprocess `cwd` doesn't resolve to anything meaningful once frozen"):
  in a PyInstaller `onedir` build, `__file__` resolves inside the
  `_internal` bundle, and walking five parents up from there lands
  somewhere inside (or above) the install directory, not a durable,
  writable, per-user location. This has never been exercised in a
  packaged build (Drive ingest isn't wired into either the CLI's own
  packaged entrypoint story or the GUI yet) - it's a latent bug, not a
  reported one, caught by reading the code against what §1c already
  identified as a requirement.
- **OAuth client id/secret.** Currently `.env`-only
  (`GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`, read via
  `os.environ` in `_client_config_from_env()`). `env_config.load_env()`
  already loads a packaged app's `<JARVIS_DAEMON_HOME>/.env` as a
  fallback (the same mechanism `EMBEDDING_API_KEY` uses) - so these two
  vars would *already* be picked up automatically if present there,
  with zero new plumbing. §4 covers the confirmed decision (bundle,
  not end-user-configured) and the resulting implementation shape.

---

## 2. Threading: OAuth consent and folder listing from inside a Qt event loop

**The concrete question the project lead asked:** does the OAuth flow's
local redirect server need special handling to not block the GUI
thread, the same way daemon calls already run on worker threads?

**Yes, and the existing pattern already covers it - almost.**
`flow.run_local_server(...)` (§1b) is a plain, synchronous, blocking
Python call from the caller's point of view - it has no concept of Qt,
asyncio, or threads of its own; it just blocks the calling thread until
done. That makes it mechanically identical to every `daemon_client.*`
call this app already routes through `run_daemon_call`
(`daemon_worker.py`): a synchronous blocking function, submitted to
`QThreadPool`, delivering its result back to the GUI thread via a Qt
signal. `run_daemon_call` itself has no daemon-specific logic in its
implementation - it takes any `Callable`, none of it assumes the
callable talks to the daemon. The straightforward move is to call
`get_drive_service` (and, separately, each `list_drive_folders`/
`list_drive_files_recursive` call the picker dialog makes) through this
exact same helper.

**Two things this reuse does *not* automatically get right, worth
being explicit about rather than discovering by accident:**

- **The helper's name.** `run_daemon_call` works for a non-daemon
  callable today (nothing in its implementation checks), but the name
  says otherwise to a future reader. Two honest options: reuse it
  as-is (zero new code, a slightly misleading name at two or three call
  sites) or add a thin, identically-shaped alias/rename (e.g.
  `run_background_call` in `daemon_worker.py`, with `run_daemon_call` as
  a one-line wrapper for existing callers, or vice versa) purely for
  naming clarity. Functionally identical either way - flagged as a
  judgment call for whoever reviews the implementation, not a design
  fork worth deciding now.
- **The browser opening is not the blocking part - the wait *after* it
  is.** `webbrowser.open()` (called inside `run_local_server`) returns
  immediately; what actually blocks is the local HTTP server waiting
  for the redirect. This means: from the moment "Connect Google
  Drive…" is clicked, the browser opens almost immediately (good, the
  user sees something happen right away), but the worker thread stays
  busy for however long the user takes in the browser - up to the
  existing 300s `_OAUTH_LOCAL_SERVER_TIMEOUT_S`. The GUI needs its own
  "waiting for you to finish signing in in your browser…" state for
  this whole window (§3), separate from and longer-lived than the
  busy-button pattern the redesign just added for ingest (docs/
  desktop-app-ux-redesign-DESIGN.md §4) - reusable in spirit (disable
  the button, show a status banner), not the same literal instance
  (this isn't polling a phase endpoint; it's one long-running call with
  exactly two outcomes - consent completed, or timed out/cancelled).
- **Cancellability.** `run_local_server` has no built-in "cancel this
  from outside" hook - once called, it runs until success or its own
  300s timeout. A user who closes the browser tab without finishing, or
  who clicks away, has no way to abort early from the GUI side today;
  they'd wait out the timeout. Worth deciding whether that's
  acceptable for v1 (simplest - do nothing extra) or whether the dialog
  should offer its own "Cancel" that can at least stop *waiting on* the
  worker's result (the worker thread itself would still run to
  completion or timeout in the background, since Python can't forcibly
  interrupt a thread stuck in a blocking C-level socket accept) - see
  open questions, §7.

---

## 3. Proposed UI

### 3a. Entry point - "Connect Google Drive…"

Currently a permanently-disabled `QPushButton` in `ConnectorsView` with
a tooltip explaining it's unimplemented. Proposed flow on click:

1. **Enable the button, wire it to a handler** that immediately shows a
   busy state ("Connecting to Google Drive…") and calls
   `get_drive_service` via the threading pattern in §2, with
   `allow_interactive_auth=True` explicitly passed (§1b).
2. **On success** (cached token was valid, or fresh consent completed):
   open the folder-browser dialog (§3b) with the now-authenticated
   `service` object.
3. **On failure** (`RuntimeError` - missing client id/secret, a
   rate-limited listing, or a genuine OAuth error; `TimeoutError`-shaped
   failure from `run_local_server`'s own 300s bound): the same modal
   error-dialog pattern `show_ingest_error_dialog` already uses for a
   write-action failure (design doc's own §5 reasoning for why write
   actions get a modal, not a banner, applies identically here - losing
   track of "did I actually get connected" is worse than an extra click
   to acknowledge).

### 3b. The folder-browser dialog

A new `QDialog`, replacing `pick_folder()`'s `input()`/`print()` loop
with the same navigation model, Qt-native:

- A single `QTreeWidget` (or a `QListWidget` with an "up one level" /
  breadcrumb row - a `QTreeWidget` is the more direct fit, and this app
  already has a working precedent for exactly this "lazy-load children
  on expand" shape in `HierarchyView`) showing the current folder's
  direct sub-folders, lazily loaded via `list_drive_folders(service,
  parent_id)` on expand - mirroring `HierarchyView`'s own
  `itemExpanded` -> daemon-call -> populate-children pattern, except
  each call here goes through the §2 background-thread helper directly
  against `jarvis_server.connectors.drive`, not `daemon_client`.
- Breadcrumb / current-location label, matching `pick_folder()`'s
  `"Location: My Drive / Sub A / Sub B"` line - cheap, already proven
  useful in the CLI.
- A "Select this folder" button (the CLI's `0`), enabled once a folder
  is selected (root or any expanded node) - **not** gated on the
  recursive preview scan finishing first; that scan is a nice-to-have
  confirmation, not a precondition, and gating "Select" on a
  potentially-slow recursive walk over a large Drive folder would just
  add a second waiting state for no real benefit.
- **The recursive preview** (`list_drive_files_recursive`, "N items
  found, M directly ingestable") - proposed as an *optional* summary
  shown after "Select this folder" is clicked, in the same confirmation
  moment the CLI already uses it for ("Ingest 'X'? This downloads and
  indexes its files"), not as a live count while browsing. Also run
  through the §2 background-thread helper - it's a real, potentially
  slow Drive API walk over an arbitrary sub-tree.
- On confirmation: close the dialog, hand `(folder_id, folder_name)`
  back to `ConnectorsView`, which calls `daemon_client.run_ingest_drive`
  through the *exact* `_start_ingest` path the redesign just built for
  filesystem ingest (docs/desktop-app-ux-redesign-DESIGN.md §4) -
  `_start_ingest` already takes `(ingest_fn, *args)` generically, so
  `self._start_ingest(daemon_client.run_ingest_drive, folder_id)` is a
  direct, symmetric reuse, not a second implementation of the
  busy-button/progress-bar/status-polling machinery.

### 3c. What's explicitly *not* proposed for v1

- No Drive-side search/filter in the picker - the CLI doesn't have one
  either; matching it, not exceeding it, for a first pass.
- No "disconnect" / token-revocation UI - `.google_drive_token.json`
  can already be deleted by hand (as it can today for the CLI); a
  "Sign out of Drive" button is a reasonable fast-follow, not a launch
  requirement.
- No progress feedback *during* the Drive ingest itself beyond what
  `run_ingest_drive` already gets for free via §3b's reuse of
  `_start_ingest` (the redesign's phase-polling already works for any
  connector's ingest call, including Drive's, unchanged) - no extra
  design needed here.

---

## 4. Config/token storage for a packaged build

Both real gaps from §1c get the same fix, matching the precedent
`env_config.py`/`SettingsDialog` already established for
`EMBEDDING_API_KEY`:

- **Token path**: change `auth.py`'s `DEFAULT_TOKEN_PATH` to resolve
  against `JARVIS_DAEMON_HOME` (falling back to the current
  repo-relative behavior only when unset, i.e. unpackaged dev mode) -
  the same override precedent `jarvis_server.internal.db.connection`
  and `graph_db.py` already use for `jarvis.db`/the graph file. A
  one-line change in shape, though it touches a function signature
  used by tests (`get_drive_service(token_path=DEFAULT_TOKEN_PATH,
  ...)`), so it's implementation work, not zero-risk - flagged for the
  implementation pass, not decided further here.
- **Client id/secret: decided - bundle into the build.** Confirmed by
  the project lead. Rejected alternative: user-supplied, extending
  `SettingsDialog` with two more fields (mirroring the existing
  `EMBEDDING_API_KEY` pattern exactly) - reuses 100% of the existing
  save/load machinery, but means *each tester* would need their own
  Google Cloud OAuth client, which doesn't actually fit "one shared
  client, several test users" (the existing `.env.example` setup §5
  assumes) and is a materially worse onboarding experience than the
  CLI's current "put two values in .env once" story.

  Bundling works specifically because this is a "Desktop app" OAuth
  client type - Google's own documentation for this type does not
  treat the client secret as confidential (unlike a server-side "Web
  application" client), since an installed/desktop app's secret can't
  be kept truly hidden from its own user anyway. A bundled secret being
  visible to anyone who inspects the build is therefore an accepted
  property of this client type, not a leak to mitigate.

  **Implementation shape - revised after a real finding.** The original
  plan was to bake the real values directly into `env_config.py`'s
  `_BUNDLED_GOOGLE_OAUTH_CLIENT_ID`/`_SECRET` constants (a build-time
  default `_apply_bundled_google_oauth_client()`, called at the end of
  `load_env()`, falls back to only when neither dev-mode `.env` nor
  `<JARVIS_DAEMON_HOME>/.env` already set them - that mechanism is
  built and tested). Attempting that surfaced a real, distinct risk
  this section's own "Google doesn't treat it as confidential"
  reasoning doesn't cover: committing the actual secret into *tracked
  source* puts it in git history permanently, a strictly larger blast
  radius than a compiled binary - confirmed directly, not
  hypothetically, when Claude Code's own credential-leakage safeguard
  refused to let a populated version of that file be committed at all.

  **Actual fix for the current, small tester group (the core team,
  §5): add `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`
  directly to each tester's own `<JARVIS_DAEMON_HOME>/.env`** - the
  same file `SettingsDialog`/`save_settings()` already writes
  `EMBEDDING_API_KEY` into, and a real `.env` value already takes
  priority over the (still-blank) bundled fallback, so this needs zero
  code changes and nothing checked into git. This is what "one shared
  OAuth client" already meant in practice for the CLI too - a value
  handed to trusted testers directly, not a public secret.
  `_BUNDLED_GOOGLE_OAUTH_CLIENT_ID`/`_SECRET` stay as the documented
  extension point for if per-machine `.env` edits ever stop scaling
  (e.g. a genuinely public release) - at which point the real values
  belong in a build-time injection (a local, gitignored file, or a CI
  secret), never committed into this file directly.

---

## 5. The shared OAuth client: does it block shipping this to real testers?

**Confirmed constraint (from `.env.example`'s own documentation and
`auth.py`'s behavior, not re-derived here):** the OAuth consent screen
is in Google's "Testing" publishing status. Two consequences:

1. **Every Drive user must be added, by email, as a test user** on the
   OAuth consent screen in Google Cloud Console before they can
   complete consent at all - an *allow-list*, not something a tester
   can bypass or request access to themselves.
2. **Tokens expire in ~7 days** in Testing mode regardless of the
   `refresh_token`/`creds.refresh()` machinery already in place -
   `auth.py` already handles this gracefully (falls back to a fresh
   interactive consent, with a clear "your authorization has expired"
   message), but it means **every added tester needs to re-consent
   roughly weekly**, indefinitely, for as long as the app stays in
   Testing mode.

**Does this block shipping to real testers?** Depends entirely on who
"real testers" means:

- **For the core team** (already-known individuals who can be
  added as test users directly, and who are already the CLI's actual
  users of this exact flow today): **not a blocker.** This is the
  status quo the CLI already ships with; the GUI reusing the identical
  OAuth mechanism doesn't make it worse, and the weekly re-consent is
  already a known, accepted cost for this group.
- **For anyone outside a manually-maintained test-user list** (a wider
  beta, an app-store-style release, "anyone who downloads the exe"):
  **yes, this blocks it**, for two independent reasons neither of which
  this design doc's scope can resolve on its own:
  1. Google's Testing-mode cap is 100 test users, each added by email
     individually - not a self-serve signup, and not something that
     scales to an unknown or growing tester list.
  2. Moving out of Testing mode into Google's "In production" /
     verified-app status is a **separate, non-trivial process**
     (app verification, a privacy policy URL, possibly a security
     assessment depending on the scope requested) that this doc
     deliberately does not scope - it's a project/business decision
     with real lead time, not an engineering task this connector's
     implementation can shortcut.

**Recommendation:** ship the GUI Drive connector for the
already-known-tester group (the core team) now, exactly as the CLI
already does - the weekly re-consent friction is unchanged, not
introduced by this feature. Treat "move off Testing mode" as an
explicitly separate, later decision with its own lead time, not a
prerequisite for this design's implementation.

---

## 6. Risk/effort sequencing

**Moderate, contained (the core of this feature):**
- The folder-browser dialog (§3b) - new UI, but directly modeled on
  `HierarchyView`'s existing lazy-expand pattern; no new interaction
  idiom to invent.
- Wiring "Connect Google Drive…" through the §2 threading pattern -
  reuses `run_daemon_call`'s existing mechanics (possibly under a
  clarified name, per §2's naming note).
- The token-path fix (§4) - small, contained, but touches a public
  function signature and its existing tests.

**Decided, resolved differently than first planned:**
- The shared-client bundling question (§4) - the underlying goal
  (every current tester working with no manual Cloud-project setup) is
  resolved: add the two values to each tester's own
  `<JARVIS_DAEMON_HOME>/.env` directly. Not resolved the way originally
  planned (baking the real secret into tracked source) - that path was
  tried and correctly blocked, see §4's own writeup.

**Genuinely a separate decision, still open:**
- Moving off Testing mode (§5) - explicitly out of scope for this
  connector's implementation; a project-level decision with its own
  timeline.

**Smaller, safe to defer past v1 (§3c):**
- Cancel-mid-consent handling (§2's cancellability note).
- Sign-out/token-revocation UI.
- Any Drive-side search/filter in the picker.

Recommend building the folder-browser dialog and the OAuth-threading
wiring together as one pass (they're not independently useful - the
button needs both to do anything), fixing the token path alongside it
since it's small and directly adjacent, and bundling the client
id/secret (§4) as part of the same pass now that it's a decided,
contained piece of work rather than an open question blocking it.

---

## 7. Open questions

Resolved since the first draft: the OAuth client id/secret is bundled
into the build (§4), decided by the project lead - no longer open.

1. **`sys.stdin.isatty()` as `get_drive_service`'s interactivity
   gate** - confirmed by reasoning that this reads `False` for a GUI
   process with no attached console, so the GUI must pass
   `allow_interactive_auth=True` explicitly; not yet confirmed
   empirically against a real frozen build (no console at all,
   `sys.stdin` may not even be a real stream) - worth a direct check
   during implementation, not assumed safe.
2. **Cancel-mid-consent (§2)** - accept "no early cancel, wait out the
   300s timeout" for v1, or invest in a cancel affordance that can at
   least stop the GUI from waiting on the worker (not the underlying
   blocked thread itself, which can't be forcibly interrupted)?
3. **`run_daemon_call`'s name (§2)** - reuse as-is for a non-daemon
   Drive call, or introduce a differently-named alias for clarity? Pure
   naming/readability call, no functional difference.
4. **Who actually gets Drive access in the near term** - confirmed
   this doesn't block the core team; is there any nearer-term plan to
   add more test users (still within Testing mode's 100-user cap and
   the by-email allow-list process) before the larger "move off
   Testing mode" decision gets made?
