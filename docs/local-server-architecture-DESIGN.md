# Local server architecture: design

**Status:** two implementation passes built. The first (§7) built the
daemon itself on defensible defaults for 6 of the 7 originally-open
questions, per the project lead's direction. The second (§8/§10) closed
the CLI-bypass scope gap the first pass flagged, and gave every client
clean, tested daemon-unreachable error handling instead of a raw
traceback. **Encryption (§5) is the one item deliberately left
undecided** - a hard stop, not an oversight; the daemon is built so it
can be added later without a redesign, but no specific scheme is
implemented.

**Source:** not covered by `mvp-spec.md`; confirmed verbally with the
project lead. No GitHub issue filed yet — recommend filing one once this
doc is reviewed, per the existing "design doc, then a ticket, then a
branch" pattern.

**Confirmed requirement:** one local background process owns `jarvis.db`
and the LadybugDB graph file (`jarvis_graph.lbdb`). The CLI, the desktop
app (Mac/Windows), and the MCP server all talk to this one process
instead of each independently opening either file.

**Why:** SQLite allows only one writer at a time; LadybugDB allows only
one process to hold a read-write connection to a given file *at all* —
that's a hard engine constraint, not a scheduling nicety. Today,
`jarvis.cmd.filesystem_ingest`, `jarvis.cmd.drive_ingest`, and every
`jarvis.cmd.mcp_server` a client spawns each open these files
independently (verified below). Two of these running at overlapping
times already risk SQLite lock contention, and a second concurrent
LadybugDB writer doesn't contend — it fails outright.

---

## 1. Verified current state (read the real code, not memory)

### 1a. `jarvis.cmd.mcp_server` is not a persistent owner of anything today

Its preflight opens one connection, checks it, and closes it before the
server even starts serving:

```python
conn = db.get_connection()
try:
    embedded_chunks = db.count_chunk_vectors(conn)
finally:
    conn.close()
```

Then it hands off to `server.run(transport="stdio")`. The module's own
docstring is explicit about the lifecycle model this implies: *"stdio,
not SSE / streamable-HTTP: single local user, no hosting (CLAUDE.md
scope). The client owns the process lifecycle - there is no port to bind
and nothing left running between sessions."*

Every one of the five `@server.tool()`-decorated functions
(`jarvis/modules/mcp_tools/search.py`, `hierarchy.py`'s `get_hierarchy` /
`browse` / `related`, `sql_query.py`'s `query_db`) follows the identical
pattern — open, use, close, every single call:

```python
conn = db.get_connection()
try:
    return _search_impl(conn, ...)
finally:
    conn.close()
```

(`hierarchy.py`: lines 247/254, 310/317, 430/437 — same shape three
times. `sql_query.py` goes further and opens via a `mode=ro` SQLite URI
on top, as a second, structural safety layer on top of its SQL-syntax
check.)

So "the MCP server" is not currently long-running in the sense that
matters here — it's five independent open/close cycles that happen to
run inside one process, for as long as *one* MCP client keeps its stdio
pipe open. And because stdio means **the client spawns this process**,
there is one `jarvis.cmd.mcp_server` process per connected client, not
one shared process serving every client. Two MCP clients connected at
once (or one MCP client plus a manual CLI ingest) is already 2+ OS
processes independently calling `db.get_connection()` against the same
file.

### 1b. Both CLI ingest entrypoints do the same thing: open, run, close

`jarvis/cmd/filesystem_ingest.py`:

```python
conn = db.get_connection(args.db)
try:
    result = run_ingest(conn, FilesystemConnector(args.path), ...)
finally:
    conn.close()
```

`jarvis/cmd/drive_ingest.py` mirrors this exactly (`db.get_connection` /
`run_ingest` / `conn.close()` at lines 134, 150, 158). Each CLI
invocation is a distinct OS process holding its own connection to
`jarvis.db` for the whole ingest run — which, for a real Drive folder or
a large local tree, can be a non-trivial amount of wall-clock time to be
holding a write-capable connection.

### 1c. SQLite is opened with no WAL mode and no busy timeout

`jarvis/internal/db/connection.py::get_connection()` in full, minus
docstring:

```python
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)
return conn
```

No `PRAGMA journal_mode=WAL`, no `PRAGMA busy_timeout`. That means
today's default rollback-journal locking: a writer blocks every reader
for the duration of its transaction, and a second connection that hits a
lock gets `sqlite3.OperationalError: database is locked` after Python's
`sqlite3` default 5-second timeout, rather than queuing gracefully. This
is a real gap independent of everything else in this doc, and worth
fixing regardless of which daemon shape gets built (see §6).

### 1d. LadybugDB's single-connection constraint is already documented as a hard constraint, not something new to this doc

`jarvis/internal/graph_db/graph_db.py`'s own module docstring: *"LadybugDB
allows only one process to hold a read-write connection to a given
database file at a time. This matches spec §5.3's existing single-writer
sync model - not a new constraint introduced here."* The graph file
(`jarvis_graph.lbdb`, `graph_db.DEFAULT_GRAPH_PATH`) is opened by
`staged_build.build_and_swap()` during `run_ingest`'s organize/reconcile
step. Two ingests racing today wouldn't corrupt the graph file so much as
one of them would simply fail to open it — but "fail outright" on an
unlucky race is still a real bug from a user's point of view.

### 1e. `jarvis.internal.db` already enforces "one owner" — just within a process, not across processes

From `docs/coding-guides/database-access.md`: *"This package is the only
place that runs SQL against `jarvis.db`."* Every table's reads/writes go
through one of `jarvis/internal/db/`'s modules, wrapped in shared
`read_query`/`write_query` middleware. That's a real, enforced discipline
today — but it's a within-process rule; nothing stops two different
*processes* from each individually respecting it while still colliding
with each other on the file itself. The daemon this doc proposes extends
the same discipline one level up: not just "one package may run SQL," but
"one process may run SQL."

### 1f. There's already a precedent for "a separate process fronts the DB" — but it's 1:1, not shared

`jarvis/modules/debug_ui/server.py` spawns `python -m jarvis.cmd.mcp_server`
as a real subprocess and drives it over the actual stdio/JSON-RPC MCP
transport — not an in-process function call. Its own docstring: *"Owns
one long-lived stdio session to a real `jarvis.cmd.mcp_server` subprocess
for the lifetime of the debug UI process."* This proves the codebase
already treats "a separate process fronting the DB" as a normal shape to
build. The difference from what's proposed here: today it's exactly one
debug-UI process talking to exactly one server process *it spawned
itself and owns the lifecycle of*; the daemon this doc proposes has to
serve multiple, independently-launched clients (CLI runs, the desktop
app, N MCP sessions) that don't spawn it and don't own its lifecycle.

---

## 2. Proposed shape — a recommendation, not a menu of options

**Recommendation: a new, separate, long-running background process (call
it "the Jarvis daemon" until it's named) — not the existing
`jarvis.cmd.mcp_server` promoted in place.**

Reasoning, grounded in what's actually in the code above:

- **MCP-over-stdio is architecturally 1:1 and client-owned.** The current
  `mcp_server.py` docstring says it outright: the client owns the process
  lifecycle, nothing is left running between sessions. A "shared owner"
  that dies whenever one particular Claude Desktop window closes — taking
  down the CLI's and the desktop app's DB access with it — does not meet
  "the CLI, desktop app, and MCP server all talk to this one process."
  The requirement needs a process whose lifecycle is independent of any
  one client's session.
- **The daemon has to serve several independent clients concurrently**
  (a CLI ingest run, the desktop app's UI, and however many MCP sessions
  are open) rather than being spawned fresh per session. That's a
  different lifecycle model than stdio gives today.
- **The actual business logic doesn't need to move** (§4) —
  `run_ingest`, `_search_impl`, the hierarchy tools' implementations, and
  `query_db`'s implementation stay exactly where they are and exactly as
  written. What moves is only *where the connection gets opened* and
  *who is allowed to call these functions directly*. That's a small,
  well-bounded change, which is what makes "new process" the cheap
  option here rather than a rewrite.
- The existing `jarvis.cmd.mcp_server` becomes a **thin proxy**: an MCP
  client still spawns it over stdio exactly as today (nothing about the
  Claude Desktop integration changes from the client's point of view),
  but every tool call it receives is forwarded to the daemon's API
  instead of calling `db.get_connection()` itself.

Concretely, after this change: `jarvis.cmd.mcp_server`,
`jarvis.cmd.filesystem_ingest`, `jarvis.cmd.drive_ingest`, and the
desktop app are all **clients** of the daemon. Only the daemon process
ever imports `jarvis.internal.db` or `jarvis.internal.graph_db`.

---

## 3. Communication mechanism

Options considered: local HTTP/RPC over loopback, a Unix domain socket /
Windows named pipe, and Python's stdlib `multiprocessing.connection`
(which picks a Unix socket on POSIX and a named pipe on Windows
automatically).

**Recommendation: local HTTP/JSON over loopback (`127.0.0.1`), reusing
the pattern already proven in `jarvis/modules/debug_ui/server.py`** (a
Starlette app bound to `http://127.0.0.1:8765`).

Reasoning:

- **The desktop app is very likely not Python.** A native Mac/Windows app
  is more likely Swift, Electron/Node, or C# than a Python process. HTTP
  has a client in every language's standard toolkit with zero extra
  dependencies. A Unix domain socket or named pipe needs its own
  platform-specific implementation in whatever language the desktop app
  ends up written in, and `multiprocessing.connection` is a Python-to-
  Python mechanism (it pickles Python objects on the wire) — a poor fit
  for a non-Python client at all, regardless of platform.
- **One code path for both platforms**, which the requirement explicitly
  calls out. A raw "Unix domain socket on Mac, named pipe on Windows"
  split means two implementations to write, test, and keep in sync.
  Loopback TCP behaves identically on both.
- **Existing precedent already in this repo.** `debug_ui/server.py`
  already runs a local HTTP server for exactly this shape of problem —
  another local process talking to the thing that owns the DB. Reusing
  that (same web framework, same "bind to `127.0.0.1` only") is less new
  surface than introducing a second IPC mechanism alongside it.
- **Debuggability.** `curl` / browser devtools / any HTTP client library
  can poke at it directly — the existing debug console already leans on
  exactly this for MCP tool calls; the daemon's API gets the same for
  free.

**Trade-off acknowledged, not hidden:** a loopback HTTP port is
reachable by any local process on the machine unless something restricts
it further. Access control is left explicitly open (§6) - the resolved
default is "loopback binding only, revisit once encryption exists"
(§6.6), not a claim that this is secure against another local process.

**Resolved: port + discovery (§6.2).** OS-assigned (`bind(("127.0.0.1",
0))`), not fixed - avoids colliding with anything else on the machine.
The daemon writes `{"port", "pid"}` to a well-known file
(`.jarvis_daemon.json`, repo root) on startup and removes it on clean
shutdown; a client reads that file, then confirms the daemon is actually
alive by calling `/health` before trusting it (a stale file from a
crashed daemon is silently distinguished this way, not treated as
"still running"). See §7.2 for the one Windows-specific implementation
snag this produced.

---

## 4. What changes for existing code

**The core logic functions do not change.** `run_ingest()`,
`_search_impl()` (`search()`'s implementation), the hierarchy tools'
`_get_hierarchy_impl()` / `browse()` / `related()` bodies, and
`query_db()`'s implementation already take a `conn` as their first
argument and contain no process-lifecycle assumptions — they're already
separated from connection management. This isn't a coincidence to design
around; it's already the shape of the code today, because every one of
these functions is currently *called from inside* a
`conn = db.get_connection(); try: ...; finally: conn.close()` wrapper
(§1a/§1b). That wrapper is exactly the seam that moves.

What does change:

- **The wrapper layer — for the MCP server. Not yet for the CLI ingest
  entrypoints (see the scope note below).** Today, `search()`,
  `get_hierarchy()`, `browse()`, `related()`, `query_db()` (the literal
  `@server.tool()`-decorated functions) are both "the place that opens a
  connection" *and* "the place callers call directly." After this
  change, the daemon process hosts these — still opening/closing a
  connection per request, same pattern as today — and exposes them over
  HTTP. `jarvis.cmd.mcp_server` no longer imports `search.py` /
  `hierarchy.py` / `sql_query.py` at all; it defines its own proxy
  versions of the same five functions (same names, same signatures) that
  forward to the daemon and parse its JSON back into the same Pydantic
  models a direct call would have returned. It stops importing
  `jarvis.internal.db` entirely.
- **Resolved: concurrency (§6.5 - reads proceed during an in-flight
  write).** The daemon does **not** hold one shared `sqlite3.Connection`
  for its whole lifetime - a single shared connection object would
  serialize reads and writes together regardless of WAL, which defeats
  the point of enabling it (a `sqlite3.Connection` executes one
  statement at a time; concurrent use of one Python object across
  threads isn't what WAL's MVCC snapshots buy you). Instead: reads
  (search / get_hierarchy / browse / related / query_db) each open their
  own connection exactly as before, unlocked, so WAL lets them proceed
  against a consistent snapshot while a write is mid-transaction. Writes
  (the two ingest routes) serialize against *each other* via one
  `asyncio.Lock` in the daemon - not because SQLite's own busy-timeout
  couldn't eventually sort out overlapping commits, but because
  `run_ingest`'s organize/reconcile step opens the LadybugDB graph file,
  which allows exactly one writer, full stop; two ingests interleaved
  inside one process is the same hazard the whole daemon exists to
  remove between processes. "One owner" in this doc means one *process*
  ever opens either file, not one *connection object* for every request
  - worth stating explicitly since the two could be conflated.
- **Scope gap, since closed (§8) - recorded here for how it was found.**
  As originally built, `jarvis/cmd/filesystem_ingest.py` and
  `jarvis/cmd/drive_ingest.py` still called `db.get_connection()` and
  `run_ingest()` directly - **not** converted to daemon clients, even
  though the daemon's `/ingest/filesystem` and `/ingest/drive` routes
  already existed and were tested (§7). This meant the confirmed
  requirement ("the CLI ... talk to this one process instead of
  independently opening the database files") was only half-satisfied:
  the MCP-server path was converted, the direct CLI path was not - and,
  more specifically, the daemon was not yet the sole owner of "the
  actual processing" (organize/label/embed) the project lead had asked
  for, since a CLI-run ingest still ran that logic in-process. §8 closes
  this gap; see it for what actually changed.
- **The graph file's own lifecycle doesn't change.** Nothing in
  `graph_db.py` or `staged_build.py` needs to change — they already take
  a path and manage their own open/close/atomic-swap correctly
  (`staged_build.build_and_swap`'s validate-then-`os.replace` design is
  unaffected). The fix is entirely about *who calls `run_ingest`* (only
  the daemon, from now on), not how `run_ingest` uses the graph file
  internally.
- **`jarvis.internal.db`'s "only place that runs SQL" rule
  (`database-access.md`) gets a process-level reading, not a new rule.**
  No changes needed inside that package; the daemon is simply the only
  process permitted to import it going forward.

---

## 5. Encryption requirement — open, not guessed

The project lead also asked for `jarvis.db` and `jarvis_graph.lbdb` to be
encrypted at rest. This doesn't have an obvious answer from what exists
in the codebase today, and picking one silently would be a real
architectural commitment — different storage engine, different
key-management story, different failure modes — so this section flags
the shape of the problem rather than guessing a solution.

- **SQLite has no native encryption.** The realistic options are
  (a) SQLCipher, a fork of SQLite that encrypts at the page level — but
  it's a different native library than the stdlib `sqlite3` +
  `sqlite-vec` combination this repo runs today, and `sqlite-vec`'s
  compatibility with a SQLCipher build hasn't been checked; or (b) leave
  SQLite itself unencrypted and rely on OS-level encryption of the file
  (BitLocker / FileVault, or a per-file wrap via Windows DPAPI / macOS
  Keychain) — which protects the file at rest on a powered-off or stolen
  disk but does nothing once the OS user is already logged in, a
  materially weaker property than SQLCipher's "the file on disk is
  ciphertext, full stop."
- **LadybugDB's own encryption support is unknown — not checked, not
  assumed.** Whether the engine this repo depends on for the graph file
  has any built-in at-rest encryption needs research before this can be
  designed at all. If it doesn't, the graph file needs either the same
  OS-level treatment as (b) above, or a purpose-built wrapper (e.g.
  decrypt to a temp file at daemon startup, delete on shutdown).
- **Where the key lives is a security decision, not an implementation
  detail.** A passphrase typed at daemon startup, an OS-keychain-backed
  key (Keychain on Mac, DPAPI/Credential Manager on Windows), or a key
  file on disk are meaningfully different threat models — does losing
  the machine also lose the data, does another local user account on the
  same machine see anything, is there a "forgot the passphrase" recovery
  story at all.
- **This interacts with §2 directly.** "Only the daemon touches these
  files" already gives encryption a natural home — the daemon decrypts
  once at startup and is the only process that ever needs the key in
  memory — but that's a starting point for how encryption *could* fit
  the daemon shape, not a decided mechanism. Locking in SQLCipher vs.
  OS-level vs. something else needs the project lead's input (§6) before
  any of this is built.

---

## 6. Decisions — resolved by the project lead, plus what's still genuinely open

Six of the seven originally-open questions were resolved with defensible
defaults, confirmed by the project lead. Recorded here for the record
(each also referenced from wherever it affects §1-4/§7):

1. **Daemon lifecycle — resolved: auto-start on first use, no separate
   install step.** Not a macOS LaunchAgent / Windows service. Simpler
   for users; whichever client (CLI, desktop app, or the MCP proxy) needs
   the daemon first starts it. See §7.3.
2. **Discovery — resolved: OS-assigned port + a well-known local file.**
   Avoids a fixed port colliding with anything else on the user's
   machine. See §3's "Resolved" note and §7.2.
3. **MCP transport — resolved for now: stay on stdio.**
   `jarvis.cmd.mcp_server` keeps its existing stdio contract with an MCP
   client and becomes a proxy to the daemon (§4) rather than moving to
   streamable-HTTP/SSE. Revisiting the transport itself is an explicitly
   separate, later roadmap item - not decided or foreclosed here.
4. **Daemon-not-running behavior — resolved: auto-start, no manual
   step.** Same mechanism as #1 - a client that can't reach the daemon
   at the discovered (or default) port starts one and waits for it,
   rather than erroring out or requiring a person to run a separate
   command first. See §7.3.
5. **Concurrency policy — resolved: WAL mode, reads proceed during a
   write; writes serialize.** WAL was already researched and approved
   for this project separately; enabling it is what makes "a search
   doesn't wait behind an in-flight ingest" possible at all. See §4's
   "Resolved: concurrency" note and §7.1.
6. **Access control on the local endpoint — resolved for now: loopback
   binding only.** No shared-secret token yet - there's no encryption
   layer decided yet for a token to meaningfully protect alongside, so
   adding one now would be security theater dressed as a mitigation.
   Revisit if/when #7 (encryption) is decided, since that decision may
   change what "protect this endpoint" even means.
7. **Scope of "the desktop app" — resolved: build against the CLI and
   desktop app as they're actually being built now**, not a hypothetical
   future version. This doc and the implementation in §7 reflect what
   exists today, not speculative flexibility for a not-yet-real client.

**Still genuinely open — needs the project lead's input before it's
built:**

- **Encryption mechanism (§5).** SQLCipher, OS-level
  (BitLocker/FileVault/DPAPI/Keychain), or something else — for
  `jarvis.db` and the LadybugDB graph file separately, since they may
  not have the same answer, and whether it's in scope for a near-term
  follow-up or further out. **Explicitly treated as a hard stop**: the
  daemon in §7 is built so encryption can be added later (one process
  already owns both files, and that process is the natural place to
  hold a decryption key in memory) without a structural redesign, but no
  specific scheme is implemented, assumed, or stubbed in.
- **Unrelated but discovered along the way (§1c), now fixed as part of
  this work (§7.1)**: `jarvis.db` used to be opened with neither WAL
  mode nor a busy timeout. Both are now set in `get_connection()` itself
  - see §7.1 for the real pragma verification.

---

## 7. Implementation (first pass)

What's actually built, where, and what it does and doesn't cover yet.

### 7.1 `jarvis/internal/db/connection.py` - WAL + busy timeout

`get_connection()` now runs `PRAGMA journal_mode=WAL` and
`PRAGMA busy_timeout=5000` (5s) on every connection it opens, before
loading `sqlite-vec`. Verified with a real pragma read-back, not
assumed - `tests/test_daemon.py::test_get_connection_enables_wal_and_busy_timeout`
opens a connection, asserts `PRAGMA journal_mode` reports `wal` and
`PRAGMA busy_timeout` reports a positive value, then opens a **second**,
independent connection to the same file and confirms it reports `wal`
too without anyone setting it again - WAL is recorded in the database
file's own header, not per-connection state, so this is the real
persistence property, not a coincidence of connection order.

This single change is what makes §6.5's "reads proceed during an
in-flight write" true; without it, this repo would still have exactly
the lock behavior §1c described regardless of anything else in this doc.

### 7.2 `jarvis/modules/daemon/server.py` - the daemon

A Starlette app (reusing the pattern already proven in
`jarvis/modules/debug_ui/server.py`) with routes: `/health`, `/search`,
`/get_hierarchy`, `/browse`, `/related`, `/query_db`,
`/ingest/filesystem`, `/ingest/drive`. Every route calls the existing
public function (`search()`, `get_hierarchy()`, `browse()`, `related()`,
`query_db()`, `run_ingest()`) exactly as it already worked - nothing
about those functions' logic changed, confirmed by
`tests/test_daemon.py`'s equivalence tests (§7.4).

**A real, Windows-specific bug found and fixed while building this**:
the natural-looking approach - bind a socket for the OS-assigned port
ourselves, then hand uvicorn the file descriptor via `uvicorn.run(app,
fd=sock.fileno())` - fails immediately on Windows. uvicorn's `fd=` path
does `socket.fromfd(config.fd, socket.AF_UNIX, socket.SOCK_STREAM)`
(confirmed by reading `uvicorn/server.py`), and `socket.AF_UNIX` does
not exist on Windows' `socket` module at all - `AttributeError` before
the server ever binds. Fixed by driving `uvicorn.Server` directly and
passing the already-bound, already-listening socket via
`server.run(sockets=[sock])` instead - that path takes a plain
`socket.socket` and works identically on both platforms (verified: the
daemon starts, writes its discovery file, and answers `/health` in a
real subprocess on this Windows machine - `tests/test_daemon.py`'s two
process-lifecycle tests).

Route handlers never hold one shared connection - see §4's "Resolved:
concurrency" note for why not. Writes go through `_WRITE_LOCK`
(`asyncio.Lock`); reads don't.

`JARVIS_DB_PATH` and `JARVIS_DAEMON_HOME` environment-variable overrides
were added to `jarvis/internal/db/connection.py` and
`jarvis/modules/daemon/{server,client}.py` respectively, for exactly one
reason: a real daemon *subprocess*, spawned fresh by a test, has no
in-process Python object a test can monkeypatch (every other test in
this repo patches `db.get_connection` or similar directly). Without
these, testing "does the daemon process actually start, write its
discovery file, and answer `/health`" would have meant either not
testing it for real, or risking a test touching the developer's actual
`<repo>/jarvis.db` and `.jarvis_daemon.*` files. Not used anywhere
outside tests today.

### 7.3 `jarvis/modules/daemon/client.py` - auto-start

`ensure_daemon_running()`: read the discovery file, confirm the daemon
it names is actually alive via `/health` (not just "the file exists" -
a stale file from a crashed daemon is a real case this distinguishes),
and if nothing is reachable, spawn `python -m jarvis.cmd.daemon` as a
detached subprocess (`DETACHED_PROCESS` on Windows, `start_new_session`
on POSIX - so it outlives the client that started it) and poll for it
to come up.

**Known, accepted gap, not solved in full**: two clients that both
observe "no daemon reachable" at the same instant could both decide to
spawn one. Mitigated, not eliminated, with an atomic lock file
(`os.open(..., O_CREAT | O_EXCL)`) that a second spawner fails to
acquire and falls back to polling instead of also spawning; a lock file
older than 30s is treated as left over from a crashed spawner and
reclaimed. This narrows the race to a much smaller window (between
losing the lock race and the winner's daemon actually becoming healthy)
rather than closing it entirely - a genuinely simultaneous first launch
of two clients is still a real, if unlikely, way to end up with two
daemons briefly racing for the same port range. Worth a more robust fix
(e.g. an OS-level named mutex) if this turns out to matter in practice.

### 7.4 What's tested (`tests/test_daemon.py`, 15 tests)

- WAL + busy timeout, via a real pragma check (§7.1).
- `/health`.
- `/search`, `/get_hierarchy`, `/browse`, `/related`, `/query_db`: each
  compared directly against calling the underlying function in-process
  with the same seeded data, plus one error-path test per route
  confirming the daemon reports the same `{"error": {...}}` shape a
  direct call already did.
- `/ingest/filesystem`: compared against a direct `run_ingest()` call on
  an independent db + corpus (same pulled/chunks_created/embed/hierarchy
  results), plus a follow-up `/search` through the same route confirming
  the ingested content is genuinely retrievable afterward. Also confirms
  a nonexistent folder is *not* treated as an ingest failure (matches
  `FilesystemConnector`'s existing graceful-walk behavior - verified by
  calling it directly first, not assumed).
- `/ingest/drive`: a real error path (empty `folder_id`, which
  `DriveConnector.__init__` already rejects) reported the same way.
- Two real-subprocess tests: the daemon process actually starts, writes
  its discovery file, answers `/health`, and stops on `SIGTERM`; and
  `ensure_daemon_running()` genuinely spawns one when nothing is
  listening, then reuses it (not a second spawn) on a second call.

**Not tested**: genuine concurrent-request behavior (a real search
request actually overlapping in time with a real in-flight ingest) -
the equivalence tests above prove each route's *result* is correct, not
that reads and writes race safely under real concurrent load. Building
a reliable, non-flaky test for that was judged more risk than the
remaining coverage gap justified for this pass; worth adding if the
concurrency policy (§6.5) is ever revisited or tightened.

### 7.5 `jarvis/cmd/mcp_server.py` - the proxy

Rewritten to stop importing `jarvis.modules.mcp_tools.search` /
`hierarchy` / `sql_query` - each of those modules registers its
`@server.tool()` onto the shared server as an import side effect, so
importing them here would have double-registered every tool name
against the proxy versions. Instead, `mcp_server.py` defines its own
`search()` / `get_hierarchy()` / `browse()` / `related()` / `query_db()`
- same names, same signatures - each forwarding to the daemon over HTTP
and parsing the response back into the exact same Pydantic models a
direct call would return. `QueryResult` moved from `sql_query.py` to
the shared `models.py` (alongside every other tool result model) so the
proxy could reference it without importing `sql_query.py` itself and
triggering that same double-registration problem.

Confirmed by direct import inspection (not assumed): after this change,
`jarvis.modules.mcp_tools.server.server` has exactly five registered
tools - `browse`, `get_hierarchy`, `query_db`, `related`, `search` -
each the daemon-proxy version, none double-registered.

**Side effect worth knowing**: `jarvis/modules/debug_ui/server.py`
spawns `python -m jarvis.cmd.mcp_server` and talks MCP-over-stdio to it
- it will keep working, but every tool call it makes now requires a
reachable (or auto-startable) daemon, where it previously worked
standalone. Not covered by an automated test (there was none before
this change either - `tests/` has no `test_debug_ui.py`), but worth
a manual check before relying on the debug console.

### 7.6 Scope gap at the time - see §4 and §8

As originally built, `jarvis/cmd/filesystem_ingest.py` and
`jarvis/cmd/drive_ingest.py` were **not** converted to daemon clients -
see §4's "Scope gap" note for the original reasoning (risk to
`tests/test_drive_ingest.py`'s existing monkeypatch-based coverage).
**This is closed as of §8** - both entrypoints now route their ingest
call through the daemon.

---

## 8. CLI conversion (second pass)

The scope gap flagged in §4/§7.6 - the CLI ingest entrypoints bypassing
the daemon entirely - is closed. This also directly answers a
clarification from the project lead: the daemon is meant to own "the
actual processing" (organize, labeling, embedding), specifically so the
team can change that logic without requiring users to update the
desktop app. Before this pass, that was only true for the MCP-server
path - a CLI-run ingest still executed `pipeline.run_ingest()`
in-process, using whatever version of that logic shipped with the CLI
itself. It's true for both paths now.

**What changed, in both `filesystem_ingest.py` and `drive_ingest.py`:**
the `conn = db.get_connection(...)` / `run_ingest(conn, connector, ...)`
call is replaced with `daemon_client.run_ingest_filesystem(path, ...)` /
`daemon_client.run_ingest_drive(folder_id, ...)` - the same typed
wrapper `jarvis/cmd/mcp_server.py` uses for its own daemon calls (§7.5),
so the request shape and daemon-unreachable handling (§10) live in one
place, not one copy per caller.

**What deliberately stayed local, and why:**

- **Google Drive OAuth sign-in and the interactive folder picker**
  (`drive.get_drive_service()`, `pick_folder()`) - only the final
  `run_ingest(...)` call moves to the daemon. Moving OAuth/listing
  behind the daemon too (so the daemon, not the CLI, holds Drive
  credentials) is a real follow-up, deferred as its own phase - it's a
  bigger, separate change (credential storage/lifetime inside a
  long-running background process is a different problem than a
  one-shot CLI process holding them for the duration of one run) and
  wasn't part of what was asked for here.
- **Migrations** (`run_migrations(args.db)`) - still run locally, via a
  short-lived, separate `sqlite3.connect()` (`jarvis/cmd/migrate.py`),
  before the ingest call. This is schema DDL against a
  `schema_migrations` bookkeeping table, not the corpus data path the
  single-owner requirement is actually about, and WAL mode + the busy
  timeout (§7.1) make a brief, non-overlapping connection from a second
  process safe by the same mechanism that already makes the daemon's own
  reads safe alongside its writes. The daemon's own `_preflight()`
  (`jarvis/modules/daemon/server.py`) already assumed migrations run as
  a separate, explicit step before it starts (it exits with instructions
  to run `python -m jarvis.cmd.migrate` if the db doesn't exist yet,
  rather than running migrations itself) - keeping this step local to
  the CLI matches a decision the codebase had already made for the
  daemon side, not a new one made here.
- **A flagged, narrowed side effect of the above: the `--db PATH` flag.**
  It still exists on both entrypoints, but now only controls which file
  the local migration step targets - the actual ingest request carries
  no db path at all (the daemon always writes to its own database,
  `JARVIS_DB_PATH` env var or the default, since it's the sole process
  that opens it). Before this pass, `--db custom.db` fully redirected
  where ingested content went; now it does not. The flag is kept rather
  than removed (no breaking CLI change) and both entrypoints' `--help`
  text says so explicitly - a deliberate, visible narrowing, not a
  silent behavior change.

**Not done, and explicitly not invented for this pass:** the earlier
plan sketch mentioned "`run_ingest_folder` stays local (file-walking
only)" as a rough description of what the CLI's local orchestration
would look like. No function by that name exists anywhere in this
codebase, and there is no separable "walk-only" step to give it -
`FilesystemConnector.sync()` fuses walking, reading, and content-hash
comparison into one call, and that hasn't changed. Introducing a
same-named local function purely to match that phrasing, when it
couldn't actually do only file-walking, would have been a name in
search of a purpose rather than a real piece of structure - so
`main()`'s existing shape (confirm -> call the daemon -> print the
summary) was kept as-is, with only the `run_ingest(...)` call site
itself replaced. Flagging this interpretation plainly rather than
silently picking one.

**Test coverage:**

- `tests/test_drive_ingest.py`'s 4 `main()` tests were rewritten to
  monkeypatch `drive_ingest.daemon_client.run_ingest_drive` and assert
  on the request payload, instead of `drive_ingest.db.get_connection` /
  `drive_ingest.run_ingest` - the 6 `pick_folder()` tests were untouched
  (they never touched either). 3 new tests cover the daemon-unreachable
  tiers (§10) reaching `main()` as a clean message. 13 tests total, all
  passing.
- `jarvis/cmd/filesystem_ingest.py` had **zero** test coverage before
  this pass (confirmed via `git ls-files tests/` - no
  `test_filesystem_ingest.py` existed), so its new
  `tests/test_filesystem_ingest.py` (6 tests) establishes the
  daemon-routed contract directly rather than migrating existing
  coverage: the happy path, the confirm-prompt decline path, a
  daemon-side ingest error (a plain `{"error": ...}` dict, not an
  exception) reported cleanly, and all three daemon-unreachable tiers.

---

## 9. Full test suite

Run fresh, after both implementation passes, on top of everything
already merged: **394 passed, 2 skipped, 0 failed** (up from 338 after
the first daemon pass, §7's own count - the +56 is 20 new tests in
`tests/test_daemon_error_handling.py`, 27 in the new
`tests/test_mcp_server.py`, 6 in the new `tests/test_filesystem_ingest.py`,
and a net +3 in `tests/test_drive_ingest.py`, whose 4 rewritten `main()`
tests plus 3 new daemon-tier tests replace the 4 originals). No
regressions elsewhere despite `get_connection()` setting WAL mode on
every connection everywhere in the codebase, both
`jarvis/cmd/mcp_server.py` rewrites, `QueryResult` moving from
`sql_query.py` to `models.py`, and the CLI conversion in §8.

---

## 10. Daemon-unreachable error handling

Requested directly: this CLI (and the MCP proxy) ship inside a packaged
app for real end users, so a raw `ConnectionRefusedError` traceback
reaching a user is not acceptable, and `mcp_server.py` had **no**
handling for this at all before this pass - a real, separate bug (if the
daemon crashed mid-session, an MCP client like Claude Desktop would hang
or fail with an unexplained error rather than a clean message).

**Three tiers**, all defined in `jarvis/modules/daemon/client.py` as
`DaemonError` subclasses:

1. **`DaemonStartupError`** - auto-start itself failed: the daemon's
   process couldn't be launched (e.g. `subprocess.Popen` raised
   `OSError`), or launched but never answered `/health` within the
   startup timeout (15s). Also subclasses `RuntimeError`, so the
   pre-existing `except RuntimeError` handling in both CLI entrypoints
   keeps working unchanged even though it now catches a more specific
   type than before.
2. **`DaemonConnectionError`** - a request to a daemon that was
   reachable moments ago (per the discovery file / a prior health check)
   failed outright: connection refused/reset, it responded with
   something that isn't valid JSON, or it responded with a non-2xx
   status that isn't one of the daemon's own deliberate `{"error":
   {"code", "message"}}` envelopes (below). The non-JSON case is
   deliberately folded into this tier rather than given a fourth one - a
   real end user can't act differently on "the daemon refused the
   connection" vs. "the daemon sent back garbage," so a fourth category
   would only add branches without adding a meaningfully different next
   step.

   **Status code is now checked, not ignored (real bug, fixed).**
   `_do_request()` used to hand back `resp.json()` unconditionally,
   regardless of `resp.status_code` - a non-2xx response was silently
   treated exactly like a 200, relying entirely on every current *and
   future* caller happening to check for an `"error"` key that isn't
   guaranteed to be there at all (a bare Starlette error page, a 404, a
   validation failure - none of those carry that shape). Fixed at the
   source: `_do_request()` now raises (routed through the exact same
   one-retry-then-tier-2/3 machinery as a malformed body, since both are
   "the daemon responded but not usably") for any non-2xx response
   whose body isn't recognizably one of the daemon's own error
   envelopes - `_is_error_envelope()` in `client.py`. A non-2xx response
   that *does* carry that envelope (currently: the two ingest routes'
   except blocks) is unaffected and still flows through as plain data,
   exactly as documented above and below - this closes the unchecked gap
   without touching the deliberate, already-tested ingest contract.
   Covered by `tests/test_daemon_error_handling.py`: a malformed 500/400
   raises `DaemonConnectionError` (with the normal one retry first), a
   well-formed 500 error-envelope still returns unchanged with no retry,
   and a read wrapper still surfaces the malformed case as an
   `ErrorResponse` exactly like any other tier-2 failure.
3. **`DaemonTimeoutError`** - the same situation as #2, but the failure
   was specifically a timeout (the daemon accepted the connection but
   never responded). Kept as its own tier, even though the user-facing
   message is identical to #2's, because "refused" and "hung" are
   different real symptoms worth being able to tell apart in the log
   file (below) even when the person looking at the screen doesn't need
   to.

**Original proposal for the messages included an example - "Check if
port XXXX is already in use by another program" - that didn't match
this architecture and was corrected before implementation**: the daemon
binds an OS-assigned ephemeral port specifically so there is no fixed
port for a user to check (§3/§6 resolved decision #2) - "which port" is
not information a user could act on even if surfaced. The actual
messages shown to a user (`_USER_MESSAGES` in `client.py`):

- Tier 1: *"The Jarvis background service didn't start in time. Try
  restarting the app."*
- Tiers 2 and 3: *"The Jarvis background service isn't responding. Try
  restarting the app."*

**One automatic retry, not a retry loop** (the one addition to the
original proposal): every real HTTP request made by `call()` - not
`ensure_daemon_running()`'s own ~15s startup poll loop, which is a
separate, already-existing, longer mechanism - gets exactly one retry
after a 1.5s pause on a connection failure or a timeout, before tier 2
or 3 is raised. This is a narrow mitigation for the daemon dying in the
split second between a health check and the real request that follows
it, not a general resilience mechanism - confirmed by
`tests/test_daemon_error_handling.py`, which counts actual HTTP attempts
directly (a fake `requests.request` incrementing a counter) and asserts
exactly 2 attempts on a persistent failure, exactly 1 on immediate
success, and the correct 2-attempt/success-returned outcome when the
first attempt fails and the second succeeds.

**Read tools vs. ingest: different error shapes, on purpose.** `search`
/ `get_hierarchy` / `browse` / `related` / `query_db` catch all three
tiers internally and return an `ErrorResponse` (`{"error": {"code",
"message"}}`, `code` is `"daemon_timeout"` for tier 3 and
`"daemon_unavailable"` for tiers 1-2) - the existing MCP wire contract
already returns errors as data, not raised exceptions (`sql_query.py`'s
own docstring documents why: a raised exception loses the code/message
distinction by the time it reaches an MCP client, confirmed empirically
via `try_mcp_error_probe.py`). `run_ingest_filesystem` /
`run_ingest_drive` let the exception propagate instead - the CLI already
has an exception-based error convention for ingest (`except RuntimeError`
etc. in `main()`), and there's no established "error variant" of a plain
ingest-stats dict to invent one for.

**`jarvis/cmd/mcp_server.py`, fixed to match.** Before this pass, its
five tool-proxy functions called `call(...)` directly with no exception
handling around it at all - any `DaemonError` would have propagated out
of the tool function as a raised exception, exactly the "loses the
code/message distinction" problem `sql_query.py` already documented for
a different case. Now each tool proxy calls the matching
`daemon.client` wrapper (`daemon_client.search`, etc.) directly, which
already implements the catch-and-return-`ErrorResponse` behavior above -
so the fix was to stop duplicating request-building logic in
`mcp_server.py` at all, not to add a second copy of the tier/retry
handling. `main()` separately calls `ensure_daemon_running()` once up
front, before starting the stdio server, so a tier-1 failure exits
cleanly with a logged reason instead of starting a stdio handshake that
would then fail on every subsequent tool call. Covered by
`tests/test_mcp_server.py` (27 tests) - there was no test file for this
module at all before this pass.

**Diagnostic logging.** `jarvis.internal.logging.logging_config` only
configures `logging.basicConfig()` with no `filename=`, i.e.
stderr-only - fine for a terminal, invisible in a packaged app with no
visible console, which is exactly the situation a support conversation
about "the service isn't responding" needs real detail for. The calm,
short message above is what a user/LLM caller sees; the full
`logger.exception(...)` detail (real exception type, traceback, which
HTTP method/path, whether it was the first attempt or the retry) now
additionally goes to `<repo>/.jarvis_daemon.log` (or
`$JARVIS_DAEMON_HOME/.jarvis_daemon.log`) - the same file the daemon
subprocess's own stdout/stderr is already redirected to by
`_spawn_daemon()`, so there is one file to check, not two. The
file-handler attachment is wrapped in `try/except OSError` so an
unwritable log directory degrades to stderr-only logging rather than
breaking `import jarvis.modules.daemon.client` outright.

---

## 11. Summary

One local background process (`jarvis.cmd.daemon` /
`jarvis.modules.daemon.server`, new and separate from
`jarvis.cmd.mcp_server`) is the sole owner of `jarvis.db` for every
client - the MCP-server proxy (§7.5) and, as of §8, both CLI ingest
entrypoints. It auto-starts on first use (no separate install step), is
discovered via an OS-assigned port written to a local file, and is
talked to over local HTTP/JSON on `127.0.0.1`. WAL mode + a busy timeout
(newly added to `get_connection()`) let reads proceed during an
in-flight write; writes serialize against each other because the
LadybugDB graph file cannot tolerate two writers regardless of SQLite's
own locking. None of `run_ingest()`, `search()`, `get_hierarchy()`,
`browse()`, `related()`, or `query_db()`'s actual logic changed - only
who's allowed to call them, and how a request reaches them - which is
also what makes the daemon the sole owner of "the actual processing"
(organize/label/embed), not just the connection, satisfying the project
lead's clarification on that point (§8).

A daemon that's unreachable - won't start, refuses the connection, or
hangs - no longer surfaces as a raw traceback anywhere in this codebase:
both CLI entrypoints and the MCP proxy now show one of two short, calm
messages (§10), with a single automatic retry absorbing a
daemon-died-at-the-wrong-instant blip before either message is shown,
and full diagnostic detail preserved in a log file for anyone who needs
to actually debug it.

**One thing this pass does not close, flagged rather than silently left
implicit: encryption at rest (§5) remains completely open by design** -
a hard stop pending the project lead's input on mechanism, not an
oversight; the daemon's single-owner shape gives it a natural home
whenever that decision lands, but nothing about encryption has been
implemented, assumed, or stubbed in.
