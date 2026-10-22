# J-11 phase 3 — retry/backoff: design

**Status:** findings + proposed defaults, no implementation yet. Same
discipline as the rest of J-11's design docs. Sequenced last, on top of
phases 1/4/2, since it touches every Drive API call site those phases
already built.

**Ticket:** J-11 phase 3. No dedicated spec section — general reliability
expectation implied by treating Drive as a real, occasionally-flaky
network dependency.

---

## 1. Verified current state

### 1a. `googleapiclient` already has real, tested retry/backoff — verified from the installed library's actual source, not assumed from a web search

Version confirmed installed: `google-api-python-client==2.200.0`.
`HttpRequest.execute(num_retries=N)` (default `0`, meaning "try once")
already implements randomized exponential backoff via `_retry_request`
and `_should_retry_response`, both read directly from
`googleapiclient/http.py` in this project's own `.venv`, not inferred.

**Verified retryable set, exact, nothing more:**
- Any HTTP status ≥ 500.
- HTTP 429.
- HTTP 403, **only** when the response body's extracted reason is
  exactly `"userRateLimitExceeded"` or `"rateLimitExceeded"`.
- A fixed set of transport-level exceptions: `ssl.SSLError`,
  `socket.timeout`, `ConnectionError`, `httplib2.ServerNotFoundError`,
  and specific `OSError` errnos (`WSAETIMEDOUT`, `ETIMEDOUT`, `EPIPE`,
  `ECONNABORTED`, `ECONNREFUSED`, `ECONNRESET`).

**Verified NOT retried** (confirmed by reading `_should_retry_response`
and grepping the installed package): `exportSizeLimitExceeded`,
`quotaExceeded`, `dailyLimitExceeded`, `storageQuotaExceeded`, plain
401/404/400/409, and any 403 whose reason isn't one of the two listed
above.

**Backoff formula, exact:** `sleep_time = random.random() * 2**retry_num`
before each retry (none before the first attempt). No base-delay
constant, no maximum-delay cap in the library itself — the multiplier is
pure `2**retry_num`, jitter is the full `[0, 1)` multiplicative term.
Total attempts = `num_retries + 1`.

### 1b. This composes with what already exists, no conflicts

- `fetch_file_bytes`'s `_is_export_size_error` check: confirmed safe.
  `exportSizeLimitExceeded` is not in the library's retryable-reasons
  list, so adding `num_retries` here cannot cause a wasted retry loop on
  an export that will deterministically fail again — the existing
  check still runs exactly as it does today, un-delayed.
- `resolve_shortcut_target`'s 403/404 handling: safe for the same
  reason — a plain "broken/vanished target" 403/404 isn't in the
  retryable-reasons list, so it still raises to that handler
  immediately.
- Phase 2's `changes.list` fallback (`J11_PHASE2_DESIGN.md` §3.4):
  composes cleanly. An invalid/expired page token's error is not in the
  retryable set, so it still reaches the fallback immediately, un-
  delayed. A genuinely transient error on that same call now gets
  real retries first, and only falls through to the existing
  full-sync fallback if those retries are exhausted — a strict
  improvement, no logic there needs to change.

  *Honest note (adversarial review): the first draft of this bullet
  called it "a broad `except HttpError`" and left it at that. That catch
  was **not** broad enough — once retries are in front of the call,
  `googleapiclient` re-raises transport failures (`ssl.SSLError`,
  `socket.timeout`, `ConnectionError`, `httplib2.ServerNotFoundError`)
  **directly, not wrapped in `HttpError`**, so an exhausted-retry
  transport error was escaping the fallback and crashing `sync_drive`.
  Phase 2's catch was widened to `except Exception` (see
  `J11_PHASE2_DESIGN.md` §3.4's note); this bullet is corrected to match.
  Caught after the fact, not decided at design time.*
- Confirmed uniform across every call shape used in `drive.py`,
  including the two media-download calls (`export_media`,
  `get_media`) — both go through the exact same non-resumable
  `execute()` → `_retry_request` path as a plain JSON call; no
  `MediaIoBaseDownload` involved, no special-casing needed.

---

## 2. Spec-locked — not open for reinterpretation

None — this phase has no dedicated spec section. The only constraint
carried over is the general one already governing every prior phase:
don't touch working, already-verified logic (§1b) to add this.

---

## 3. Genuinely open — decisions needed, proposed defaults in italics

### 3.1 The retry count

*Default:* `DRIVE_API_NUM_RETRIES = 5`, one module-level constant in
`drive.py`, used identically on all six call sites. Rationale: the
library has no maximum-delay cap, so the count itself is the only
control over worst-case wait. With the verified formula, cumulative
worst-case wait across 5 retries is bounded by `2**6 - 2 = 62` seconds
(sum of `2**1` through `2**5`, upper bound since `random()` is in
`[0, 1)`) — comfortably bounded for a manually-triggered background
sync a single local user is running, not an interactive request someone
is staring at a spinner for. `5` is a plain, documented, tunable
constant, not a guessed magic number — matching this project's existing
convention for constants like J-09's `RECONCILE_MATCH_TAU`.

**Known, accepted limitation (adversarial review finding 2):** the `~62s`
bound above is *per call*. A full sync of a large Drive makes hundreds of
`.execute()` calls (one `files.list()` page per ~100 files, one
`export_media`/`get_media` per file, plus shortcut `get`s), and there is
**no run-level aggregate deadline or circuit breaker**. Under a sustained
outage or `dailyLimitExceeded` partway through a big first sync, the run
can therefore back off for a very long time — potentially hours — before
it finally reports errors, with no progress signal to the user in the
meantime. That is explicitly *not* addressed here: it is tolerable for a
single-user, manually-triggered background sync at MVP scale, and a
proper fix (a whole-run deadline, or a progress/cancel mechanism) is a
separate, larger design question flagged for a future ticket, not
something to bolt on inside this phase.

### 3.2 Where the constant is defined and applied

*Default:* one constant in `drive.py`, passed as `num_retries=
DRIVE_API_NUM_RETRIES` on every `.execute()` call in the file:
`list_drive_files`'s `files().list()`, `fetch_file_bytes`'s
`files().export_media()` and `files().get_media()`,
`resolve_shortcut_target`'s `files().get()`, `_sync_drive_full`'s
`changes().getStartPageToken()`, and `_sync_drive_incremental`'s
`changes().list()`. No wrapper function, no decorator, no new module —
`num_retries` is a plain keyword argument the library already exposes;
introducing an abstraction layer over six call sites that each already
just call `.execute()` once would be complexity with no purpose.

---

## 4. Out of scope for this phase

- Any custom backoff/retry logic — the library's own, verified in §1a,
  is used as-is.
- Any change to which errors are retryable — that's the library's
  decision, not something to override or second-guess without a
  concrete reason not present here.
- A retry/backoff mechanism for anything outside Drive API calls
  (embedding calls, LLM calls) — out of scope for this ticket, and
  those already have their own separate concerns if they need this.

---

## 5. Testing approach

Since the retry *behavior* itself is the library's own, already-tested
responsibility, these tests verify only what's actually under this
project's control: that the constant is correctly wired to every real
call site, not that Google's exponential backoff algorithm works (it
does, that's not this project's code to re-verify).

- Each of the six `.execute()` call sites is invoked with
  `num_retries=DRIVE_API_NUM_RETRIES` — assert via the existing fake
  service/request stubs' captured call arguments (extend them to record
  kwargs if they don't already).
- Regression: the export-size-cap path (`test_doc_over_export_cap_is_
  skipped_with_warning`) still skips cleanly with `num_retries` now
  present on that call — locks in §1b's verified-safe interaction rather
  than just trusting it stays that way as the code evolves.
- Regression: a broken/vanished shortcut target still resolves to the
  correct WARNING and `None`, unaffected by `num_retries` being present
  on that `files().get()` call.

---

## 6. Implementation shape

```python
# drive.py, near the other module-level constants
DRIVE_API_NUM_RETRIES = 5  # see docs/J11_PHASE3_DESIGN.md §3.1 for the
# worst-case-wait reasoning behind this number; googleapiclient's own
# exponential-backoff-with-jitter (verified in googleapiclient/http.py,
# not assumed) handles the actual retry/sleep logic.
```

Then, mechanically, six one-line additions of `num_retries=
DRIVE_API_NUM_RETRIES` to each existing `.execute()` call already listed
in §3.2. No other code changes.

---

## 7. Summary

- Verified, not assumed: the exact retryable status/reason set and the
  exact backoff formula, read directly from the installed library's
  source.
- Confirmed safe against every existing specific-error-handling path
  this project already built (export-size cap, shortcut resolution,
  phase 2's cursor-invalidation fallback) — nothing needs to change in
  any of them.
- The entire implementation is one constant and six keyword arguments —
  verifying this thoroughly first avoided building a redundant, worse
  version of something that already existed and was already correct.
