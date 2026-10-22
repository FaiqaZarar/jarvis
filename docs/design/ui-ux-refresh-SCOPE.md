# Jarvis UI/UX refresh: scope

**Status:** scope only. No mockups, no code. A separate design effort starts from this document, then implementation.

**Background:** this scope was settled through a set of design decisions made while planning the redesign; this document gathers their outcomes. The resulting design system is [DESIGN.md](../../DESIGN.md).

Vocabulary follows `CONTEXT.md` (Source, Sync root, Sources page, Setup health).

---

## 1. Goal

Now that the MVP is out, make Jarvis's desktop app:

1. **Understandable** - a new user can tell what to do next without help.
2. **Recoverable** - a failed or interrupted sync reads as "something to retry", never "the system broke".
3. **Cohesive** - one visual language, identical in structure on Windows and macOS.

## 2. Problems this scope addresses

| Problem | Where it shows today |
|---|---|
| First run is two blocking dialogs; connecting Claude Desktop is buried in Settings; nothing checks that the connection still works | `main_window.py` first-run flow, `claude_desktop_setup.py` |
| The page that matters most (Connectors) is third in the nav, with two equal buttons and no clear next step | `nav_rail.py`, `views/connectors_view.py` |
| Failures show raw exception text, lead with the problem, and often can't be acted on | `connectors_view.py`, `main_window.py`, backend messages |
| Progress and failures are visible only on the Connectors page | `connectors_view.py` |
| The update flow ends in modals, "Later" forgets the update, failed installs are silent | `main_window.py`, `update_banner.py`, `update_installer.py` |
| Some colour pairs fail contrast, keyboard focus is mostly invisible, copy is inconsistent | `theme.py`, views |

## 3. In scope

### 3.1 Sources page as home

The Sources page (today "Connectors") becomes the default page, with one primary action per state. The nav stays on the left; there is no bottom bar. It is a **sidebar with icons and text labels, expanded by default** (the audience will include non-technical users), and the user can collapse it to icon-only, which is remembered. Collapsed items keep tooltips and accessible names. The sync indicator and the needs-attention mark appear on the Sources item, as a short tag when expanded and a dot when collapsed.

Five states, and nothing else:

| State | Primary action |
|---|---|
| Empty (no Sources) | "Add your first source" |
| Idle (Sources, all synced) | "Add a source" |
| Syncing | Progress is the focus; "Cancel" is a quiet secondary control |
| Needs attention (failed, cancelled, interrupted, file errors) | "Retry" on the affected Source |
| Just finished | "Search your files" (the one promoted route to Search) |

- **Adding a Source:** one "Add source" action opening a chooser (Folder / Google Drive, room for later connectors). Drive sign-in stays inside that flow. The user can exit at any step (chooser, folder pick, Drive sign-in, Drive folder pick); closing returns to the Sources page in its previous state and adds nothing.
- **Sources list:** the page body. A Source appears from the moment it is added, whatever its sync result, with status, last-synced time and a Sync now/Retry action.
- **Sync indicator on every page:** shows running syncs. A failure while on another page leaves a "needs attention" mark on the Sources nav item, with a way back.

### 3.2 Setup health checklist

Replaces the first-run modal chain and the buried Claude Desktop option.

| Item | Kind |
|---|---|
| Signed in | Blocking. Stays a hard gate (existing explicit rule), shown as the first step |
| Add your first Source | Blocking for a useful app, but non-modal |
| Connect Claude Desktop | Advisory but prominent |

"Blocking" means the Sources page leads with the item, not that the rest of the app is locked.

- **Lifecycle:** the main content of the Sources empty state on a new install; a compact strip above the list once partly done; gone when complete; returns as a warning strip if a done item regresses (overwritten Claude config, stale path). A regression also feeds the Sources nav "needs attention" mark, so the nav carries one signal.
- **Dismissal:** only Claude Desktop ("I don't use Claude Desktop"), remembered across launches, reversible from Settings.
- **Exits:** sign-in stays a hard gate, but every blocking step shows a visible "Quit Jarvis". The first Source step also offers "Not now", which opens the app with that item still open on the checklist.
- **Claude Desktop states:** connected (the `jarvis` entry exists and its path works), not connected -> Connect, needs repair (stale or different path) -> Repair, Claude Desktop not installed, can't connect from here (e.g. macOS translocated run) -> "Move Jarvis to Applications and reopen". The existing backup-and-confirm step before writing the config stays. After connecting: "Restart Claude Desktop to finish" - not a tracked step.
- **"Added a Source"** is done as soon as any Source exists, whatever its sync result.
- **API key** is not a checklist step: the shared embedding key is bundled into packaged builds (test phase, PR #169), so `has_embedding_api_key()` is already true on first launch. The key only surfaces as a recovery case (3.3) or a personal-key entry in Settings, with the state "Saved, restart service to apply". When bring-your-own-key returns, revisit whether it becomes a checklist step again.

### 3.3 Error and recovery

Categories with their own treatment:

| Category | What the user does |
|---|---|
| Bad or missing API key (the bundled key was revoked, rate-limited or exhausted, or a personal key is wrong) | One guided action: save key, restart service, retry |
| Google Drive login expired | One guided action: reconnect Drive, then retry |
| Some files failed | Retry picks up only the failed files |
| Cancelled or interrupted | Retry; saved work is kept |
| Background service not running | Restart service, then retry |
| Anything else | Retry, plus one-click "Send feedback" with the failure details attached |

- **Status language:** lead with what is safe, then what is wrong, then exactly one action. Example: "Paused. 340 of 500 files are saved. Google Drive needs you to sign in again. [Reconnect Drive]". "Error" is kept out of headlines.
- **Action words:** Sync now (routine), Retry (after a problem, may say how many saved files it will skip), Cancel (while running), Restart service (only when down). No "Resume" or "Restart ingest" - the backend does not offer those.
- **Closed mid-sync:** the next launch shows the real outcome of that run once the backend allows it; until then, "Interrupted last time. Retry?"
- **Not promised:** a "Rebuild this Source" action. The backend has no operation for it.
- **Cancel:** does not take effect during Reading-files. Cancel is always visible. After clicking, the page shows "Cancelling, will stop after this step", the user can leave the page, saved work is kept, and the Source lands in Needs attention with Retry.

### 3.4 Update lifecycle

Eight user-visible states: available, downloading (real progress and size), ready to install, installing and restarting, download failed, install failed, up to date (after a manual check), update didn't finish (detected on the next launch).

- **Where:** the top banner for available, downloading, ready and failed. The Settings Updates group mirrors the current state, with version and last-checked time, and holds the manual check. The "Restart to update" modal becomes a banner action, "Restart now" / "Later".
- **"Later":** the banner goes away and a quiet "Update available" chip stays; the banner returns on the next launch, no more often.
- **Controls:** Update starts the download and the user keeps working; nothing is forced. Restart now warns if a sync is running ("A sync is running and will stop. You can retry it after the update.") and still allows it.
- **Failures** (same tone rule as 3.3, and they say the current version keeps working): download failed -> Retry with a plain reason; install failed -> Retry plus "Download manually" link; update didn't finish -> same banner with Retry.
- **Platform handling:** a short final state before quitting ("Installing the update. Jarvis will reopen in a moment") because the app cannot show progress once it quits; on macOS under translocation, "Move Jarvis to Applications and reopen" instead of attempting an install.
- **Auto-check:** the app checks once at startup (`main_window.py:194`); there is no timer. Whether to add periodic checks is not decided here.

### 3.5 Cross-cutting standards

- **Theme:** light, direction "Cobalt" (amended after the visual-direction review; the earlier decision was dark-only). The app has one theme and does not follow the OS dark mode. Every colour comes from one palette with no stray literals, so a dark theme stays a later, cheap addition.
- **Accessibility baseline:** 4.5:1 text contrast, 3:1 controls and outlines, visible focus on everything, full keyboard operation, accessible names on icon-only buttons (the collapsed sidebar), checked on both OSes. Deferred: screen-reader audit, user text-size setting. Failing pairs today: white on accent 3.23 (2.49 on hover), error colour 3.7-4.2, disabled text 2.3-2.6.
- **Copy:** short glossary ("background service", "Sync now"), no raw exception text, modals only for destructive confirmations, sentence case, applied to every string the refresh touches. Not an app-wide rewrite.
- **Search and Topics:** a small "states and labelling" pass (empty, loading, titles). No filters, no open-document action, no ranking. Search also points to the Sources page from an empty result.
- **Windows icon:** generate one 16-256 px `.ico` from `icon.svg` and wire it into the spec, installer and shortcuts.
- **Token-only surfaces:** Settings, Stats & Activity, feedback and sign-in dialogs take the new tokens and nothing else.

### 3.6 Windows and macOS

One shared design. Platform deltas the design must account for (from `research/platform-deltas`):

- Only Windows forces a dark title bar (DWM call in `theme.py`, `enable_dark_title_bar`); both use native chrome. With a light theme that forced dark bar has to go, and a Mac or Windows machine running in OS dark mode may show a dark title bar over light content (inferred, unverified). How the app behaves under OS dark mode is open (see DESIGN.md map).
- One font stack for both OSes, fixed px sizes, no high-DPI handling, icons drawn at 1x.
- macOS has `icon.icns`; Windows has no `.ico` and no installer icon.
- Update install differs (macOS bash + `hdiutil`, Windows PowerShell + Inno Setup) and neither shows install-phase progress.
- Credential storage (Keychain vs Credential Manager) and Claude config paths differ; the macOS path is documented-only.
- App Translocation exists only on macOS; Windows has the analogous moved-install case, undetected.
- Both builds are unsigned and not notarized.

Nothing counts as done until checked on both.

## 4. Out of scope

- Structural redesign of Settings, Stats & Activity, the feedback dialog and the sign-in dialog.
- New backend capabilities beyond the dependencies in section 5.
- New features: new connectors, search ranking changes.
- Bottom tab bar navigation.
- A dark theme or following the OS dark mode, a screen-reader audit, a user text-size setting.
- A distinct Windows icon design.
- Usage instrumentation, a CI test job.
- Per-file error list, "Rebuild this Source", expired-sign-in detection, true checkpoint resume.
- Gatekeeper and SmartScreen warnings (a release-process risk, recorded not designed around).

## 5. Dependencies

Work the UI above relies on that is not UI work. Sizes are from `prep/10-phasing.md` (next migration number is 0020; real db path is `jarvis/server/jarvis_server/internal/db`).

| # | Dependency | Needed by | Size |
|---|---|---|---|
| D1 | Record a Source before its first sync completes: additive columns on `sync_roots`, row written when the sync starts; `last_synced_at` keeps meaning "last clean sync" | Sources list, checklist "first Source" | M |
| D2 | Typed daemon route exposing run history and per-Source status; `/ingest/status` also carries run id and root | Sources list, sync indicator, lost-result case | M |
| D3 | Store failure reason, error code and failed-file count | One-click feedback, lost-result case | M |
| D4 | Distinct error types beyond one `internal_error` code | Recovery categories | M |
| D5 | Mark failed/cancelled phases as failed | Run history accuracy | S |
| D6 | Fix the `.env` 401 text (and the Drive terminal-command text) for packaged users; ships with D4 | Bad key, expired Drive login | S |
| D7 | Detect the `jarvis` entry and a stale path in Claude's config (desktop-side) | Claude Desktop item | M |
| D8 | `/health` reports whether the running service holds an API key | Settings personal key and recovery: "Saved, restart service to apply" | S |
| E1 | Detect an update that did not finish, on next launch (desktop-side) | Update lifecycle | M |
| E3 | Guided "save key, restart service, retry" action, delivered with D8 and reused for recovery (desktop-side) | Recovery and Settings | S-M |
| R1 | Release process: a tag can briefly lack a Windows installer asset, and the checker then reports "no update" | Update lifecycle | S |

Named, not scheduled: cancel during Reading-files (S-M, `pipeline.py` and both connector loops); per-file error list (L); wipe-and-resync a Source (L); expired-sign-in detection; usage instrumentation; CI test job.

## 6. Phasing

Thin vertical slices, each grouping its dependencies with the UI that needs them. Windows and macOS verification before the next slice starts.

1. **Updates** - update lifecycle (3.4), E1, R1, translocation guard. Almost no backend work; the riskiest unknowns (translocation, unsigned install) need real-OS testing early.
2. **Setup health** - checklist (3.2), D7, the standards that touch the checklist.
3. **Sources and recovery** - Sources page and recovery (3.1, 3.3), D1-D6, D8, E3, D2's sync indicator.

Fallbacks (string-matching error text, a GUI-side "connected" flag, deriving "first Source" from the synced list) are acceptable only inside an unreleased slice, never as a release state.

## 7. Success criteria

"Worked" = a scripted pass by a person on a clean machine plus automated tests of the state logic. Rates (time to first Source, recovery rate, update completion) are unmeasured today and need instrumentation as a separate dependency.

**Release gates:**

1. A new user adds a first Source unaided.
2. A failed sync is recovered without leaving the Sources page.
3. A real end-to-end update completes on each OS.
4. The same design is verified on both OSes.

Supporting checks sit under these: checklist and state mappings, dismissal persistence, banner states, recovery wording per category, accessibility baseline, copy rules.

- **Testers:** one or two outsiders per OS, no coaching, no time limit.
- **Clean machine:** fresh VM or reset profile, installed from the real released artifact, with Claude Desktop installed in one run and absent in another.
- **Windows:** manual script on a clean Windows and macOS install per gate and per slice; automated tests run once by hand on Windows before sign-off.
- **By eye:** tone and visual parity against the written rules, product owner signing off on both OSes side by side.
- **Regression:** the token-only surfaces pass their existing tests (`test_desktop_app_settings.py`, `_feedback.py`, `_sign_in.py`, `_views.py`) and keep their shape.
- **Blocked criteria** stay, tagged with their blocker; the UI is done only when the dependency lands or the criterion is explicitly waived.
- Recorded but not failing: Gatekeeper/SmartScreen warnings, and the Claude-use log as a manual check in the first-run script.

## 8. Open items

- Whether to add periodic update checks (today: once at startup).
- Unverified (code read only, nothing run on either OS): real appearance on either OS, macOS font resolution, DPI behaviour, updater under translocation, SmartScreen, real keyring failure behaviour, how Claude Desktop reacts to a stale command path, behaviour for a nonexistent folder.
- Known bug to fix regardless of this scope: Search rows pass pixel constants to `setPointSize` (`search_view.py:117,135,145`, `feedback_dialog.py:243`), so rows render larger than intended and may clip.
- Likely bug to check on both OSes: `DaemonStatusBanner` and `UpdateBanner` are plain `QWidget` subclasses with no `paintEvent` or `WA_StyledBackground`, so their themed background, border and radius may never paint (`theme.py:291-305`). Also, `theme.py` has no `QPushButton:focus` rule, and Qt removes the native macOS focus halo for QSS-styled buttons, so keyboard focus is likely invisible on buttons. Nav icons are rasterised at 1x only. Source: `research/qss-capabilities` (cea9b5e).
- Documentation drift: `CLAUDE.md` and the coding guide give `jarvis/internal/db` and `jarvis/migrations`; the real paths are under `jarvis/server/jarvis_server/`. `docs/desktop-app-ux-redesign-DESIGN.md` says the app has no styling or theme, which is no longer true.
- Stale user-facing string: "only available on Windows right now" at `claude_desktop_setup.py:127`.
