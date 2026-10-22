# Desktop app UI/UX redesign (Search, Hierarchy, Connectors): design

**Status:** design only, per the project lead's direction. Nothing in this
doc is implemented yet.

**Source:** the project lead's direct instructions (this doc), plus
`jarvis/modules/desktop_app/main_window.py`, `views/search_view.py`,
`views/hierarchy_view.py`, `views/connectors_view.py`, `daemon_worker.py`,
`error_presentation.py`, `app.py`, `jarvis/modules/daemon/client.py`,
`jarvis_server/daemon/server.py`, and `jarvis_server/pipeline.py` as they
exist today (read in full for this doc, not recalled from memory), plus
`docs/desktop-app-DESIGN.md` and `docs/organize-checkpointing-DESIGN.md`
for existing conventions this doc should follow.

**Goal, stated by the project lead:** "a modern app feel - animations, a
cleaner overall visual redesign," applied consistently across Search,
Hierarchy, and Connectors, not three separately-patched screens. Real
interactivity, so the app always visibly communicates "something is
happening," not silence. The concrete trigger: clicking "Add a folder…"
and picking one gives zero visible feedback while a real, possibly
multi-minute ingest runs - a user cannot tell "it's working" from
"nothing happened." That has to be fixed as part of this redesign, not
carved off separately, since it's the sharpest instance of the same
underlying problem (no interactivity/feedback language exists anywhere
in this app yet).

---

## 1. Verified current state (read the real code, not memory)

### 1a. There is no visual design today - zero, not "minimal"

Grepped across `jarvis/modules/desktop_app/` for `setStyleSheet`, `QSS`,
`QIcon`, `setWindowIcon`, and any animation class
(`QPropertyAnimation`, etc.): **one** hit total -
`error_presentation.py`'s `DaemonStatusBanner._label.setStyleSheet("color:
#7a1f1f;")`. That is the entire custom styling surface of the app. No
`QIcon`/`setWindowIcon` call exists anywhere, and no image asset for this
app exists anywhere in the repo outside `.venv`'s bundled third-party
packages (confirmed by a repo-wide glob for `*.ico`/`*.png`/`*.svg`/`*.icns`).
The Mac PyInstaller spec confirms this independently: `icon=None`, with
its own comment noting "No project icon exists yet."

Every widget in every view is a plain, undecorated Qt widget at its
platform default: `QTabWidget`, `QLineEdit`, `QPushButton`, `QListWidget`,
`QTreeWidget`, `QTextEdit`, `QSplitter`. No custom fonts, no spacing
system, no color palette, no hover/pressed states beyond whatever the
OS's native Qt style already draws for free. This isn't a redesign of an
existing theme - there is no existing theme to redesign.

### 1b. `MainWindow` (main_window.py)

A `QMainWindow` with a `QVBoxLayout` central widget: `DaemonStatusBanner`
on top, a `QTabWidget` below holding `SearchView` / `HierarchyView` /
`ConnectorsView` as its three tabs (lines 82-100). Tabs start disabled
and only enable once `ensure_daemon_running()` succeeds
(`_on_daemon_ready`, line 134). `ConnectorsView.ingest_succeeded` is
connected to `HierarchyView.load_top_level` (line 88) so a successful
ingest refreshes the hierarchy tab automatically - the one piece of
"something happened" feedback that already exists anywhere in this app,
and it's in a tab the user may not even be looking at during the ingest.

### 1c. `SearchView` (views/search_view.py)

A `QLineEdit` + "Search" `QPushButton` row, a `QListWidget` of
plain-text-formatted hits (`format_hit()`, line 29), and a "Load more"
button gated on `next_cursor`. `_on_search` clears the list and disables
"Load more" before firing the daemon call (lines 68-76) - the only
existing "clear old state before a new async call starts" pattern in the
codebase, though it has no busy/loading indicator of its own either.

### 1d. `HierarchyView` (views/hierarchy_view.py)

A `QSplitter` of a `QTreeWidget` (topics, lazily expanded via
`itemExpanded` → `browse()`) and a `QListWidget` (chunks in the clicked
topic), plus a manual "Refresh" button. Expandable-but-unloaded nodes get
a literal `QTreeWidgetItem(["Loading…"])` placeholder child (line 109) -
text only, no spinner, no styling; it's the closest thing to a loading
affordance anywhere in this app today, and it's easy to miss.

### 1e. `ConnectorsView` (views/connectors_view.py) - the confirmed gap

```python
def _on_add_folder(self) -> None:
    path = QFileDialog.getExistingDirectory(self, "Select a folder to ingest")
    if not path:
        return
    run_daemon_call(
        self._pool,
        daemon_client.run_ingest_filesystem,
        path,
        on_success=self._on_ingest_result,
        on_error=self._on_ingest_error,
    )
```

Confirmed by reading this in full: between the folder picker closing and
`_on_ingest_result`/`_on_ingest_error` firing, **nothing on screen
changes at all.** `add_folder_button` stays enabled and clickable, the
`QTextEdit` summary keeps showing whatever the last ingest (or nothing)
left there, there is no busy cursor, no disabled state, no label. A user
who clicks it twice fires two overlapping ingest requests at the daemon
(serialized behind `_WRITE_LOCK` server-side - see §4 - but the second
click still looks, from the UI, like the first click did nothing). This
is the exact gap the project lead flagged, confirmed directly rather than
assumed.

### 1f. The threading seam this redesign hooks into (daemon_worker.py)

Every daemon call already goes through `run_daemon_call(pool, fn, ...,
on_success=..., on_error=...)`, which runs `fn` on a `QThreadPool` worker
and delivers the result back via a Qt signal (§ design doc reasoning
already documented in that module). This is the one place in the entire
app where "a daemon call started" and "a daemon call finished" are both
already knowable moments - any busy/spinner/disable-button state added by
this redesign should hook `run_daemon_call`'s call site (set busy state
right before calling it, clear it in `on_success`/`on_error`), not
duplicate that bookkeeping per view.

---

## 2. What's realistic in PySide6, honestly

PySide6/Qt genuinely supports real animation and real theming - this
isn't a "fake it with static screenshots" situation - but the *kind* of
animation matters for effort. Grouped by real cost:

**Cheap, real, recommend doing (quick wins, §6):**
- **QSS theming at the `QApplication` level** (`app.setStyleSheet(...)`
  once, in `app.py`'s `main()`): colors, fonts, borders, spacing,
  `:hover`/`:pressed`/`:focus`/`:disabled` pseudo-states on buttons, tabs,
  line edits. This is the single highest-leverage change for "looks
  modern" - QSS is CSS-like and every widget in this app already exists
  and would just start rendering the shared theme instead of the OS
  default, no widget-tree restructuring required.
- **`QPropertyAnimation` on `windowOpacity`/a `QGraphicsOpacityEffect`'s
  `opacity` property** for fade-ins (the error banner appearing, a
  results list populating, a tab's content appearing) - a handful of
  lines per animation, no custom painting.
- **Indeterminate `QProgressBar`** (`setRange(0, 0)`) - Qt animates this
  natively via the active style; combined with QSS it already looks
  reasonably modern with zero custom animation code. This is the
  pragmatic choice for ingest progress (§4) given the daemon doesn't
  report fine-grained percentage progress.
- **Busy-button pattern**: disable + relabel (e.g. "Add a folder…" →
  "Ingesting…") + show a small inline spinner - a few lines wired
  through the `run_daemon_call` seam (§1f), reusable across every view
  that fires a daemon call, not just Connectors.

**Real, but genuinely more work - flag effort honestly (medium, §6):**
- **A custom-painted spinner** (subclassed `QWidget`, `paintEvent`
  drawing a rotating arc, driven by a `QTimer` or `QPropertyAnimation` on
  a rotation property) - looks more distinctive/on-brand than the native
  `QProgressBar`, but is a new widget class with its own paint code, not
  a QSS tweak. Worth it only if the native indeterminate bar reads as too
  "plain Windows/Mac default" once the rest of the theme is in place -
  recommend shipping the native version first and revisiting only if it
  looks out of place.
- **Animated tab transitions.** Qt's `QTabWidget` has no built-in
  crossfade/slide between tabs. A real animated transition means
  replacing (or wrapping) it with a `QStackedWidget` and manually
  animating opacity/geometry on tab change - doable, but it's new
  plumbing around every tab switch, not a style property.
- **Richer list/tree rows** (a topic "pill," bolded title, secondary-color
  preview text per search hit) via a custom `QStyledItemDelegate` -
  real, valuable, and squarely within Qt's supported extension points,
  but it's a delegate class per view with its own paint logic, not a CSS
  rule.
- **Toast-style transient notifications** ("Ingest complete") - a small
  custom `QWidget` with a slide/fade `QPropertyAnimation` and a
  self-dismiss timer. Contained, but new, not a variant of anything that
  exists today.

**Not a reasonable scope for this pass - flag as out of scope:**
- A full custom-drawn design system: translucent/blurred panels, custom
  scrollbars, shader-driven effects. Achievable in Qt in principle, but
  disproportionate effort for an internal tool's redesign.
- Rewriting the UI in Qt Quick/QML for "true" fluid animation. That's a
  second UI stack and a rewrite, not a redesign of the existing
  `QWidget`-based views - explicitly not recommended.
- Fully custom card-based rendering for every list/tree item everywhere
  (as opposed to a delegate that keeps `QTreeWidget`/`QListWidget` and
  improves how each row paints) - bigger than this pass needs, and
  `hierarchy_view.py`'s own docstring already made the same call once
  before (deliberately kept `QTreeWidget` over a custom item model for
  the same reason: "more machinery than a first pass needs").

---

## 3. A cohesive visual language, applied once

The concrete failure mode to avoid: each view growing its own
`setStyleSheet()` calls with its own one-off colors (exactly what
`error_presentation.py` does today, harmlessly, because it's the only
styled thing in the app - it would stop being harmless the moment a
second and third view start doing the same independently).

Proposed shape, matching this codebase's existing "one source of truth"
convention (`get_connection()` for DB access, one `daemon_worker.py` for
threading):

- **One module, e.g. `jarvis/modules/desktop_app/theme.py`**, defining a
  small set of named constants (background/surface colors, one accent
  color, primary/secondary text colors, border color, and the
  error/success/warning colors `DaemonStatusBanner` and the ingest-result
  card would both need) and a `build_stylesheet() -> str` function that
  formats them into one QSS string.
- **Applied once**, in `app.py`'s `main()`, via
  `app.setStyleSheet(theme.build_stylesheet())` - every widget in every
  view inherits it by default. Per-widget `setStyleSheet()` calls should
  be reserved for genuinely instance-specific state (e.g., a banner that
  is red *specifically because it's showing an error right now*, not
  "red because that's this widget's color").
- **Typography:** use the platform's default UI font (Segoe UI / San
  Francisco) rather than bundling a custom one - both already read as
  "modern" on their respective OS, and bundling a font is real added
  packaging surface for a benefit that's genuinely marginal here. Define
  2-3 point-size steps (heading / body / secondary-caption) as constants
  in the same theme module, reused everywhere instead of ad hoc font
  sizes.
- **Spacing:** a small set of margin/padding constants (the only
  precedent today is `DaemonStatusBanner`'s hardcoded `(8, 4, 8, 4)`)
  reused across all three views' layouts instead of each view picking its
  own numbers.
- **Iconography:** none exists today (§1a). Needs a small bundled icon
  set - a folder icon for "Add a folder…," a refresh icon for Hierarchy's
  refresh button, a search icon, tab icons - most practically sourced
  from a permissively-licensed icon set (e.g. a handful of SVGs from
  Feather/Lucide) rather than commissioned art, given this is meant to be
  sequenced as a quick win, not a branding project. The app's own
  window/taskbar icon is a separate question - see §7, since none exists
  at all right now (confirmed: `icon=None` in the packaging spec).

---

## 4. The ingest feedback fix

### 4a. What the daemon actually returns today - checked, not assumed

`POST /ingest/filesystem` and `POST /ingest/drive`
(`jarvis_server/daemon/server.py::api_ingest_filesystem`/`_drive`) each
run the *entire* pipeline - `connector.sync()` → chunk → embed → organize
→ reconcile (`jarvis_server/pipeline.py::run_ingest`) - inside one
`await asyncio.to_thread(...)` call, serialized behind one
`_WRITE_LOCK`, and return exactly one JSON body once that whole call
returns. **There is no intermediate status anywhere** - no SSE, no
WebSocket, no polling endpoint, nothing published mid-run that any client
could read. The client side already knows ingest can take a long time:
`jarvis/modules/daemon/client.py::run_ingest_filesystem` uses a 30-minute
timeout with retry-on-timeout disabled specifically because "a real
ingest can genuinely run for minutes" (confirmed measured: ~260s for a
261-chunk/58-cluster corpus, per that module's own comment) - the backend
already treats this as a long-running operation, the UI just doesn't
reflect that anywhere.

The only persisted state that looks superficially like "progress" is
`organize_runs.status` (`in_progress`/`completed`,
`jarvis_server/internal/db/organize_runs.py`) - but it's written only
around the organize/reconcile sub-stage specifically (not sync/chunk/embed),
and it exists for crash-resume across process restarts
(`docs/organize-checkpointing-DESIGN.md`), not as a UI-facing progress
signal. It's the wrong tool for this: too coarse (one of two states) and
scoped to the wrong stage.

### 4b. Why polling is realistic without a bigger rearchitecture

`api_ingest_filesystem`/`_drive` `await asyncio.to_thread(...)` rather
than blocking the event loop directly - confirmed by reading the route
handlers. That means the daemon's event loop stays free to serve *other*
requests concurrently while an ingest thread runs. A second, cheap `GET`
route added alongside the existing ones can be polled while the original
long-running `POST` is still in flight, with no change to the
request/response shape of the ingest routes themselves.

### 4c. Recommended design - simplest viable, not a streaming system

1. **A module-level "current phase" holder in the daemon**
   (`jarvis_server/daemon/server.py`), e.g. a plain dict
   `_ingest_status = {"phase": None, "connector": None, "started_at": None}`.
   A single global is sufficient and not a race risk: `_WRITE_LOCK`
   already guarantees at most one ingest is ever in flight per daemon
   process (the same fact `organize-checkpointing-DESIGN.md` §1a already
   relies on).
2. **Phase markers written at the stage boundaries `pipeline.run_ingest`
   already has** - before `connector.sync()` ("reading files" /
   "syncing from Drive"), after sync/before chunking ("preparing
   documents"), before `embed_and_index_chunks` ("generating
   embeddings"), before `_run_organize` ("organizing into topics"),
   before `_run_reconcile` ("finalizing"). These are stages that already
   exist as distinct function calls in that module today - this is
   instrumentation of existing seams, not new pipeline structure.
3. **One new route, `GET /ingest/status`**, returning that dict
   (phase + elapsed seconds since `started_at`). Trivially cheap to
   serve - no DB access, no lock contention with the read routes.
4. **Client side:** while a `run_ingest_filesystem`/`_drive` call is in
   flight, `ConnectorsView` starts a `QTimer` polling `GET /ingest/status`
   (e.g. every 500ms-1s) and updates a status label with the returned
   phase text; the timer stops the moment the main POST call resolves
   (success or error), matching exactly the existing
   `on_success`/`on_error` seam in `daemon_worker.py` (§1f).
5. **Visible UI state for the duration:** an indeterminate `QProgressBar`
   (§2), the live phase label, and "Add a folder…" / "Connect Google
   Drive…" both disabled so a second click can't fire a concurrent
   request the daemon would just serialize anyway, but which would
   otherwise look to the user like their first click did nothing (the
   original bug, in a new shape).

This is deliberately **not** a websocket/SSE system: it's one new `GET`
route reading a plain in-process variable, plus a client-side poll timer
reusing infrastructure (`run_daemon_call`, Qt signals) that already
exists. A full job-queue/streaming redesign would be disproportionate to
what a five-stage, single-daemon-process pipeline actually needs.

### 4d. Fallback sequencing if the daemon change doesn't fit this pass

If even the small phase-marker addition to `pipeline.py`/`server.py` is
judged out of scope for an initial pass, ship the **client-only** half
first: indeterminate spinner, "Add a folder…" disabled for the duration,
a generic status line ("Ingesting… this can take a few minutes for a
large folder") plus a live elapsed-time counter - all achievable from
`ConnectorsView` alone, zero daemon changes, and already a large
improvement over "zero feedback." Live phase text becomes a fast-follow
once the small backend hook lands. See §6 for how this fits the overall
sequencing.

---

## 5. View-by-view redesign proposal

**Shell (`MainWindow`):** themed tab bar (QSS: accent-colored active-tab
underline, hover state on inactive tabs) instead of the OS default.
`DaemonStatusBanner.show_error`/`hide` get a fade/slide
`QPropertyAnimation` instead of an instant `show()`/`hide()` - one
widget, contained change.

**Search:** the query row restyled as a single rounded "search pill"
(QSS on `QLineEdit`, focus-ring color change on `:focus`). Results stay a
`QListWidget` but gain a `QStyledItemDelegate` (§2, medium effort) so
each hit renders as a small topic tag + bolded document title + secondary-
color preview, instead of `format_hit()`'s current single plain-text
block. An empty state (icon + "Search your notes, files, and Drive
content") replaces the currently-blank list before a first search.

**Hierarchy:** the existing text-only `"Loading…"` placeholder child
(§1d) gets a themed, distinguishable style (italic, secondary color, or a
tiny inline spinner icon) so it visibly reads as "in progress," not as an
inert tree item. The `QSplitter` handle gets a visible, grabbable style
(currently the invisible platform default). "Refresh" adopts the same
busy-button pattern as ingest (§4c item 5) while `load_top_level()`'s
call is in flight - trivial once that pattern exists, reused rather than
reinvented per view.

**Connectors:** the ingest-feedback fix (§4) lives here. Additionally,
replace the plain `QTextEdit` result dump with a small structured summary
card (icon + key numbers laid out, not a monospace text blob) - a real
layout change, moderate effort, not a QSS tweak. "Connect Google Drive…"
stays disabled (still out of scope per that view's own existing
docstring) but should look *deliberately* disabled in the new theme's
language, not default Qt gray.

---

## 6. Risk/effort sequencing - do not build this all in one pass

**Quick wins (do first - most of the "feels modern" ask, least risk):**
- App-wide QSS theme pass (§3) across all three views + `MainWindow` -
  colors, fonts, spacing, hover/focus/pressed/disabled states.
- Banner fade animation.
- Busy-button pattern (disable + relabel + indeterminate spinner) wired
  through `run_daemon_call`, applied to ingest first, then Hierarchy's
  Refresh.
- Client-only half of the ingest fix (§4d): spinner + generic status text
  + elapsed time, zero daemon changes.

**Medium (real, scoped, but each is genuinely new code, not styling):**
- The daemon phase-status polling endpoint + live phase text (§4c) - the
  one item other parts of this redesign don't block on, so it can land
  in parallel with the quick-wins pass.
- Search result item delegate.
- Structured ingest-result summary card (replacing the `QTextEdit` dump).
- Hierarchy's "Loading…" placeholder restyle/spinner.

**Larger / riskier (sequence last, reassess after the above lands):**
- Animated tab transitions (`QStackedWidget` + manual animation) -
  meaningful new plumbing around every tab switch.
- A custom-painted spinner widget, if the native indeterminate
  `QProgressBar` ends up looking out of place next to the new theme.
- Dark mode (see §7 - not yet a decided requirement).
- Sourcing/commissioning a real icon set and app icon/branding, if
  nothing existing can be reused (see §7).

Recommend landing the quick-wins bucket as one pass across all three
views together (that's what makes it read as "one cohesive redesign"
rather than three patches), the daemon status endpoint as a small,
independent, parallel change, and treating everything in the "larger"
bucket as separate, individually-scoped follow-ups rather than blocking
the whole redesign on them.

---

## 7. Open questions

1. **Dark mode** - build one now as part of this pass, defer it, or rely
   on Qt's OS-palette detection (which doesn't fully auto-follow the OS
   theme across platforms/PySide6 versions without explicit handling,
   and wasn't checked as part of this audit)? Affects whether `theme.py`
   needs two palettes from day one or can add a second one later.
2. **Window size/position persistence** across launches - not currently
   implemented (confirmed: no `QSettings` geometry save/restore anywhere
   in `main_window.py`/`app.py`). Worth adding as part of "feels modern,"
   or explicitly out of scope for this pass?
3. **App icon / branding** - none exists today (confirmed: `icon=None` in
   the Mac PyInstaller spec with its own "no project icon exists yet"
   note, no `QIcon`/`setWindowIcon` call anywhere, no image asset in the
   repo outside `.venv`). Does the project lead have real branding to
   hand off, or does a placeholder need to be designed as part of this
   redesign?
4. **Icon set for in-app buttons/tabs** (folder, refresh, search,
   settings) - license and source a small existing set, or is custom
   iconography expected? Materially changes the effort estimate for the
   "iconography" line in §3/§6.
5. **How far to take interactivity on data views** - is the Search result
   item delegate (§5) an expected deliverable of this pass, or a
   fast-follow once the theme/animation basics land?
6. **Scope boundary for the daemon status endpoint (§4c)** - land it as
   part of this UI redesign's own PR(s), or file it as a small, separate
   daemon-side ticket that this redesign depends on but doesn't itself
   implement? Either is workable; it changes how this work gets split up
   and reviewed.
