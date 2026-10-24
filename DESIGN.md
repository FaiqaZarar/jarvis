# Jarvis design system

The visual and verbal rules for the Jarvis desktop app (Windows and macOS). Read it before designing or building any UI. It describes the system and its components, not individual screens.

**Status:** implemented in the desktop app (`jarvis/modules/desktop_app/`, tokens in `theme.py`). The scope it was written for is in `docs/design/ui-ux-refresh-SCOPE.md`. Change the design system here first, then the code.

**Audience:** people who will eventually be non-technical. That is why the navigation has words, failures are recoverable, and the copy avoids technical terms.

**Source of truth:** this file is the source of truth for tokens. `jarvis/modules/desktop_app/theme.py` is brought into line with it; token names below keep the existing constant names where they still fit.

---

## 1. Principles

1. **Calm and plain.** Short sentences, ordinary words, no jargon, no jokes, no blame.
2. **One main action.** Each state of each page has exactly one primary action, and it is visually the strongest thing on the page.
3. **Safe first.** When something goes wrong, lead with what is safe, then what is wrong, then one action. Failures read as recoverable.
4. **Always an exit.** No flow, dialog or step is a dead end. The user can always back out.
5. **Honest state.** The interface says what is true now: syncing, saved, needs attention, checking. It never promises what the backend cannot do.
6. **Flat and precise.** Structure comes from borders, spacing and weight, not shadows or effects. Qt styling cannot draw shadows, blur or transitions, and this system does not pretend otherwise.
7. **Complexity stays beneath the surface.** The user never needs to know about services, protocols or file chunks.

## 2. Foundations

### 2.1 Theme

One **light** theme, direction "Cobalt": precise, crisp, blue accent. The app does not follow the OS dark mode. Colours live in one palette with no stray literals in widgets, so a dark theme stays a later, cheap addition. How the app behaves under OS dark mode (native title bar, native widgets) is open (section 8).

### 2.2 Colour tokens

Names match `theme.py` constants where they exist. Contrast is computed (WCAG): text pairs need 4.5:1, control outlines and graphic accents need 3:1.

| Token | Hex | Role |
|---|---|---|
| `BACKGROUND` | `#F5F7FA` | Page background |
| `SURFACE` | `#FFFFFF` | Cards, rows, inputs, dialogs |
| `SURFACE_ALT` | `#EEF1F6` | Raised or hovered fill, secondary button hover, disabled fill |
| `BORDER` | `#E4E7EC` | Decorative border: cards, rows, dividers |
| `BORDER_CONTROL` | `#7F899C` | Outline of buttons and inputs (new) |
| `TEXT_PRIMARY` | `#111827` | Body and headings |
| `TEXT_SECONDARY` | `#5D687C` | Captions, secondary lines, inactive nav |
| `TEXT_DISABLED` | `#98A2B3` | Disabled text (informational, not required to meet 4.5:1) |
| `ACCENT` | `#3157D5` | Primary action, selection, focus ring, progress |
| `ACCENT_HOVER` | `#4A6BDE` | Primary hover |
| `ACCENT_PRESSED` | `#2748B8` | Primary pressed |
| `ACCENT_TEXT` | `#FFFFFF` | Text on `ACCENT` (new) |
| `ACCENT_LIGHT` | `#E7ECFF` | Selected nav item, info banner fill (new) |
| `ACCENT_SECONDARY` | `#C07434` | Orange for graphics only (the collapsed-nav attention dot, markers); never for text (new) |
| `ERROR` / `ERROR_BG` | `#C23B31` / `#FDECEA` | Failure text and border / fill |
| `SUCCESS` / `SUCCESS_BG` | `#1F7A4D` / `#E6F4EC` | Success text and border / fill |
| `WARNING` / `WARNING_BG` | `#9A5A1E` / `#FFF4E8` | Needs-attention text / fill |
| `WARNING_BORDER` | `#E8C9A6` | Needs-attention tag border (new) |
| `SUCCESS_BORDER` | `#B6DCC6` | Success tag border (new) |
| `INFO_BORDER` | `#C9D4FB` | Info banner and tag border (new) |
| `INFO_BG` | `#E7ECFF` | Same value as `ACCENT_LIGHT` |

Computed contrast for the required pairs (all pass):

| Pair | Ratio | Needs |
|---|---|---|
| `TEXT_PRIMARY` on `BACKGROUND` / `SURFACE` | 16.53 / 17.74 | 4.5 |
| `TEXT_SECONDARY` on `BACKGROUND` / `SURFACE` / `SURFACE_ALT` | 5.24 / 5.62 / 4.96 | 4.5 |
| `ACCENT_TEXT` on `ACCENT` / `ACCENT_HOVER` / `ACCENT_PRESSED` | 6.08 / 4.72 / 7.76 | 4.5 |
| `ACCENT` on `ACCENT_LIGHT` / `SURFACE` / `BACKGROUND` | 5.16 / 6.08 / 5.66 | 4.5 |
| `ERROR` on `ERROR_BG` / `SURFACE` | 4.63 / 5.30 | 4.5 |
| `SUCCESS` on `SUCCESS_BG` | 4.69 | 4.5 |
| `WARNING` on `WARNING_BG` / `SURFACE` | 5.02 / 5.45 | 4.5 |
| `BORDER_CONTROL` on `BACKGROUND` / `SURFACE` / `SURFACE_ALT` | 3.28 / 3.52 / 3.11 | 3 |
| `ACCENT` on `SURFACE` (graphic) | 6.08 | 3 |
| `ACCENT_SECONDARY` on `SURFACE` / `BACKGROUND` (graphic) | 3.63 / 3.38 | 3 |

Informational: `TEXT_DISABLED` on `SURFACE` is 2.58 (disabled controls are exempt); `BORDER` on `BACKGROUND` is 1.16 (decorative only; any control uses `BORDER_CONTROL`).

Colour is never the only signal: every status also has words or an icon.

### 2.3 Typography

- **Typeface:** native. Windows: Segoe UI. macOS: the OS system font, obtained by using the application's default font, not by naming a family (`-apple-system` does not resolve under Qt). No bundled font. Revisit if real screenshots show the platforms drifting too far apart.
- **Sizes, always in px, never points:** 12 caption (the floor), 13 body, 15 section heading, 19 page title.
- **Weights:** 400, 500, 600 only. Page titles and primary buttons 600; body 400; nav labels 500 (600 when selected).
- **Line height:** about 1.4 for body text.
- Existing constants `FONT_SIZE_SMALL`, `FONT_SIZE_BASE`, `FONT_SIZE_HEADING` map to 12, 13, 15; a page-title size is added.

### 2.4 Spacing

A 4 px grid: **4, 8, 12, 16, 24, 32**. Default gap 12. Default card padding 16. Page padding 20 to 24. (Replaces the current 4 / 8 / 16.)

### 2.5 Shape, borders, focus

- **Radius:** 8 for buttons, inputs, cards and banners; fully round for tags and progress bars; 4 only for small inner pieces such as a checkbox.
- **Borders:** 1 px. Cards and rows use `BORDER`; buttons and inputs use `BORDER_CONTROL`. There are no shadows, so borders and spacing carry all structure.
- **Keyboard focus:** a 2 px `ACCENT` ring on every focusable control (`TEXT_PRIMARY` on a primary button, whose fill is already `ACCENT`). This is required, not decorative: Qt removes the native macOS focus ring for QSS-styled buttons. On buttons the ring is drawn as the border with `outline: none`: Qt ignores QSS `outline` width and offset there and draws the platform focus box around the label, which clips it.

### 2.6 Sizes

Buttons and inputs 32 px tall. The single main call to action on a page 40 px. List rows at least 48 px. Nav items 40 px. Sidebar 208 px expanded, 62 px collapsed.

### 2.7 Icons

Keep the current filled style. 20 px in the sidebar, 16 px inline. Drawn at the screen's pixel density and recoloured per state (default, hover, selected), not rasterised once at 1x. Wider iconography and illustration rules are not specified until a feature needs them.

### 2.8 Motion

State changes are instant. The only motion is the existing 180 ms fade when a banner appears or disappears. Anything new needs a feature that asks for it.

### 2.9 Elevation

None. Surfaces are white on a pale grey page, separated by a 1 px border. (Layering of dialogs over the page is open: section 8.)

## 3. Interaction states (defined once)

Every clickable component (buttons, inputs, nav items, rows) shares these five states. Components below list only their extra states.

| State | Treatment |
|---|---|
| Default | As defined per component |
| Hover | `SURFACE_ALT` fill (primary: `ACCENT_HOVER`). Plain labels have no hover (QSS cannot) |
| Pressed | Primary: `ACCENT_PRESSED`. Others: `BORDER` fill |
| Keyboard focus | 2 px `ACCENT` ring (the border, on buttons) |
| Disabled | `SURFACE_ALT` fill, `BORDER` outline, `TEXT_DISABLED` text; no hover or pressed change |

**Busy:** an action in flight changes its label to present tense ("Retrying…") and disables itself. No spinner.

**Progress:** a bar always has words next to it ("Reading files, 310 of 500"), never a bare percentage. Indeterminate only when the total is unknown.

## 4. Components

Only the components the scope needs. New components are added when a feature needs them.

### 4.1 Sidebar navigation

- **Layout states:** expanded (default; icon plus text) and collapsed (icon only). The user can collapse it with the Collapse control at its foot, and the choice is remembered.
- **Items:** Sources, Search, Topics, Activity, Settings (Settings sits at the bottom, above Collapse). Visible label, tooltip and accessible name are the same words.
- **Item states:** default (`TEXT_SECONDARY`), hover (`SURFACE_ALT`), keyboard focus, selected (`ACCENT_LIGHT` fill, `ACCENT` text, weight 600). Collapsed items keep the same states and show a tooltip with the label on hover and on keyboard focus.
- **Sources tag:** while syncing, a quiet blue tag "Syncing"; when something needs attention, a quiet amber tag "Attention" (`WARNING_BG`, `WARNING_BORDER`, `WARNING` text, 11 px, one line, never wraps). Collapsed, the tag becomes a 10 px dot (`ACCENT` for syncing, `ACCENT_SECONDARY` for attention).
- Setup-health regressions feed the same Attention mark, so the nav carries one signal.

### 4.2 Banners

- **Kinds:** error (`ERROR_BG`, `ERROR` border), warning (`WARNING_BG`, `WARNING_BORDER`), info (`ACCENT_LIGHT`, `INFO_BORDER`), success (`SUCCESS_BG`, `SUCCESS_BORDER`).
- **Anatomy:** a bold lead, one sentence, at most one primary action, an optional quiet secondary action.
- **Behaviour:** success and info can be dismissed. Error and warning stay until resolved. At most two banners show at once, errors above everything else.
- **Update ready** is the **info** kind (blue), not amber: amber means "this needs your attention", and an update does not.

### 4.3 Buttons

- **Primary:** `ACCENT` fill, `ACCENT_TEXT`, weight 600. One per state of a page. The single main call to action is 40 px; all others 32 px.
- **Secondary:** `SURFACE` fill, `BORDER_CONTROL` outline, `TEXT_PRIMARY`.
- **Quiet:** no fill, no outline, `TEXT_SECONDARY` (Cancel, Later, "I don't use it").
- States per section 3.

### 4.4 Inputs

`SURFACE` fill, `BORDER_CONTROL` outline, 32 px, 8 px radius. States per section 3, with the focus ring.

### 4.5 Tags

Fully round, 12 px, weight 600. Synced (`SUCCESS`), Syncing (`ACCENT`), Needs attention (`WARNING`), New (`TEXT_SECONDARY`). Fill and border follow the banner colours. Always words.

### 4.6 Sources row

- **Anatomy:** name (weight 600), a secondary line (path, or progress), a status tag, one action. At least 48 px tall.
- **States:** not synced yet, synced, syncing (with a progress bar and words), cancelling, needs attention, busy (action disabled, label changes).
- **Order:** needs attention first, then syncing, then the rest by name, so a problem is never off-screen.
- **Actions:** Sync now (routine), Retry (after a problem), Cancel (quiet, while running). Remove (quiet, beside the main action, only while nothing is syncing) takes the source out of the library after a confirm; files on disk or in Drive are never touched.

### 4.7 Setup checklist and strip

- **Variants:** a full card (the main content of the empty Sources page) and a compact strip above the list once partly done. Both share the same item states.
- **Items:** Signed in, Add your first source, Connect Claude Desktop (optional).
- **Item states:** done, to do, needs attention (repair), dismissed (Claude Desktop only; hidden). While the service is starting and status is unknown, an item shows a muted "Checking…", never a false "to do".
- Sign-in stays a hard gate; every blocking step shows a visible "Quit Jarvis". The first-Source step also offers "Not now". The API key is not a step: without one, a first-run dialog asks for it before anything else.

### 4.8 Empty state

A centred card: a title, one sentence, one primary action. Used for no Sources, no search results and no topics yet. It always points to what to do next.

### 4.9 Progress indicator

A bar plus words (section 3). `ACCENT` fill on `SURFACE_ALT`, fully round.

### 4.10 Dialogs

- **Add source chooser:** options Folder and Google Drive; Drive sign-in happens inside it. Exit at every step.
- **Confirm dialog:** only for destructive actions. The title states the action, the body states the consequence.
- **Rules for both:** always an exit (Escape, a Cancel button, a close control). Main action on the right. A destructive action uses `ERROR` on its button only. Keyboard focus starts on the safe action. Failures never appear in a modal.

## 5. Voice and copy

### 5.1 Tone

Calm, plain, direct. No jokes, exclamation marks or blame. Address the user as "you". Name "Jarvis" in the third person when it acts ("Jarvis reads your files"); never "I". Short sentences, safe fact first.

### 5.2 Vocabulary

| Say | Never say |
|---|---|
| Source, Sources | connector |
| Sync now, Syncing, Retry | ingest, ingestion |
| background service | daemon |
| Claude connection | MCP, mcpServers |
| files | documents, chunks, embeddings |
| API key (with a one-line helper) | token, credential |

Also kept out of user copy: agent(s), memory layer, substrate, compounding, harness, infrastructure.

**Product sentence:** "Jarvis keeps your files and notes in one place, so Claude can use them." Reused on the welcome and empty states and the Claude Desktop step.

### 5.3 Mechanics

Sentence case everywhere, including buttons and titles. No trailing full stop on buttons, tags or labels; full sentences end with one. No exclamation marks. An ellipsis only on a busy label ("Retrying…"), never to mean "opens a dialog". Dialog titles state the action ("Add a source"), not "Done". Counts always name the thing ("340 of 500 files"). Times are relative up to a week ("2 minutes ago", "yesterday", "3 days ago"), then a date ("12 Oct").

### 5.4 Rules for messages

- Lead with what is safe, then what is wrong, then one action. "Error" is not a headline.
- Never show raw exception or backend message text.
- Failures use a banner or a row, never a modal.
- Anything unclassified is "Anything else": a Retry and a one-click Send feedback.

## 6. Message catalogue (drafts, for review at the design stage)

`{n}`, `{m}`, `{total}`, `{mb}`, `{name}` and `{version}` are filled in at run time.

### Failures

| Case | Message | Action |
|---|---|---|
| Bad or missing API key | **Paused.** Jarvis needs a valid API key to read your files. Everything already saved is safe. | Fix API key |
| Google Drive login expired | **Paused.** {n} of {total} files are saved. Google Drive needs you to sign in again. | Reconnect Drive |
| Some files failed | **Mostly done.** {n} files couldn't be read. The other {m} are saved. | Retry |
| Cancelled or interrupted | **Stopped.** {n} of {total} files are saved. Retry picks up where it left off. | Retry |
| Background service not running | **The background service isn't running.** Everything already saved is safe. | Restart service |
| Anything else | **Something went wrong.** Everything already saved is safe. | Retry, Send feedback |
| Interrupted last time | **Interrupted last time.** {n} of {total} files are saved. | Retry |
| Cancelling | **Cancelling.** Jarvis will stop after this step. Everything already saved is kept. | (none) |

### Syncing

| Case | Message |
|---|---|
| In progress | **Syncing {name}.** Reading files, {n} of {total}. You can keep using Jarvis. |
| Busy labels | Syncing…, Retrying…, Restarting… |
| Finished | **All caught up.** {total} files saved from {name}. Nothing needs your attention. (Action: Search your files) |

### Updates

| State | Message | Action |
|---|---|---|
| Available | **Jarvis {version} is available.** | Update, Later |
| Downloading | **Downloading the update.** {mb} of {total} MB. You can keep working. | (none) |
| Ready | **Update ready.** Restart Jarvis to finish. Your current version keeps working until then. | Restart now, Later |
| Restart while syncing (confirm) | Title: **Restart now?** A sync is running and will stop. You can retry it after the update. | Restart now, Cancel |
| Installing | **Installing the update.** Jarvis will reopen in a moment. | (none) |
| Download failed | **The update didn't download.** Your current version keeps working. | Retry |
| Install failed | **The update couldn't be installed.** Your current version keeps working. | Retry, Download manually |
| Didn't finish | **The last update didn't finish.** Your current version is running. | Retry |
| Up to date | **You're up to date.** | (none) |
| Cannot update here (macOS) | Move Jarvis to your Applications folder and reopen it to update. | (none) |

### Setup

| Item | Message |
|---|---|
| Card title | **Finish setting up** |
| Strip | **Setup.** {n} of 3 done. Connect Claude Desktop so Claude can use your files. |
| API key (Settings or recovery), restart needed | Saved. Restart the background service to apply. (Action: Restart service) |
| Checking | Checking… |
| Claude Desktop, not connected | Connect Claude Desktop (optional). (Action: Connect) |
| Claude Desktop, needs repair | The Claude connection needs repair. (Action: Repair) |
| Claude Desktop, not installed | Claude Desktop isn't installed. |
| Claude Desktop, cannot connect here | Move Jarvis to your Applications folder and reopen it. |
| After connecting | Restart Claude Desktop to finish. |
| Dismiss | I don't use Claude Desktop |
| Exits | Quit Jarvis, Not now |

### Empty states and dialogs

| Where | Message |
|---|---|
| No Sources | **Add your first source.** Point Jarvis at a folder or Google Drive. It reads the files once, then keeps them current. (Action: Add your first source) |
| Add source chooser | Title: **Add a source.** Options: Folder, Google Drive. Drive: Sign in to Google to continue. Exit: Cancel |
| Search placeholder | Search your notes, files and Drive content |
| No results | **No files matched.** Add more sources to search more of your files. (Action: Go to Sources) |
| Topics empty | **Topics appear after your first sync.** |
| Confirm (template) | Title states the action. Body states the consequence. Buttons: Cancel (default), the action |

API key helper text and any wording that makes a claim about how keys are stored must be confirmed for accuracy before it ships.

## 7. Platform notes

One design for Windows and macOS; nothing is done until it is checked on both. Known differences (from `research/platform-deltas`, code read only, nothing run on either OS):

- Only Windows forces a dark title bar today (`enable_dark_title_bar` in `theme.py`). With a light theme that must be removed or made light. Both OSes use native title bars.
- Fonts differ by design (Segoe UI, system font). Text will wrap slightly differently; layouts must tolerate it.
- macOS has an `.icns` app icon; Windows has no `.ico` or installer icon yet (planned: one 16-256 px `.ico` from `icon.svg`).
- Updates install differently (macOS script with `hdiutil`, Windows Inno Setup silent) and neither can show progress once the app quits. See the Installing message.
- Under macOS App Translocation the updater cannot work; see the "Cannot update here" message.
- Both builds are unsigned and not notarized; Gatekeeper and SmartScreen warnings are likely and are recorded, not designed around.

### What the toolkit cannot draw

Qt styling (QSS) has no box-shadow, no transition and no backdrop blur. Gradients, transparent colours, radius, per-state styling and outlines are supported. A widget holds one graphics effect. A translucent window needs a frameless window (no native title bar). Do not design what these rules rule out. (Research: `research/qss-capabilities`, cea9b5e, unverified on either OS.)

## 8. Open items

- **Behaviour under OS dark mode:** light-only regardless (force a light appearance, if Qt allows on both OSes) or let native chrome follow the OS. Needs a check of what Qt and the native title bars do on Windows and macOS.
- **Layering of dialogs over the page** on a light theme (borders only, no shadow).
- **Imagery and illustration** rules; wider iconography beyond the sidebar and inline sizes.
- **Unverified in a running app (both OSes):** whether the banners paint their themed background, border and radius (they are plain widgets with no paint hook), button keyboard focus, nav icon sharpness on high-density screens, and how the system font resolves on macOS.
- **Contrast edge:** `ERROR` on `ERROR_BG` (4.63) and `SUCCESS` on `SUCCESS_BG` (4.69) pass with little room; keep these exact values.
- **Wording to confirm:** the message catalogue in section 6, and any claim in the API key helper text.

## 9. Implementation map

Mapping the doc to code, for whoever builds it (not part of the design):

- Tokens: `jarvis/modules/desktop_app/theme.py`. Existing constants keep their names; add `BORDER_CONTROL`, `ACCENT_TEXT`, `ACCENT_LIGHT`, `ACCENT_SECONDARY`, `WARNING_BORDER`, `SUCCESS_BORDER`, `INFO_BORDER`, a page-title size, and the new spacing steps.
- Replace the literal `white` text colours on accent fills with `ACCENT_TEXT`.
- Replace every `setPointSize` call with pixel sizes (`search_view.py`, `feedback_dialog.py`).
- Add a `QPushButton:focus` outline rule; Qt removes the native focus ring for these.
- Banners (`DaemonStatusBanner`, `UpdateBanner`) need a paint hook (`WA_StyledBackground` or a `paintEvent`) for their background and border to render.
- Nav: `views/nav_rail.py` becomes the expanded/collapsible sidebar; icons drawn at device pixel ratio and recoloured per state.
- Windows title bar: remove or lighten `enable_dark_title_bar` and its filter.
