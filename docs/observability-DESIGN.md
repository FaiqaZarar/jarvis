# Observability and error handling: design

**Status:** built and tested end to end in the real app (15 Oct 2026); in review. Telemetry, error codes and the reliability fixes are in #199; the error banners and opt-out UI in #200. The PostHog project (EU) has the **Jarvis – Overview** dashboard and a daily email digest; new errors open a GitHub issue automatically (§10). Remaining work is listed in §13.

**Source:** the 4 Oct 2026 planning meeting. That week's priorities were (1) a tracing stack, (2) usage visibility, and (3) error handling and messaging driven by the actual failure, done proactively. The goal is a working demo on Sun 11 Oct, ahead of collecting 5-10 outside testers over the following 2-3 weeks. In the same meeting the project lead also asked for alerts on the top 10 most-recurring errors.

**Decision:** OpenTelemetry for instrumentation, PostHog as the backend (§2). User data stays on the user's machine. Only diagnostics leave it (§5).

---

## 1. Goals

When Jarvis runs on a tester's machine, the team should be able to answer the following without asking the tester for anything:

1. **What broke, where, and why.** Which sync phase failed, which error, on which app version and OS, and for which tester.
2. **Whether testers use it.** Daily active users, syncs, searches, and Claude tool calls.
3. **What to fix first.** The top 10 most-recurring errors, delivered on a schedule, plus a real-time alert for anything new or spiking.

Separately, **the user is told what happened and how to fix it on their own machine.** This includes the full detail (file names, paths), which never leaves the machine (§6).

**Non-goals for this pass:**
- Server-side (Railway) tracing.
- Session replay.
- Sending any document content anywhere.

---

## 2. Decision: OpenTelemetry + PostHog

The options were PostHog and Grafana Cloud (Tempo/Loki/Prometheus). Both are free at tester scale.

| Need | PostHog | Grafana Cloud |
|---|---|---|
| Usage (daily active users, retention, feature use) | Built in | Not a product-analytics tool. It would have to be built from log queries. |
| Errors (grouping, counts per user, alerts) | Error tracking, Python SDK | Only as log lines or failed spans. No error inbox. |
| Traces (per-phase timing, failing step) | Accepts OTLP traces. **Beta.** | Tempo, mature |
| Desktop client | Python SDK works anywhere | Faro is browser-only. N/A for PySide6. |
| Key shipped inside the installer | Project key is write-only by design. Safe to ship. | The token is a real credential. It needs a relay on our server first. |

**PostHog covers all three goals in one tool**, which matters most right now, when the question is "what are testers doing and hitting". Grafana's strength is backend monitoring. That becomes relevant once the server roadmap (LLM routing, sync, hosted MCP) carries real traffic.

**Lock-in is avoided by instrumenting with OpenTelemetry.** Traces are standard OTLP, so moving them to Grafana Tempo later is an endpoint and key change, not a code change. The likely long-term shape is PostHog for product usage and client errors, plus Grafana for the server once it matters.

**Accepted risk:** PostHog's tracing is in beta. If it falls short, the OTLP exporter points elsewhere and nothing else changes.

**Free tier (per month):** 1M events, 100k exceptions, 50 GB logs. Ten testers at roughly 200 events a day comes to about 60k events a month.

---

## 3. Current state (verified in code)

What already exists, and the gaps this design closes:

- **Logging** is stderr-only `basicConfig` (`jarvis/server/jarvis_server/internal/logging/logging_config.py`).
  - Only the daemon-client logger also writes to `LOG_FILE`, which is hardcoded to `~/Library/Logs/Jarvis/jarvis_daemon.log` (`jarvis/modules/daemon/client.py:106`). That is a macOS path, used on Windows too.
  - The file is never rotated.
  - Other desktop-process logs are lost in a windowed build.
- **Error codes** are mostly generic.
  - Any ingest failure comes back as `{"code": "internal_error", "message": "TypeName: msg"}` (`jarvis/server/jarvis_server/daemon/server.py:531`), so a bad API key and expired Drive auth differ only in message text.
  - Domain errors are plain `RuntimeError`/`ValueError`: Drive auth (`connectors/drive/auth.py`), embedding 401/403 (`internal/embed/provider.py`) and LLM output.
- **Persisted failures:**
  - `ingest_runs.status` records `failed`/`interrupted` but **not why** (`migrations/0016_ingest_runs.sql`).
  - Per-file read errors are counted, never stored (`connectors/drive/sync.py`).
- **Timing** exists only for MCP tool calls and LLM calls (`tool_calls.duration_ms`). Sync phases are not timed.
- **Auto error reports** (`jarvis/modules/desktop_app/error_reporting.py` → cloud `/errors/report` → GitHub issue) work only when signed in. Their one call site is a Drive connect failure.
- **UI:**
  - Ingest failures show a modal (`show_ingest_error_dialog`), sometimes with the raw `TypeName: msg` text.
  - DESIGN.md §5.4 says failures never use a modal and never show raw backend text. This design gives the UI the error codes it needs to follow that.
- **A likely false failure:** an ingest is one blocking POST with a 30-minute timeout (`_INGEST_TIMEOUT_S`, `jarvis/modules/daemon/client.py:146`). An organize run can take about 2.5h, so a large first sync would surface as "background service isn't responding" while the service is still working. Not yet reproduced. It is the first thing to check (§11).

---

## 4. Architecture

```
Desktop app (PySide6)                Local background service (daemon)
   │  telemetry.init("desktop")         │  telemetry.init("daemon")
   ├── OTel spans ──────────────┐       ├── OTel spans ─────────────┐
   ├── PostHog events           │       ├── PostHog events          │
   └── PostHog exceptions       │       └── PostHog exceptions      │
                                ▼                                   ▼
                   ┌──────── privacy filter (allow-list, §5) ────────┐
                   └──────── batched, async, dropped when offline ───┘
                                         │
                                         ▼
          PostHog: /i/v1/traces (OTLP) · events · error tracking · /i/v1/logs
```

**One module, `jarvis_server/internal/telemetry/`,** used by both processes. The desktop app already imports `jarvis_server`, so no new package boundary is needed. It owns the following:

- Setting up the OTel `TracerProvider` with an OTLP/HTTP exporter, the PostHog client, and an OTel log handler for WARNING and above.
- Resource attributes:
  - `service.name` (`jarvis-desktop` / `jarvis-daemon`)
  - `service.version` (the embedded `VERSION` tag)
  - `os.type`
  - `install.id`
- The privacy filter (§5). **Every outgoing span, event, exception and log passes through it.** No call site talks to PostHog or the exporter directly.
- Batching and shutdown flush. Exporter failures are swallowed, so telemetry can never raise into, block, or slow down the app.
- The off switch (§9).

**Configuration:**
- The PostHog project key and host are baked in at build time, the same way the existing `_bundled_credentials.py` step works in `.github/workflows/build.yml`.
- A source checkout has no key, so **telemetry is off in dev by default** and dev runs never pollute the tester project.
- An env var (`JARVIS_POSTHOG_KEY`) turns it on locally for testing.

**Identity:**
- `install.id` is a random UUID, created on first launch and stored in app data. Events go under it before sign-in, so setup failures before sign-in are still visible.
- At sign-in the install ID is linked to the cloud **user ID**, using PostHog's alias/identify, so earlier events join the tester's history.
- **Email is never sent.** If `/auth/verify` doesn't already return the user ID, it is added there.

**New dependencies:**
- `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `posthog`.
- These need checking under PyInstaller on all three platforms (hidden imports), as with the earlier packaging investigation.

---

## 5. Privacy: what leaves the machine

**Rule:** user data stays on the user's side. Only diagnostics are sent.

| Sent | Never sent |
|---|---|
| Error code and exception type | Document text, chunks, embeddings |
| Stack trace (code file names and line numbers only) | File names, folder names, paths, Drive IDs |
| Timings and counts ("312 files, 3 failed, 4 min") | Search queries, MCP tool inputs and outputs |
| Event names | Topic labels (generated from content, so they count as user data) |
| App version, OS, install ID / user ID | Email, local variables, raw exception messages |

**Two leaks guarded against explicitly:**
1. **Log messages.** Only the template is sent (`record.msg`, e.g. `"Failed to read %s"`), never the formatted message or `record.args`.
2. **Exception messages.** `FileNotFoundError`, Drive API errors and others embed paths or file names in `str(exc)`. Only the type, our error code and the stack frames are sent. The message is dropped.

**Enforcement:**
- **The allow-list filter in the telemetry module.** Attributes and properties not on the list are dropped, not passed through. A new field has to be added to the list deliberately.
- **A test** that runs a sync over a fixture folder with a recognisable file name and content. It captures everything the exporter and PostHog client would send, and fails if the file name, path or content appears anywhere in it.
- **PostHog project settings:**
  - "Discard client IP data" is on.
  - Capture of local variables on exceptions is off (it is also off in the SDK setup).
  - Session replay is not used.

---

## 6. Error codes and user-facing messages

One classification produces **two outputs:**
- **Locally, to the user:** full detail (which files, which folder) and one action, worded per DESIGN.md §5 and §6.
- **To PostHog:** the code, phase and counts only (§5).

**Classification lives in the daemon.** A `classify(exc) -> ErrorCode` function maps known exception types and HTTP statuses to a code. The ingest route returns `{"error": {"code": <code>, "message": <calm text>}}` instead of `internal_error` plus the raw text.

| Code | Raised from | User sees (DESIGN.md §6) | Action |
|---|---|---|---|
| `embedding_key_invalid` | `internal/embed/provider.py` 401/403 | Paused. Jarvis needs a valid API key to read your files. Everything already saved is safe. | Fix API key |
| `drive_auth_expired` | `connectors/drive/auth.py` `RefreshError` | Paused. {n} of {total} files are saved. Google Drive needs you to sign in again. | Reconnect Drive |
| `file_read_failed` | connector sync, per file | Mostly done. {n} files couldn't be read. The other {m} are saved. (Expandable list of the files and reasons, local only.) | Retry |
| `cancelled` / `interrupted` | `IngestCancelled` / orphaned run at startup | Stopped. {n} of {total} files are saved. Retry picks up where it left off. | Retry |
| `service_unreachable` | `DaemonError` tiers (desktop side) | The background service isn't running. Everything already saved is safe. | Restart service |
| `network_unavailable` | connection errors to Drive or embedding API | *New, draft:* Paused. You're offline. Everything already saved is safe. | Retry |
| `rate_limited` | 429 from Drive / embedding / LLM | *New, draft:* Paused. A service Jarvis uses asked it to slow down. Everything already saved is safe. | Retry |
| `folder_not_found` | missing local root | *New, draft:* Jarvis can't find {name}. It may have been moved or the drive unplugged. | Choose folder |
| `organize_failed` | `organize/clustering.py`, reconcile | *New, draft:* Your files are saved and searchable. Jarvis couldn't group them into topics this time. | Retry |
| `unknown` | anything unclassified | Something went wrong. Everything already saved is safe. | Retry, Send feedback |

**As built (step 2):** `jarvis_server/internal/errors.py` classifies by exception type and HTTP status, following the `raise ... from` chain. Three typed exceptions were added at the raise sites (`EmbeddingAuthError`, `DriveAuthRequired`, `RateLimitedError`), all still `RuntimeError` subclasses. `folder_not_found` is not built: the filesystem connector treats a missing folder as an empty sync, not a failure, so nothing raises it today. `service_unreachable` stays the desktop's existing `DaemonError` handling.

Rows marked *new, draft* are additions to the DESIGN.md §6 catalogue and go through the same design review as the rest of it. Wording follows §5.2 vocabulary: "background service", "Sync", "files".

**Stored locally (new migration, `0020`):**
- `ingest_runs.error_code`: why a run failed, not just that it did. This lets the next launch show "Interrupted last time" with the real reason.
- A new `ingest_run_file_errors` table (`run_id`, `file_ref`, `error_code`):
  - Lets Retry pick up only the failed files.
  - Lets the user see which files failed and why.
  - **Never leaves the machine.** Only its row count is sent.
- *As built (step 3):* migration `0020_ingest_run_errors.sql`; the table's columns are `run_id`, `source_id`, `display_name`, `error_code`. Connectors report failed files through `_common.record_file_error` (the same "pipeline pops it from the sync stats" pattern as `ext_stats`), so they never need the run id. Retry-only-failed-files still needs connector support and is not built yet.
- *Opt-out (step 3, backend only):* `env_config.save_telemetry_enabled()` writes `JARVIS_TELEMETRY` to `<home>/.env`, which both processes read at startup.
- *UI (#200):* sync failures now use the Sources page's error banner, never a modal, with the action for the code: **Fix API key** (saves the key, restarts the background service, retries - ticket 05's single guided action), **Reconnect Drive** (signs in again, then retries), **Retry** otherwise, plus a quiet **Send feedback** for `unknown`. Drive-connect and cancel failures moved to the same banner. The banner gained DESIGN.md §4.2's optional quiet secondary action; the old failure dialog is gone. Settings has a **Privacy** section with the "Share usage and diagnostics" toggle. A one-time notice on the Sources page was built and then dropped after review; the Settings toggle, with its helper text, is where the user learns about and controls sharing.

**Send feedback:** this stays a user choice. The dialog shows exactly what will be attached. File names are included only if the user ticks a box.

**UI changes:**
- Failures move from the modal to the banner/row with one action, per DESIGN.md §4.2 and §5.4.
- Each action button performs the fix directly (Reconnect Drive, Fix API key, Choose folder) rather than describing it.
- The parallel UI/UX work touches the same surfaces, so ownership of this layer should be agreed up front (§12).

---

## 7. Traces

**One trace per sync**, started in the daemon's `_run_ingest_for`, with a child span per `pipeline.PHASE_*`:
- Reading files
- Processing documents
- Generating embeddings
- Organizing into topics
- Reconciling

**Other spans:**
- Drive sign-in and token refresh (desktop process).
- Embedding API batches and LLM labelling calls (these replace nothing; `tool_calls` stays as the local record).
- Search, plus each MCP tool call routed through the daemon.
- Daemon startup and preflight/migrations.

**Attributes (allow-listed):**
- On spans: `jarvis.connector` (`filesystem` / `drive`), `jarvis.trigger`, `jarvis.phase`, `jarvis.error_code`.
- On phase spans: `jarvis.files.total`, `jarvis.files.failed`, `jarvis.chunks.created`, `jarvis.batches`.

**Failures:**
- `span.record_exception()` (type and stack only, per §5) and status `ERROR` on the span where the failure happened.
- The same failure goes to PostHog error tracking with the trace ID attached, so an error links to its trace.

---

## 8. Usage events

| Event | Properties (allow-listed) |
|---|---|
| `app_opened` | version, os |
| `signed_in` | (none) |
| `source_added` | connector |
| `sync_started` | connector, trigger |
| `sync_finished` | connector, status, error_code, duration_s, files_total, files_failed |
| `search_performed` | result_count, duration_ms (never the query) |
| `mcp_tool_called` | tool, success, duration_ms (never inputs or outputs) |
| `feedback_sent` | severity |

**As built (step 2):** `app_opened`, `sync_started`, `sync_finished` and a single `tool_called` (tool, success, duration_ms). `tool_called` stands in for both `search_performed` and `mcp_tool_called`: the app's own search and Claude's tool calls hit the same background-service routes, and the service can't yet tell them apart. `signed_in`, `source_added`, `feedback_sent` and the install-ID → user-ID link are deferred because they live in desktop UI files the parallel UI pass is changing. Later, `source_removed` (connector, files_total) was added with the Remove action on the Sources page.

Daily active users, retention and the "syncs per day" view come from PostHog's built-in product analytics. No custom aggregation is needed.

---

## 9. Opt-out and disclosure

- **Settings → Advanced:** a "Share usage and diagnostics" toggle, **on by default for the tester builds.** It has a one-line helper: *"Shares error reports and usage counts, never your files or searches."*
- ~~A one-time notice on first launch~~ - dropped after review; the Settings toggle and its helper text carry this.
- **Turning it off stops everything,** both PostHog and OTel, in both processes. The daemon reads the same setting.
- Wording goes through DESIGN.md §5.

---

## 10. Top-10 recurring errors and alerts

**Grouping:**
- PostHog groups exceptions into issues. By default it uses the stack trace and message. Our messages are scrubbed and stacks shift between releases, so **each exception carries an explicit fingerprint: `error_code` + phase** (e.g. `drive_auth_expired` · Reading files).
- One real problem stays one issue and its count is accurate.

**Scheduled digest:**
- A PostHog insight lists the top 10 issues by occurrence count over the period, with distinct users affected per issue.
- It is subscribed to email or Slack (insight subscriptions are on the free plan): daily while testers are active, weekly after.

**Real-time alerts** (PostHog error-tracking alerts):
- **New issue:** an error never seen before, typically right after a release.
- **Spiking:** an existing issue's rate jumps.
- **Reopened:** an issue marked resolved recurs.

**Delivery:** PostHog supports email, Slack or a webhook. It cannot post to WhatsApp. A webhook-to-WhatsApp bridge is possible but deferred until after the demo.

*As set up:* the daily digest is an email subscription to the dashboard (9:00 local time), covering daily active users, errors by type and the top 10 recurring errors. Error-tracking alerts offer chat tools and GitHub, not email, so **"issue created" opens a GitHub issue** in a separate private repository, `jarvis-errors`. It is kept apart from the main repository because PostHog's GitHub app asks for write access to code and workflows, which an alerts-only repository can safely grant. The issue carries only the error type and a PostHog link. "Reopened" and "spiking" offer chat tools only and are not set up yet (§13).

---

## 11. Fixes folded in

1. **Log path.** Move `LOG_FILE` to the platform log location (`%LOCALAPPDATA%\Jarvis\Logs` on Windows, `~/Library/Logs/Jarvis` on macOS, `$XDG_STATE_HOME/jarvis` on Linux). Add a `RotatingFileHandler`, and route all desktop-process loggers to it, not just the daemon client's. Settings' "Open folder" already reads `LOG_FILE`, so it follows automatically.
2. **The 30-minute ingest timeout.**
   - Reproduce first: one organize run longer than 30 minutes.
   - If confirmed, the fix is to make the ingest POST return immediately once the run starts. The desktop then learns the outcome from `/ingest/status`, which it already polls and which already knows the phase. It would gain a terminal `{status, error_code}`.
   - This removes the long blocking request entirely rather than raising the timeout again.
*As built:* (1) `logging_config.default_log_dir()` and `rotate_if_large()` (startup rotation at 5 MB, 3 backups - not a RotatingFileHandler, because several processes append to the one file and Windows can't rename a file another process holds open); the client now attaches the file handler to the root logger, so every module's logs reach it. (2) Reproduced against the real daemon app with a shortened timeout: the client raised "isn't responding", a retry got `ingest_in_progress`, and the daemon finished the run anyway. Fixed: POST /ingest/* now starts the run on its own thread and replies 202; GET /ingest/status carries the finished run's `outcome`; `run_ingest_filesystem`/`run_ingest_drive` poll it and return exactly what the POST used to, so callers are unchanged and no fixed timeout remains. `_WRITE_LOCK` is now a `threading.Lock`, held by the run's thread.

*Found in real-app testing, then fixed:* (a) a new client reused an **old** daemon - one kept alive by Claude Desktop's MCP connection, from the installed app - whose ingest route still ran the whole sync inside the request, so the start call timed out at 120 s. `/health` now reports `api_version` (`jarvis_server/daemon/protocol.py`), and the client replaces any daemon reporting an older or missing version, alongside the existing build-fingerprint check (which only works between two packaged builds). (b) Status polls during a run went through `call()`, whose auto-start treats a slow `/health` as a dead daemon - during heavy organize work that could have spawned a second daemon mid-sync. The ingest wrapper now polls the running daemon directly, keeps waiting through slow answers, and gives up only after 5 minutes of silence or a refused connection.

3. **Auto error reports.** PostHog error tracking supersedes `error_reporting.py` → `/errors/report` → GitHub issue: it groups, counts and alerts. User-initiated **feedback → GitHub stays as is.** Retire the auto-report path once PostHog is live, with no back-compat shim.

---

## 12. Plan to the 11 Oct demo

| Day | Work | Demo-critical |
|---|---|---|
| Tue 6 | This doc reviewed. PostHog project created (§13). | yes |
| Wed 7 | Telemetry module, privacy filter and its test. Sync trace with phase spans. Packaging check. | yes |
| Thu 8 | Error classification and codes. Daemon error envelope. Exceptions to PostHog with fingerprint. Usage events. | yes |
| Fri 9 | Migration `0020` (run error code, local per-file errors). UI banners with direct actions. Opt-out toggle and notice. | partly |
| Sat 10 | PostHog dashboard, top-10 digest, alerts. 30-minute timeout reproduction. Log path fix. | yes |
| Sun 11 | **Demo:** a tester syncs and the trace shows per-phase timing. Force an expired Drive login: the user sees "Reconnect Drive" locally, and the team sees the issue in PostHog under that user. The alert fires and the top-10 digest lists it. | |

Work after the demo:
- The ingest-timeout fix if it's confirmed.
- Retiring auto error reports.
- The WhatsApp bridge, if wanted.
- Grafana for the server once the roadmap needs it.

---

## 13. Open questions

Resolved: PostHog project created (free tier, **EU**); daily email digest; new-error alerts to GitHub issues in `jarvis-errors`; the error UI built with this work (#200).

Still open:
1. **Build secret:** `POSTHOG_PROJECT_KEY` must be set on the repository; without it, builds ship with telemetry off.
2. **Packaged build check:** confirm telemetry works inside the installed `.exe` / `.app`.
3. **Reopened and spiking alerts:** need a chat tool (Slack or Teams) chosen by the team.
4. **Ownership:** move `jarvis-errors` to team ownership; invite the team to PostHog.
5. **Opt-out default:** on for tester builds, with no first-launch notice (the Settings toggle carries it). Confirm for public builds.
6. **Follow-ups:** `signed_in`, `source_added`, `feedback_sent` events and linking an install to its signed-in user; telling the app's own searches apart from Claude's tool calls; retrying only failed files; retiring the old automatic error reports.
