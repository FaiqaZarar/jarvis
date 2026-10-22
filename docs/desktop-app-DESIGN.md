# Desktop app (Mac/Windows): design

**Status:** design only, per the project lead's direction, following the
same "design before code" convention already used for
`docs/local-server-architecture-DESIGN.md` and
`docs/organize-checkpointing-DESIGN.md`. Nothing in this doc is
implemented yet.

**Confirmed scope for v1:** search, browse the topic hierarchy, and
manage connectors/ingest - not a minimal search box. **Tech stack:**
PySide6 (not PyQt6, for its permissive LGPL license - PyQt6 is GPL/
commercial-dual-licensed, which is a real constraint for anything
redistributed as a packaged app).

**One thing worth surfacing up front, not silently glossed over:**
`docs/local-server-architecture-DESIGN.md` (§3) reasoned "the desktop
app is very likely not Python" as part of why it chose HTTP/JSON over a
Python-specific IPC mechanism like `multiprocessing.connection`. That
speculation is now moot - the project lead has settled on Python/PySide6
- but it doesn't change anything about the daemon's design: HTTP/JSON
over loopback works identically for a Python client, which is exactly
what `jarvis/cmd/mcp_server.py` and the CLI ingest entrypoints already
prove today. Noted for continuity, not as a conflict to resolve.

---

## 1. Verified current state (read the real code, not memory)

### 1a. The daemon's HTTP API - confirmed complete for v1's scope

Read `jarvis/modules/daemon/server.py` directly. Its full route table:

```
Route("/health", health)
Route("/search", api_search, methods=["POST"])
Route("/get_hierarchy", api_get_hierarchy, methods=["POST"])
Route("/browse", api_browse, methods=["POST"])
Route("/related", api_related, methods=["POST"])
Route("/query_db", api_query_db, methods=["POST"])
Route("/ingest/filesystem", api_ingest_filesystem, methods=["POST"])
Route("/ingest/drive", api_ingest_drive, methods=["POST"])
```

This is exactly what the earlier design doc said exists, confirmed
against the actual file, not recalled. **Nothing is missing for v1's
three confirmed features** (search, hierarchy browsing, connector/ingest
management) - every endpoint v1 needs already exists:

- Search -> `/search` (`api_search`, wraps `search_mod.search`).
- Hierarchy browsing -> `/get_hierarchy` (top-level forest or a subtree)
  and `/browse` (one node's children one level deep + its direct-member
  chunks, paginated via `next_cursor`).
- Ingest -> `/ingest/filesystem` and `/ingest/drive`, both wrapping
  `pipeline.run_ingest()` exactly as the CLI does, serialized against
  each other through the daemon's own `_WRITE_LOCK`.

**What does NOT exist, confirmed by reading every route**: any
progress-reporting, streaming, or job-status endpoint. `/ingest/*` is a
single blocking `POST` that returns once `run_ingest()` has completely
finished - no partial progress, no way to poll "how far along is this."
This directly shapes §7's open question on ingest progress - it isn't a
UI decision alone, it's gated on whether the daemon grows a new
capability.

### 1b. The daemon client wrapper - directly reusable, not a second HTTP client to write

Read `jarvis/modules/daemon/client.py` in full. It already provides
exactly the seven calls v1 needs, as plain, typed Python functions:
`search`, `get_hierarchy`, `browse`, `related`, `query_db`,
`run_ingest_filesystem`, `run_ingest_drive` - the same functions
`jarvis/cmd/mcp_server.py`'s tool proxies and the CLI ingest entrypoints
already call. This is not "close enough to reuse with changes" - it's a
direct match: same daemon, same wire format, same auto-start and
error-handling behavior already built and tested.

**One real architectural fact this reuse surfaces, not present in any
CLI caller so far:** every one of these functions is a **synchronous,
blocking call** (`requests.request(...)`, no `async`). The CLI and
`mcp_server.py` can call them directly because neither has a UI event
loop to keep responsive. A Qt application does - calling `daemon_client.search(...)`
directly from a button's click handler would freeze the whole window for
the duration of the HTTP round-trip (and, for `ensure_daemon_running()`'s
auto-start path, potentially the full 15s startup timeout). **This is
the one place v1 needs to add something the CLI never needed**: every
call into `daemon_client` from the GUI thread must go through a worker
thread (Qt's `QThreadPool`/`QRunnable`, or a `QThread` per call), with
the result delivered back to the GUI thread via a Qt signal - not a
different client, just a threading wrapper around the exact same
functions. Confirmed by reading `client.py`'s `call()`: nothing about it
is already async-safe or non-blocking in a way that would make this
unnecessary.

### 1c. Folder-picking precedent - the underlying calls are reusable, the CLI's own picker UI is not

**Filesystem**: the CLI (`jarvis/cmd/filesystem_ingest.py`) takes a
plain path argument - there's no picker to reuse or replace, and no gap
either. `QFileDialog.getExistingDirectory()` is Qt's direct, standard
equivalent; nothing daemon- or connector-specific stands in the way.

**Drive**: `jarvis/cmd/drive_ingest.py`'s `pick_folder()` is a real,
working interactive picker (list sub-folders, open one by number, select
with `0`, back with `b`) - but it's built entirely on `input()`/`print()`,
so the function itself is not portable to a GUI. What **is** reusable
underneath it: `drive.get_drive_service()` (OAuth sign-in, opens a
browser - unchanged, needed either way) and `drive.list_drive_folders()`/
`drive.list_drive_files_recursive()` (the actual Drive API calls
`pick_folder()` is built on). v1 needs a new, Qt-native folder-browser
dialog (a simple tree/list widget navigating parent/child folders via
these same two functions) - a UI rewrite of `pick_folder()`'s
interaction model, not a rewrite of anything Drive-specific. Confirmed
by reading `drive_ingest.py`'s own module docstring: "OAuth sign-in and
folder listing/picking ... stay local to this CLI" - the same statement
holds for the desktop app, just with a different picker UI on top.

---

## 2. App structure

Three primary views, matching the three confirmed v1 features, plus a
persistent daemon-status indicator (§5) that isn't a fourth view so much
as a strip present across all three.

### 2a. Search view

A query box, a filter affordance for `SearchFilters` (topic, source,
date range, file type - all four already exist on the wire, per
`models.py`), and a results list bound to `SearchResult.results`
(`list[ChunkHit]`) - each row showing the chunk's text preview, its
resolved topic (`ChunkHit.topic`, nullable), and its source document
(`ChunkHit.document`/`sources`). `next_cursor` drives a "load more"
action rather than full re-pagination, matching how `search()` already
expects to be called.

### 2b. Hierarchy view

A `QTreeView` is the natural fit for `TopicNode`'s actual shape - it's
explicitly a tree with a tri-state `children` field (`None` = not
expanded here, `[]` = expanded and empty, a list = expanded and
populated), which maps directly onto Qt's own lazy-population model
pattern instead of needing to invent one:

- Initial load: `get_hierarchy(root=None)` populates the top-level
  forest (children of the hidden synthetic root).
- Expanding a node the tree hasn't loaded children for yet: `browse(topic_id)`
  returns that node's own children one level deep, **plus** its
  direct-member chunks (paginated) - so expanding a topic in the tree is
  also exactly the call that populates a chunk list for "what's directly
  in this topic," with no second round-trip needed for that pane.
- A custom `QAbstractItemModel` subclass wrapping `TopicNode`/`ChunkHit`
  (not the stock `QStandardItemModel`, since lazy loading via
  `canFetchMore`/`fetchMore` needs a real model, not a pre-built static
  tree) is a reasonable, standard fit here.

### 2c. Connectors / ingest view

Not a single "run ingest" button - a small management surface:

- **Filesystem**: "Add a folder" -> `QFileDialog.getExistingDirectory()`
  -> `run_ingest_filesystem(path)`.
- **Drive**: "Connect Google Drive" (triggers `drive.get_drive_service()`'s
  OAuth flow, which opens a system browser - unchanged from the CLI) ->
  the new folder-browser dialog from §1c -> `run_ingest_drive(folder_id)`.
- A list of what's already been ingested (source, when, rough size) -
  there's no dedicated "list connectors" endpoint today, but this is
  answerable from `query_db()` against `document_sources`/`connector_state`
  (already exposed, read-only, exactly as the debug UI already does
  similar ad hoc queries) rather than needing a new daemon route.
- Per-ingest outcome display: `run_ingest_filesystem`/`run_ingest_drive`
  already return the same stats dict `print_ingest_summary()` renders
  for the CLI (pulled/skipped/errors, chunks created, embed stats,
  hierarchy stats) - the GUI's summary panel is a direct visual
  translation of that dict, not a new data need.

---

## 3. How the app talks to the daemon

**Reuse `jarvis.modules.daemon.client` directly - do not write a second
HTTP client.** Confirmed in §1b: it already has every call v1 needs,
with the daemon-unreachable handling, auto-start, and wire-format
parsing already built and already tested (`tests/test_daemon.py`,
`tests/test_mcp_server.py`, `tests/test_daemon_error_handling.py`). The
only new code this app needs at the transport layer is the threading
wrapper from §1b - a thin layer that calls the existing typed functions
on a worker thread and marshals the result back via a Qt signal, not a
new request-building or error-handling layer.

---

## 4. Daemon lifecycle from the app's perspective

**Recommendation: auto-start on launch, reusing `ensure_daemon_running()`
exactly as built - do not invent a new mechanism, and do not assume the
daemon is already running.** This is already the pattern the CLI and MCP
proxy both use (auto-start-on-first-use, no separate "start the daemon"
step, design doc's resolved decision #1/#4) - a desktop app is,
architecturally, just another client, and there is no reason for it to
behave differently. Concretely: call `ensure_daemon_running()` once
(off the GUI thread, per §1b) shortly after the app window opens, before
enabling any daemon-dependent UI, rather than waiting for the user's
first search/browse/ingest action to discover whether a daemon exists.

**A real gap this recommendation surfaces, not yet handled by any
existing code:** `jarvis.modules.daemon.client._spawn_daemon()` spawns
`[sys.executable, "-m", "jarvis.cmd.daemon"]` (confirmed by reading it).
Inside a **packaged, frozen** app (PyInstaller/Briefcase - §6),
`sys.executable` points at the packaged app's own binary, not a general
Python interpreter - `-m jarvis.cmd.daemon` would not run the daemon
module; at best it would just relaunch the same packaged app a second
time with an unhandled extra argument. **This is not solved by anything
built so far** - every existing caller of `_spawn_daemon()` (the CLI,
the MCP proxy) runs from a real, unfrozen Python interpreter, where
`sys.executable -m <module>` genuinely works. The desktop app is the
first client that runs from a frozen binary, and this needs a real
decision before implementation, not an assumption that today's spawn
code "just works" there too. Two real options, not evaluated further
here since this doc is design-only:

1. **One frozen executable, argv-dispatched.** The packaged app's own
   entry point checks for a `--daemon` (or similar) argument at startup
   and, if present, runs `jarvis.modules.daemon.server.main()` instead
   of the GUI - `_spawn_daemon()` (or a desktop-app-specific override of
   it) spawns `[sys.executable, "--daemon"]` instead of `-m jarvis.cmd.daemon`
   when running under a frozen build (`sys.frozen`, PyInstaller's own
   standard flag for this exact check). One binary to build and sign.
2. **Two frozen executables, shipped side by side.** The GUI and the
   daemon are packaged as separate binaries in the same install
   directory/app bundle; a frozen-aware `_spawn_daemon()` variant points
   at the daemon binary's own path instead of `sys.executable`. Simpler
   spawn logic, but two things to build, sign, and notarize instead of
   one.

Flagged here and again in §7 - picking between these needs input beyond
what this design doc can resolve alone (signing/notarization cost is a
real factor for option 2, on top of implementation complexity).

**RESOLVED (v1 implementation): option 1, one frozen executable,
argv-dispatched.** Built exactly as described above - see §8.1 for what
actually changed and how the dispatch was verified without needing a
real PyInstaller build yet.

---

## 5. Error handling

**Reuse the existing three-tier hierarchy and its messages - do not
reinvent the taxonomy, only the presentation.**
`jarvis.modules.daemon.client` already defines `DaemonStartupError`
(tier 1: auto-start failed), `DaemonConnectionError` (tier 2: connection
refused/reset or a non-JSON response), and `DaemonTimeoutError` (tier 3:
timed out) - each with a calm, pre-written user-facing message and
exactly one automatic retry per real request before tiers 2/3 are
raised (`docs/local-server-architecture-DESIGN.md` §10, already
implemented and tested). This is the same pattern
`jarvis/cmd/mcp_server.py` was retrofitted to use instead of leaking a
raw exception.

What genuinely differs for a GUI, and needs new (thin) code, not a new
design: **presentation, not classification.** The CLI prints
`_USER_MESSAGES[type(exc)]` to stderr; `mcp_server.py` returns it as an
MCP `ErrorResponse`. Neither is right for a desktop app - the natural
translation is a **non-blocking banner or status-bar message** for a
read action (search/browse - the action just shows "couldn't reach the
background service, try again" and leaves the rest of the UI usable),
and a **modal dialog** for a write action (ingest) that failed outright,
since silently losing "did my ingest actually run" is worse for a
multi-second-to-multi-minute operation than an extra click to
acknowledge. Both cases read the exact same three exception types and
the exact same pre-written messages - only the widget they're shown in
changes per action type.

**One real, pre-existing behavior worth being explicit about, not new to
this app:** `daemon_client.call()`'s default timeout is 120 seconds,
applied uniformly to reads *and writes* - confirmed by reading
`run_ingest_filesystem`/`run_ingest_drive`, neither of which overrides
it. A real ingest (embedding + LLM-driven organize) can plausibly run
longer than 120s on a large folder. If it does, the one-retry-on-timeout
behavior means the client fires a **second** `POST /ingest/filesystem`
while the daemon's `_WRITE_LOCK` is still held by the first, in-flight
one - the retry simply queues behind it and, once the lock frees, starts
a second, largely-redundant ingest of the same folder (not unsafe -
`run_ingest`'s own pull step dedupes unchanged content - but wasteful,
and a confusing thing for a user to observe as "it ran twice"). This
already exists identically for the CLI today; it isn't introduced by the
desktop app. Worth flagging here because a GUI's ingest action is more
likely to be pointed at a genuinely large folder than the CLI's typical
use so far - see §7.

---

## 6. Packaging

**Recommendation: PyInstaller** - confirmed, on Windows, by a real
packaging spike (below), not just reasoning about it. Mac is still
unverified.

Reasoning:

- **The daemon's dependency stack is the real risk, and PyInstaller has
  the deeper track record against it.** The daemon this app spawns
  imports `umap-learn`, `hdbscan`, `numba`, `llvmlite`, `scipy`,
  `scikit-learn` (per `requirements.txt`'s J-08 section) - compiled and
  JIT-heavy packages that are a known source of packaging pain for
  *any* freezer, not specific to one tool. PyInstaller has a much larger
  body of community precedent and existing hooks for exactly this class
  of scientific-Python dependency than Briefcase does; that's a real
  reason to prefer it here, not a claim that either tool "just works"
  without verification.
- **Briefcase's strength - more idiomatic native packaging output,
  closer platform integration for signing/notarization - matters less
  here than dependency-bundling reliability does**, given how much of
  this app's actual runtime weight is the daemon's ML stack rather than
  the GUI itself.
- **Two artifacts, two tools either way.** Neither PyInstaller nor
  Briefcase produces a `.dmg` directly from a single command without
  extra tooling on top (PyInstaller produces a `.app`; wrapping it in a
  `.dmg` needs `create-dmg`/`dmgbuild` or similar, same as most
  PyInstaller-based Mac distributions do today). This is a real, small
  extra step either way, not a point in Briefcase's favor once weighed
  against the dependency-bundling risk above.

**RESOLVED on Windows (real build-and-run spike, not just reasoning about
it): yes, this dependency stack freezes and runs correctly with
PyInstaller** - including a real, complete ingest through `numba`-JIT'd
`umap-learn`/`hdbscan` clustering, an LLM-driven reconcile, and a real
LadybugDB graph write, all inside a frozen executable. Mac is still
unverified (no Mac available to test on directly, per the task) and
should not be assumed to behave identically - PyInstaller's binary
handling is platform-specific enough that this spike's fixes (below) are
Windows-specific findings, not guaranteed to transfer as-is.

**What was actually built and run**, from `jarvis/cmd/desktop_app.py`
(the real argv-dispatched entrypoint, not a test script) via
`packaging/jarvis_desktop.spec` (onedir, console build - kept in
the repo as the reproducible recipe):

1. `jarvis_desktop.exe` launches, and `ensure_daemon_running()` correctly
   spawns the daemon via the frozen `[sys.executable, "--daemon"]` path
   (`getattr(sys, "frozen", False)` - confirmed true, not the unfrozen
   `-m jarvis.cmd.daemon` path, which would be broken here) - two
   separate `jarvis_desktop.exe` processes, one GUI and one daemon,
   exactly as §4's design intends.
2. The frozen daemon subprocess starts uvicorn successfully, meaning
   `numba`, `llvmlite`, `umap-learn`, `hdbscan`, `scipy`, and
   `scikit-learn` all *import* successfully frozen - the first real
   risk, confirmed clear on the very first build attempt (see below).
3. A real search (`POST /search`) returned real, correctly-ranked hybrid
   BM25+vector results, including a genuine network call to
   `openrouter.ai` for the query embedding (confirmed in the daemon
   log's own `Embedding batch succeeded` line) - proves the search path
   end to end, not just that the process didn't crash.
4. A real ingest (`POST /ingest/filesystem` against `docs/`, with
   `organize_hierarchy=True`) completed with status 200 and a genuine
   result - `49 topics, 51 live after reconcile, 43 born / 41
   tombstoned / 2 merge / 2 split / 6 reparent` - meaning
   `numba`-JIT-compiled UMAP + HDBSCAN clustering actually *ran* (not
   just imported) across four clustering passes (base n=214, then
   rollups at n=36, n=8, n=4), the LLM-driven reconcile/matching logic
   ran, and a real ~2.9MB LadybugDB graph file was written to disk - the
   single most direct proof this dependency stack works frozen, since
   import success alone doesn't guarantee JIT compilation or native
   extension loading succeeds at call time.

**Four real fixes were needed - none of this worked on the first build
attempt, plus one that isn't a build-flag fix at all:**

1. **`sqlite_vec`'s `vec0.dll` wasn't bundled at all**, and the very
   first run failed immediately: `sqlite3.OperationalError: The
   specified module could not be found` during migrations. `sqlite_vec`
   loads its extension via `conn.load_extension(loadable_path())` - a
   runtime, hand-computed file path, not an `import` statement -
   invisible to PyInstaller's static analysis no matter how good its
   hooks are. Fixed with `--add-binary "<vec0.dll path>;sqlite_vec"` so
   it lands next to the frozen `sqlite_vec/__init__.py` at the same
   relative path `loadable_path()` computes.
2. **`jarvis/migrations/*.sql` weren't bundled** - `MIGRATIONS_DIR =
   Path(__file__).parent.parent / "migrations"` in `jarvis/cmd/
   migrate.py` is real path arithmetic that resolves correctly once
   frozen (`__file__` for a bundled module still supports `.parent`
   traversal), but the `.sql` files themselves are plain data,
   never auto-detected. Fixed with `--add-data "jarvis/migrations;
   jarvis/migrations"`, placed at the exact relative path that
   arithmetic expects.
3. **`tiktoken`'s encoding registry failed**: `ValueError: Unknown
   encoding cl100k_base`, surfacing during chunking (`jarvis/internal/
   chunk/chunk.py`). `tiktoken` discovers its encodings by having
   `tiktoken_ext.openai_public` register itself as a plugin, found via
   `pkgutil` namespace-package scanning at runtime - a well-documented,
   generic PyInstaller/tiktoken incompatibility (this is not
   jarvis-specific), not a hook gap. Fixed with `--hidden-import
   tiktoken_ext.openai_public --collect-submodules tiktoken_ext`.
4. **LadybugDB's graph-write path failed**: `ModuleNotFoundError: No
   module named 'ladybug._lbug_capi'` during `reconcile._persist_graph`.
   Traced (not guessed) to `ladybug/_backend.py`: it tries a compiled
   pybind11 backend (`._lbug`) via `importlib.import_module()` with a
   *string* module name (not a literal import - equally invisible to
   static analysis), and only falls back to the ctypes-based
   `._lbug_capi` if that raises `ImportError` - meaning the frozen
   build's pybind import itself was silently failing, and the fallback
   wasn't bundled either, so the failure surfaced one level down.
   `--collect-all ladybug` (bundling everything from the package,
   binaries included, rather than chasing the exact reason the deferred
   pybind import fails under a frozen build) resolved it completely -
   confirmed by the real graph file the subsequent full ingest wrote.
5. **Not a build-flag fix - a real, separate config-loading gap worth
   fixing in code before shipping**: `_spawn_daemon()`
   (`jarvis/modules/daemon/client.py`) hardcodes the daemon subprocess's
   `cwd` to `REPO_ROOT` (`Path(__file__).resolve().parents[3]`), which
   resolves to a real but meaningless internal bundle directory once
   frozen. The daemon's `load_dotenv()` (`server.py`) has no explicit
   path, so it searches upward from `cwd` for a `.env` file - this
   "worked" in the very first test only by accident, because the build
   happened to be nested inside the real repo checkout (whose real
   `.env` was found several directories up). Rebuilding the exact same
   frozen app fully outside the repo tree (`C:\temp\...`, no `.env`
   anywhere in its ancestry) reproduced the real failure directly:
   `EMBEDDING_API_KEY is not set` -> a live `401 Unauthorized` from
   OpenRouter -> `search()` returning a clean `ErrorResponse` (not a
   crash - the existing error handling did its job) instead of results.
   Placing a `.env` at the exact path the frozen `cwd` resolves to
   (`_internal/.env` in onedir mode) proved the underlying mechanism
   still works once given a real config file, and the full ingest
   above was run this way - **but this isn't how a real installed app
   should distribute a user's API key**, and remains open: some real
   settings mechanism (an in-app "connect your embedding provider" flow
   persisted via `QSettings`/a proper per-user config file, not a
   bundled or cwd-discovered `.env`) is a genuine, unresolved product
   decision, flagged here rather than silently patched over by this
   spike's `.env`-placement workaround.

**Build size**: ~550MB for a onedir build (console, unstripped) -
substantial, expected given `numba`/`scipy`/`scikit-learn`/`PySide6`/
`llvmlite` are all real weight, not investigated further here (UPX
compression, `--exclude-module` for unused heavy extras, and a
`--onefile` build were all out of scope for proving *whether* this
works, which was the actual ask).

**Not yet verified even on Windows**: `--windowed` (no console) builds,
`--onefile` packaging, code signing, and the GUI's own visual rendering
(this spike drove the daemon directly over HTTP to prove the real risk -
the dependency stack - rather than automating the native Qt window,
which this environment has no tooling to screenshot/drive). The GUI
process itself was confirmed to launch and stay alive without crashing
throughout every test run.

---

## 7. Open questions

1. **Frozen-daemon spawn (§4)** - which of the two options (argv-dispatched
   single binary, or two side-by-side binaries) to build. Real
   trade-offs on both sides (signing/notarization cost vs. spawn-logic
   complexity); not decidable from this doc alone.

2. **Ingest progress.** Per §1a, the daemon has no progress/streaming
   mechanism at all today - `/ingest/*` is one blocking call, start to
   finish. Should v1 show only a spinner/"ingesting..." state for the
   whole duration (no code change needed beyond the GUI itself), or does
   real progress (files pulled so far, chunks embedded so far) matter
   enough for v1 to also propose a daemon-side change (a polling
   `/ingest/status` endpoint, or a streaming response) - which would
   itself need its own design pass, not a decision buried in this one.

3. **The write-timeout/retry interaction (§5).** Given a real ingest can
   run past `call()`'s 120s default timeout, should the desktop app pass
   a longer timeout specifically for `run_ingest_filesystem`/
   `run_ingest_drive` (the two ingest wrappers already accept no such
   override today - `call()`'s `timeout` parameter isn't exposed through
   them), accept the current one-retry-may-double-ingest behavior as a
   pre-existing, shared characteristic not worth solving just for this
   app, or fix it at the `daemon_client` level for every caller (CLI
   included) rather than only for the GUI? This is bigger than a
   desktop-app decision if the fix belongs in the shared client.

   **RESOLVED: fixed at the `daemon_client` level, for every caller.**
   Not a desktop-app-only fix - see §8.5 for what actually changed. This
   stopped being a hypothetical during manual desktop-app testing: a real
   ~4m20s ingest genuinely succeeded server-side, but the client-side
   request hit the old 120s default, timed out, auto-retried (also
   timing out, since the daemon was still busy behind its write lock on
   the first attempt), and surfaced a false "ingest failed" dialog for an
   ingest that had, in fact, already succeeded.

4. **Last-used folder/connector memory.** Should the app remember the
   last filesystem folder browsed to, or the last Drive account/folder
   used, across launches? Straightforward to add (a small local
   preferences file, unrelated to the daemon's own `jarvis.db`), but a
   real product decision, not inferable from anything already built.

5. **System tray icon.** A tray icon (with a "daemon status" indicator,
   quick search, or "ingest running" affordance) is a common pattern for
   this kind of always-available local tool, but changes the app's
   basic lifecycle model (does closing the main window quit the app, or
   minimize to tray while the daemon keeps running?) enough that it
   should be a deliberate v1/later-phase call, not a default assumption.

6. **Drive account scope.** The CLI's Drive flow supports exactly one
   signed-in account at a time (via `drive.get_drive_service()`'s cached
   OAuth token). Does v1's connector-management view need to support
   multiple Drive accounts, or is "one Drive account, like the CLI
   today" an acceptable v1 scope? Affects how much new state the
   connectors view needs beyond what `drive.py` already tracks.

7. **What "manage connectors" includes beyond adding them.** §2c covers
   *adding* a filesystem folder or a Drive folder. Does v1 also need to
   support *removing*/deactivating a previously-ingested source from the
   UI? `document_sources.tombstone_document_source` already exists at
   the DB layer (used by the pull-side dedup/removal logic), but there's
   no daemon route exposing "deactivate this source" as a user-triggered
   action today - if v1 needs this, it's a real, new daemon-side gap,
   not just a UI addition, similar in shape to open question 2.

---

## 8. Implementation (v1 skeleton)

What actually got built on branch `desktop-app-v1` - the core skeleton
per the project lead's scope, not every feature from §2/§7.

### 8.1 Frozen-daemon spawn (§4, resolved)

`jarvis/cmd/desktop_app.py` is the new single entrypoint: checks
`"--daemon" in sys.argv` and dispatches to
`jarvis.modules.daemon.server.main()` (via `jarvis.cmd.daemon.
_ensure_fixed_hash_seed()` first, reused directly, not duplicated) or to
the GUI's own `jarvis.modules.desktop_app.app.main()` - both as deferred
imports, so a `--daemon` run never needs PySide6 importable and a GUI
run never needs the daemon's own import chain.

`_spawn_daemon()` (`jarvis/modules/daemon/client.py`) and
`_ensure_fixed_hash_seed()` (`jarvis/cmd/daemon.py`) both gained the
same small `getattr(sys, "frozen", False)` check, choosing
`[sys.executable, "--daemon"]` under a frozen build and
`[sys.executable, "-m", "jarvis.cmd.daemon"]` otherwise - confirmed via
the existing daemon test suite (`tests/test_daemon.py`,
`tests/test_daemon_error_handling.py`) that the unfrozen path is
completely unchanged, not just "should be."

**Verified without a real PyInstaller build**, per the task's own ask
for how: `sys.frozen` is a plain attribute PyInstaller sets on the
`sys` module at runtime - nothing about checking it requires an actual
frozen executable to exist. `tests/test_frozen_daemon_spawn.py`
monkeypatches `sys.frozen` directly (`monkeypatch.setattr(sys, "frozen",
True, raising=False)` - `raising=False` because the attribute doesn't
exist at all under a normal interpreter, confirmed) and mocks
`subprocess.Popen`/`subprocess.run` to capture the exact argv each
function would have spawned, for both the frozen and unfrozen case, plus
`jarvis.cmd.desktop_app.main()`'s own `--daemon` routing itself (does a
`--daemon` argv actually reach `daemon.server.main()`, not the GUI, and
vice versa). This exercises the identical code path a real frozen build
takes; what it can't verify is that PyInstaller itself successfully
bundles the daemon's heavy dependency stack (numba/llvmlite/umap-learn/
hdbscan) into one working executable at all - that's §6's still-open,
separate packaging-spike question, unaffected by this dispatch logic
being correct.

### 8.2 App skeleton, threading wrapper, auto-start, error presentation

- `jarvis/modules/desktop_app/daemon_worker.py` - `DaemonCallWorker`
  (`QRunnable`) + `run_daemon_call()`, exactly as §1b/§3 called for: runs
  any `daemon_client` function on a `QThreadPool` worker thread,
  delivers the result via a `succeeded`/`failed` Qt signal pair. Reuses
  `daemon_client`'s functions directly - no request/response logic is
  reimplemented here, confirmed by `tests/test_desktop_app_daemon_worker.py`'s
  own test that monkeypatches only `daemon_client.call()` (the one real
  seam) and checks the worker's result is a genuine, model-validated
  `SearchResult` produced by `daemon_client.search()` itself.

  **A real bug found while testing this, not by inspection**:
  `run_daemon_call`'s returned worker (and its `signals` `QObject`, the
  actual home of the signal/slot connection) could be garbage-collected
  before the pool thread ever ran it, if the caller didn't keep the
  return value alive - the documented, intended usage. `QThreadPool.
  start()` transfers ownership of the *C++* `QRunnable` to the pool, but
  that doesn't keep the *Python* wrapper object alive on its own. Fixed
  with a plain module-level `_in_flight` set holding a strong reference
  for exactly as long as a call is outstanding, removed once either
  signal fires.

- `jarvis/modules/desktop_app/main_window.py` - a `QTabWidget` with the
  three views (§2's "your call on exact widget arrangement" - a plain
  tab bar over a sidebar, since three fixed top-level destinations don't
  need a separate navigation widget; swapping to a sidebar later doesn't
  touch how the views talk to the daemon). Auto-start (§4): `ensure_
  daemon_running()` runs once, off the GUI thread, as soon as the window
  is constructed; every daemon-dependent tab starts disabled and only
  enables once that succeeds. A failure shows the three-tier message
  (§5, unchanged from the CLI/MCP-proxy's own messages) on a shared,
  non-blocking banner - the tabs stay disabled, since nothing under them
  can do anything useful yet.
- `jarvis/modules/desktop_app/error_presentation.py` - `DaemonStatusBanner`
  (the non-blocking banner, reused by the startup check and both read
  views) and `show_ingest_error_dialog` (the modal dialog, used only by
  the connectors view's write action) - exactly the §5 split, no new
  message text anywhere, only which widget shows it.
- `jarvis/modules/desktop_app/app.py` - the GUI's own `main()`. Also sets
  `JARVIS_DAEMON_HOME` (via `QStandardPaths`, Qt's own cross-platform
  per-user-app-data-directory helper) as a default, `os.environ.
  setdefault(...)` so a developer/test override still wins - not asked
  for verbatim in the implementation instructions, but a direct,
  necessary consequence of "auto-start on launch" actually working for a
  packaged app's real user, who has no `.env` file and no reason to know
  what an environment variable is. This is the entire reason
  `JARVIS_DAEMON_HOME` derivation exists at all (see the prior PR that
  added it) - flagged here since it's a small addition beyond the
  letter of what was asked, not silently invented.

### 8.3 Search view (§2a) - built fully; hierarchy/connectors views - real, deliberately lighter

Search: query box, results list bound to `ChunkHit` via a pure
`format_hit()` function (directly unit-tested with no Qt involved),
"load more" via `next_cursor` - filters are still the explicit follow-up
§2a already called out, not built here.

Hierarchy and connectors views are real, working code (not stubs), but
two deliberate scope calls worth recording plainly rather than letting
"built the skeleton" imply more than it does:

- **`QTreeWidget`, not a custom `QAbstractItemModel`.** §2b suggested a
  custom model as "a reasonable, standard fit" for real lazy loading via
  `canFetchMore`/`fetchMore` - a valid upgrade path, but more machinery
  than a first skeleton needed. `QTreeWidget`'s item-based API gets the
  same lazy-on-expand behavior via `itemExpanded` with far less code.
- **Drive ingest is stubbed, not built.** The button exists (disabled,
  with a tooltip) so the connectors view's overall shape matches §2c,
  but the actual Drive folder-browser dialog from §1c - real, non-trivial
  new UI navigating `drive.list_drive_folders()` - wasn't one of this
  pass's itemized deliverables (only the search view was called out for
  full build-out). Filesystem ingest is fully wired end to end.
  Similarly, §2c's "list of what's already been ingested" surface is
  deferred - the per-ingest outcome summary is this pass's only
  outcome-facing piece for this view.

### 8.4 Tests

`tests/test_frozen_daemon_spawn.py` (8), `tests/test_desktop_app_daemon_worker.py`
(7), `tests/test_desktop_app_views.py` (17) - 32 new tests total. The
daemon-communication logic (frozen dispatch, the threading wrapper,
auto-start) got the same rigor as the rest of this codebase's tests, per
the task's explicit ask - real signal delivery via `pytest-qt`'s
`qtbot.waitSignal`/`waitUntil` (not fixed sleeps), a real cross-thread
identity check (`threading.get_ident()` compared between the calling
and executing thread, not just "the callback eventually ran"), and the
real `_ensure_fixed_hash_seed`/`_spawn_daemon` functions exercised
directly rather than reimplemented as test doubles. View tests are
real but intentionally lighter, per the task's own allowance for that -
basic instantiation plus result-handling methods driven with
constructed model objects, not full simulated mouse-click interaction
sequences.

Full suite: **446 passed, 4 skipped** without `--run-slow`; **448
passed, 2 skipped** with it - no regressions, confirmed against the
count immediately before this pass (414/4).

### 8.5 Ingest timeout / no-retry-on-timeout-for-writes (§7, open question #3, resolved)

Fixed in `jarvis/modules/daemon/client.py` - the shared module every
caller (CLI, MCP proxy, desktop app) already goes through, per the
question's own framing that this is bigger than a desktop-app-only fix:

- `call()` gained a `retry_on_timeout: bool = True` parameter. Default
  behavior (every read: `search`/`get_hierarchy`/`browse`/`related`/
  `query_db`) is unchanged - reads are expected to be fast, so a timeout
  is a real anomaly still worth the existing one automatic retry.
- `run_ingest_filesystem`/`run_ingest_drive` now pass
  `retry_on_timeout=False`. A timeout on a write raises
  `DaemonTimeoutError` immediately instead of retrying, because the
  daemon may still be genuinely working on the original request behind
  its write lock - retrying would fire a redundant second ingest at a
  daemon already busy with the first one, not recover anything. A
  connection failure (refused/reset) still retries unconditionally for
  every call kind, write or read: it means the daemon never even started
  processing the request, so a retry can't cause duplicate work the way
  a timeout-retry on a write can.
- The two ingest wrappers also now pass a much longer timeout,
  `_INGEST_TIMEOUT_S = 30 * 60.0` (30 minutes), instead of the shared
  120s `_DEFAULT_TIMEOUT_S`. No real-corpus timing benchmark exists
  anywhere in this codebase (`docs/organize-checkpointing-DESIGN.md` §3
  already noted this), so this is a reasoned starting value, not a
  measured one: the real ~4m20s/261-chunk/58-cluster ingest observed
  during manual testing already exceeded the old 120s default on a
  modest corpus, and 30 minutes is meant to comfortably clear a
  realistically-sized real corpus while still eventually detecting a
  genuinely dead connection rather than blocking forever. Worth
  revisiting once a larger real corpus is actually measured.

Tested in `tests/test_daemon_error_handling.py` (10 new tests, 22 -> 32
in that file): both ingest wrappers pass the longer timeout while every
read wrapper still passes the original 120s default; a timeout on an
ingest call raises immediately with exactly one HTTP attempt, including
a test that queues a would-be-successful second response specifically to
prove it's never consumed; a timeout on a plain `call()` (reads) still
retries once, unchanged; and a connection error (not a timeout) on an
ingest call still retries once, unchanged - confirming the opt-out is
timeout-specific, not a blanket "writes never retry."

Full suite: **462 passed, 4 skipped, 1 failed** without `--run-slow`;
**465 passed, 2 skipped** with it. The one failure
(`test_clustering.py::test_noise_routes_to_unfiled_with_outlier_scores`)
is pre-existing test-order-dependent flakiness in random-blob-based
clustering, unrelated to this change - confirmed by re-running it alone,
where it passes.

### 8.6 Two more real bugs from the same live-testing session

A second real failure surfaced immediately after §8.5's fix, ingesting
`docs/` through the desktop app: a raw `ConnectionError: ('Connection
aborted.', ConnectionResetError(10054, 'An existing connection was
forcibly closed by the remote host'...))` dialog. Traced directly from
`.jarvis_daemon.log` (not guessed): the daemon process never crashed or
restarted (one `pid 18220` for the whole session, confirmed via the log's
single `"Started server process"` line, the discovery file, and
`tasklist`) - the connection that actually reset was the daemon's own
**outbound** HTTPS call to the external embeddings API
(`jarvis/internal/embed/provider.py::_embed_batch`, embedding cluster
*titles* during hierarchy organize - the one embedding call per ingest
that's never cache-covered), not the loopback connection to the desktop
app at all. `api_ingest_filesystem` caught the resulting exception and
returned a well-formed `500` with a JSON `{"error": {...}}` body (visible
in the log's own uvicorn access line); the desktop app displayed that
body's message field verbatim, which is why it read exactly like a raw
Python exception. This investigation surfaced two separate, real fixes,
both made at the shared `daemon_client`/embed-provider level (not
desktop-app-specific):

1. **`daemon_client._do_request()` now checks the response status code
   (docs/local-server-architecture-DESIGN.md §10, expanded).** It
   previously called `resp.json()` unconditionally, so any non-2xx
   daemon response - not just this incident's well-formed 500, but any
   malformed or unexpected one - was silently handed back to the caller
   as if it were ordinary data. Fixed at the source, not per-caller: see
   §10 of that doc for the full rationale and the `_is_error_envelope()`
   distinction (a deliberate `{"error": {...}}` body still flows through
   as data, unchanged; anything else now raises). This is a
   `daemon_client`-level fix, so every caller (CLI, MCP proxy, desktop
   app) benefits, the same "fix it in the shared client" principle §7's
   open question #3 already established for the timeout/retry fix.
2. **`OpenRouterVoyageEmbedder._embed_batch()` now retries a genuinely
   transient connection failure** (`jarvis/internal/embed/provider.py`) -
   up to `_EMBED_MAX_RETRIES = 2` retries with a short, growing pause
   (`_EMBED_RETRY_DELAY_S = 2.0`, doubled per attempt), mirroring
   `daemon_client.call()`'s own retry shape. Scoped narrowly on purpose:
   only `requests.ConnectionError`/`requests.Timeout` (the request never
   got a real answer at all) retry; an `HTTPError` - a real, deterministic
   response from the provider, including 401/403 auth failures - never
   does, since retrying an invalid key or a bad request can't succeed
   differently the second time.

Tested in `tests/test_daemon_error_handling.py` (5 new tests, 32 -> 37)
and `tests/test_embed.py` (5 new tests, 7 -> 12): a malformed 500/400
response raises `DaemonConnectionError` instead of being returned as
data, a well-formed 500 error-envelope is unaffected (regression guard
for the existing ingest contract), and an ingest call surfaces a
malformed 500 as a raised error end-to-end; the embedder recovers from
one simulated `ConnectionError`/`Timeout` before succeeding, gives up
after exhausting its retries against a persistent failure (exactly 3
attempts, not a loop), and never retries an auth failure or any other
real HTTP error status.

Full suite: **473 passed, 4 skipped** without `--run-slow`; **475
passed, 2 skipped** with it - no failures this run (§8.5's flaky
clustering test didn't trip this time either).

### 8.7 The exact same gap, live, in a sibling function §8.6 didn't cover

While verifying §6's packaging spike build against a real, fresh ingest
(`docs/`, run through the frozen `.exe` itself, not the dev daemon), the
*identical* failure class recurred: `ConnectionError: ('Connection
aborted.', ConnectionResetError(10054, ...))`. Traced directly from the
frozen build's own `.jarvis_daemon.log` - this time from
`OpenRouterLLM.complete()` (`jarvis/internal/llm/llm.py`), the LLM call
that labels each topic cluster, not `_embed_batch()` (§8.6 already fixed
that one). Same OpenRouter host, same transient-reset failure mode,
same "no retry at all" gap - just a structurally separate function
§8.6's fix never touched. A fresh database has no checkpointed labels to
reuse, so a real ingest against it makes far more of these LLM calls
than the checkpoint-heavy runs earlier fixes were tested against,
which is almost certainly why this specific gap hadn't surfaced yet.

Fixed identically to §8.6's embedding retry, mirrored precisely:
`OpenRouterLLM.complete()` now retries up to `_LLM_MAX_RETRIES = 2`
times with a short, growing pause (`_LLM_RETRY_DELAY_S = 2.0`, doubled
per attempt) on `requests.ConnectionError`/`requests.Timeout` only - any
other `requests.RequestException` (including a real `HTTPError` -
401/403/400/etc.) still raises immediately, unretried, since the server
already gave a deterministic answer. Confirmed live: retrying the exact
same ingest against the frozen build succeeded completely on the next
attempt, with the checkpointing already in place ensuring nothing
already embedded got redone (265/265 chunks were cache hits).

Tested in new `tests/test_llm.py` (6 tests, mirroring
`tests/test_embed.py`'s retry coverage exactly): `complete()` recovers
from one simulated `ConnectionError`/`Timeout` before succeeding, gives
up after exhausting its retries against a persistent failure (exactly 3
attempts, not a loop), and never retries a 401 or any other real HTTP
error status.

Full suite: **483 passed, 4 skipped** without `--run-slow`; **485
passed, 2 skipped** with it - no failures either run.

**Worth noting for later, not fixed here**: `_embed_batch()` and
`complete()` now carry the identical retry logic maintained in two
places. Both talk to OpenRouter, both hit the same failure class live,
and a third caller (a future one) would need the same fix copied a
third time. A small shared helper (e.g. `jarvis/internal/http_retry.py`,
wrapping "run this request-making callable, retry on
ConnectionError/Timeout only") would remove the duplication - flagged
here rather than done speculatively, since exactly two call sites isn't
yet the "three strikes" signal this codebase's own conventions look for
before extracting a shared abstraction.

### 8.8 First-run settings screen for the embedding API key

Closes the real friction §6/§8.6 already flagged but left open: a
packaged app's user has no repo checkout and no way to hand-place a
`.env` file inside a frozen build's internal folder - the only way the
packaging spike itself got a working `EMBEDDING_API_KEY` into a frozen
build was by manually copying one there, explicitly called out at the
time as "not how a real installed app should distribute a user's API
key."

**`jarvis/internal/env_config.py`** is the new shared source of truth
for "where is a real key, and where does a new one get saved" - both
the daemon (`server.py`) and the desktop app (`app.py`/`main_window.py`)
call into it, so the two can't drift on what counts as "configured":

- `load_env(daemon_home)` tries a real repo-relative `.env` first
  (`load_dotenv()`'s own unchanged, cwd-based upward search - dev mode
  is completely untouched), then a packaged fallback at
  `<daemon_home>/.env` (or `$JARVIS_DAEMON_HOME/.env` if no explicit
  path is given) - the same OS-standard per-user app-data directory
  already used for the db/graph/log files, not a location inside the
  frozen build itself. The fallback never overrides a value the dev-mode
  search already found (`load_dotenv`'s own `override=False` default) -
  dev mode wins if both exist.
- `server.py` now calls `load_env(_DAEMON_HOME)` in place of its old
  bare `load_dotenv()`, moved to run before any other module that reads
  `os.environ` is imported (unchanged ordering guarantee, just now
  routed through the shared helper) - this is the actual fix to the
  gap: the daemon's own startup, independent of how it was launched,
  now finds a key saved at the packaged location.
- `has_embedding_api_key()` is a presence check, not a live validation
  call against OpenRouter - a wrong key still fails clearly and loudly
  on first real use (`OpenRouterVoyageEmbedder`'s existing 401/403
  handling), which is enough; validating here too would just be the
  same check done twice, at the cost of a network call before the app
  has even opened.
- `save_settings()`/`read_saved_settings()` use `dotenv.set_key`/
  `dotenv_values` (already a dependency, not a new one) rather than
  hand-writing the file - correct quoting, in-place updates, and a
  read that never touches `os.environ` as a side effect (for pre-
  filling the dialog on revisit).

**`jarvis/modules/desktop_app/settings_dialog.py`** (`SettingsDialog`,
a `QDialog`) asks for the API key, pre-filled with the current saved
value if any, plus model/API-base fields pre-filled with the exact
defaults already established elsewhere (`voyageai/voyage-4` from
`.env.example`, `https://openrouter.ai/api/v1` matching `provider.py`'s
own `DEFAULT_API_BASE`) - editable, not forced. Cancel is always
available, first-run or not: the daemon doesn't actually need a key to
start (only real embed/search calls do), so there's no correctness
reason to trap the user in this dialog, only convenience in offering it
up front.

**`main_window.py`** checks `env_config.has_embedding_api_key()` before
the existing daemon check (§4) even runs: no key -> `SettingsDialog`
shown first-run-style; either way (saved or cancelled) the existing
`_check_daemon()` flow proceeds exactly as before - this is purely an
insertion before that flow, not a replacement of any of it. A
`Settings` menu action reopens the same dialog any time afterward
(requirement: "a way to revisit/change this later"); if the daemon is
already running when a new key is saved through it, a message explains
a restart is needed - the daemon only reads its environment once, at
its own startup, so an already-running one can't pick up a change made
after the fact.

**`app.py`** calls `env_config.load_env()` right after setting
`JARVIS_DAEMON_HOME`, before constructing `MainWindow` - both so the
window can decide whether the first-run screen is needed at all, and so
`_spawn_daemon()`'s `env=dict(os.environ)` hands the daemon subprocess
these values directly (reinforcing, not replacing, that subprocess's
own `load_env()` call).

**Real, manual verification against a rebuilt frozen `.exe`** (not just
code review - matching this doc's own established practice): a fully
isolated copy of the frozen build (`C:\temp\...`, no `.env` anywhere in
its ancestry, confirmed) was launched directly in `--daemon` mode with
nothing configured - it started cleanly, with no crash and no warning
about a missing key at startup (constructing the embedder/LLM client,
where that warning fires, only happens on an actual search/ingest
call). A `.env` was then placed at exactly `<JARVIS_DAEMON_HOME>/.env` -
the same file `SettingsDialog`'s "Save" would produce - and the same
daemon process's `/ingest/filesystem` call against a small real folder
produced a genuine `Embedding batch succeeded: size=34 dimensions=1024`
in its own log, followed by a real `/search` call returning real,
correctly-sourced results - proof the packaged-location fallback is
what supplied the key, not an accident of directory nesting (§8.6's own
lesson about not trusting a build that happens to sit near a real
repo). Dev mode was separately confirmed unaffected: importing
`jarvis.modules.daemon.server` unfrozen, with only the real repo `.env`
in place and no `JARVIS_DAEMON_HOME` override, still resolves
`EMBEDDING_API_KEY` and `DISCOVERY_FILE` exactly as before.

**Not verified by this pass**: actually clicking through the dialog
itself (typing into the fields, pressing Save) on the real, visible
native window - this environment has no tooling to drive a native Qt
window, so the dialog's own logic (pre-fill, validation, persistence)
is covered by widget-level tests instead (`tests/test_desktop_app_
settings.py`), and the daemon-side pickup is covered by the real,
running-process verification above.

Tested in `tests/test_env_config.py` (14 tests) and `tests/
test_desktop_app_settings.py` (10 tests): `has_embedding_api_key()`
true/false: dev-mode value winning over the packaged fallback and vice
versa; the `JARVIS_DAEMON_HOME`-env-var fallback when no explicit path
is given; `save_settings()` writing to the correct path, creating the
directory if missing, applying immediately to `os.environ`, and
omitting a blank API base; `read_saved_settings()` round-tripping what
was saved; `MainWindow` showing the dialog only when no key is
configured (with `SettingsDialog.exec()` mocked - a real modal loop
would hang the test, the same class of bug already found once for
`show_ingest_error_dialog`) and proceeding to the daemon check
regardless of how the dialog was closed; the Settings menu action
reopening it and warning about a restart only when the daemon was
already running; and the dialog's own pre-fill/validation/save logic
driven directly, without ever entering the modal event loop. One
existing test file (`test_desktop_app_views.py`) needed a new autouse
fixture defaulting `has_embedding_api_key()` to `True`, since none of
its tests are about this new screen and several construct `MainWindow`
directly - without it, those would have hit the exact same real-modal-
hang risk.

Full suite: **507 passed, 4 skipped** without `--run-slow`; **509
passed, 2 skipped** with it - no failures either run.

**A real mistake made and worth recording plainly**: while copying a
real `.env` into the isolated test environment during manual
verification, its contents (including a real OpenRouter API key and a
real Google OAuth client secret) were printed into the session
transcript by an unnecessary `cat`. Both credentials should be treated
as exposed and rotated - this is a process note for whoever reads this
later, not a code defect.

### 8.9 Physical server/client isolation (the project lead's original,
### verbal Sept 7 requirement - confirmed still wanted, not superseded)

§7's own audit (before this section existed) found the *import graph*
already cleanly separated server logic from the GUI - but no physical
folder boundary, and no `pyproject.toml` anywhere in this repo for
*any* part of the codebase. Confirmed directly with the project lead
that the original ask - an isolated folder, compileable into a
standalone package - was still wanted, not made moot by the "one
frozen executable" packaging decision. Done as four verified stages.

**Stage 1 - the move.** `jarvis/internal/`, `daemon/server.py` (not
`client.py` - see below), `pipeline.py`, `organize/`, `connectors/`,
`mcp_tools/`, and `migrations/*.sql` moved into a new, separately-
packaged distribution: `jarvis/server/jarvis_server/` (import name
`jarvis_server`, chosen over a nested `jarvis.server.*` path
specifically to avoid PEP 420 namespace-package fragility colliding
with PyInstaller's static analysis - a real, discussed tradeoff, not a
default). `daemon/client.py` (the HTTP client) stays in
`jarvis/modules/daemon/` - it's used only by client-side callers (CLI,
MCP proxy, GUI), never by the daemon itself.

Two things this move genuinely required, not just "moved files":
- **A real bug the move itself caused, fixed, not worked around**: six
  files used `Path(__file__).parent...` arithmetic to find the repo
  root as their default (unless-overridden) fallback for `jarvis.db`,
  the graph file, migrations, Drive's OAuth token, and Drive's staging
  dir. Every one silently broke at its new, deeper file location.
  Fixed by recomputing each and *verifying empirically*
  (`Path(file).resolve().parents[N] == repo_root`, checked in code, not
  just counted by eye - a real arithmetic mistake was caught exactly
  this way mid-fix). Migrations themselves moved fully into
  `jarvis_server/migrations/` rather than getting the same repo-root
  treatment - they're data that ships *with* the code, not a
  user-data-location default, so reaching outside the package for them
  would have defeated the point of this whole move.
- **An unplanned real finding**: `organize/clustering.py` had an
  unconditional import of `jarvis.modules.debug_ui.trace` (debug-console
  instrumentation hooks, no-ops unless a trace is opened) - a genuine
  reverse dependency from server code onto the client-side debug tool.
  Moved `trace.py` into `jarvis_server/organize/trace.py`;
  `debug_ui/playground.py` now imports it from there, the same
  direction every other shared piece (`daemon.client`, `env_config`,
  `mcp_tools.models`) already goes: client depends on server's public
  interface, never the reverse. Confirmed after the move: zero imports
  from `jarvis_server` back into `jarvis.*` anywhere (checked directly).
- Full suite after the move alone: **507 passed, 4 skipped** / **509
  passed, 2 skipped** with `--run-slow` - an exact match to the
  pre-move baseline, confirmed before proceeding to stage 2.

**Stage 2 - the two entanglement points, resolved with reasoning
recorded, not silently picked:**
1. `drive_ingest.py`'s direct `connectors.drive` import for interactive
   OAuth + folder browsing **stays, as a deliberate, documented
   exception** (comments added from both sides - `jarvis/cmd/
   drive_ingest.py` and `jarvis_server/connectors/drive/__init__.py`).
   Reasoning: OAuth consent has to block in the *foreground* of
   whatever process the user is running to open a browser - a
   background daemon has no foreground to do that from. Routing it
   behind a new daemon endpoint would mean the daemon starts owning
   Google OAuth token storage/refresh - real scope creep in the wrong
   direction, not cleaner isolation.
2. `migrate.py` **now genuinely belongs to the server**
   (`jarvis_server.internal.migrate`) - the daemon is the sole process
   that opens `jarvis.db`, and its `_preflight()` already auto-migrates
   it unconditionally on every startup (unchanged by this move). Given
   that, the CLI's own pre-ingest `run_migrations(args.db)` call in
   `filesystem_ingest.py`/`drive_ingest.py` was already redundant with
   real, current behavior - its own `--db` flag's help text admitted as
   much ("does not change where ingested content ends up"). Removed
   both the call and the now-dead `--db` flag; `python -m
   jarvis.cmd.migrate` (a thin wrapper around the same function) stays
   as the real, README-documented, standalone way to migrate an
   arbitrary file by hand. Two now-obsolete test fixtures
   (`test_filesystem_ingest.py`/`test_drive_ingest.py`, each stubbing a
   `run_migrations` attribute that no longer exists on those modules)
   removed; all 19 tests in both files still pass.
- Full suite after stage 2: **507 passed, 4 skipped** - unchanged.

**Stage 3 - `jarvis/server/pyproject.toml`.** Declares every server-side
dependency (everything in `requirements.txt` except `PySide6`/
`pytest-qt`, which are GUI-only) and `jarvis_server/migrations/*.sql` as
package data. Verified as *genuinely* installable, not just "the file
exists": built a real wheel (`python -m build --wheel`) and installed
it into a **fresh, fully isolated venv** (`pip install --no-deps
<wheel>`, no editable pointer back to this repo) - confirmed
`jarvis_server.__file__` resolved from that venv's own `site-packages`,
`MIGRATIONS_DIR` pointed at the wheel's own bundled migrations (not
reaching back into this checkout), and every server module (`daemon.
server`, `organize.clustering`, `connectors.drive`, `mcp_tools.search`)
imported cleanly from there. For this repo's own dev/test workflow, `pip
install -e ./jarvis/server` plus a `pytest.ini` `pythonpath` line
(belt-and-suspenders - the editable install is what actually makes
`jarvis_server` resolve correctly inside *subprocesses* too, which
`pythonpath` alone does not) is the supported setup.

**Stage 4 - the frozen `.exe`, rebuilt and re-verified for real, not
just recompiled.** `packaging/jarvis_desktop.spec` updated:
`pathex` gained `../jarvis/server` (PyInstaller's static analysis can't
reliably trace through a PEP 660 editable install's import hooks, so it
needs the real source directory directly, same as the main `jarvis`
package already required), and the migrations `--add-data` entry moved
to `jarvis_server/migrations` to match. Rebuilt cleanly (same one
benign `tbb12.dll` warning as every prior build). Copied fully outside
the repo tree again (`.env` placed at the frozen `cwd`'s `_internal/`
location, exactly as §8.8 established) and re-ran the same real
verification this project now treats as standard, not "it starts": a
real ingest against `docs/` (268 chunks, all newly embedded via real
OpenRouter calls, 3 API calls; real UMAP+HDBSCAN clustering producing
54 topics / 56 live after reconcile; a real ~2.9MB graph file written),
followed by a real search returning real, correctly-sourced results.
The daemon log's own logger names (`jarvis_server.internal.migrate`,
`jarvis_server.daemon.server`) confirm the new import paths are what's
actually running inside the frozen build, not the old ones silently
still present.
