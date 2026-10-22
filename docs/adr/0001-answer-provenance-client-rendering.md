# ADR 0001: Client rendering for answer provenance

**Status:** Accepted
**Date:** 2026-09-18
**Context doc:** `docs/answer-provenance-DESIGN.md` (server-side half)

## Context

The server-side half made every response self-describing: `SearchResult`
echoes its own `query`, and the `chunk_context` view joins document
metadata and topic breadcrumb for raw SQL. None of that reaches the user.
The only thing putting provenance in front of a human was the assistant
choosing to write a "Provenance" section from memory, which it silently
skipped whenever it didn't think to.

The goal for this half: a receipt the user reads **after** the answer,
**reliably**, without depending on the assistant remembering.

The target client is **Claude Desktop**, where `jarvis` is registered as a
local stdio MCP server.

## Constraints (established by investigation, not assumption)

1. An MCP server can only shape the JSON it returns from a tool call. It
   cannot push UI into a chat client.
2. **Claude Desktop chat has no hook or middleware surface.**
   `claude_desktop_config.json` accepts only `mcpServers`,
   `coworkUserFilesPath` and `preferences`. (The `PostToolUse` strings in
   the app bundle belong to the Claude Code runtime embedded in Desktop,
   not the chat surface.)
3. **Claude Code has the inverse capability set**: `PostToolUse` hooks can
   rewrite a tool result (`updatedToolOutput`) or write to the user's
   transcript bypassing the model (`systemMessage`) — but Claude Code does
   not support MCP Apps. No mechanism reaches both clients.
4. **MCP Apps render at the tool-call position.** Placement is
   host-determined; the spec gives servers no placement control. The only
   server knob is `ui/request-display-mode` (inline/fullscreen/pip), which
   changes size, not position.
5. **There is no turn-end signal** exposed to a server, and no
   server-initiated rendering path — no notification, subscription or
   sampling mechanism puts content in the conversation.
6. `annotations.audience: ["user"]` is a no-op: the spec's language is
   permissive, and Claude clients do not honor it
   (anthropics/claude-code#72239).
7. `claude.ai/hidden_in_chat` is per-**tool-definition**, not per-result,
   its consumer is not in the Desktop bundle, and it is feature-flagged.
   There is no collapsed-by-default render state for tool results.

## Decision

A zero-argument **`show_provenance`** tool that the model is instructed to
call last, after writing its answer, returning one aggregated receipt as
Markdown.

Consequences of constraints 4 and 5 taken together: **"positioned after
the answer" and "guaranteed present" are mutually exclusive on this
surface.** Anything guaranteed is attached to a tool result and therefore
renders above the prose; anything after the prose requires the model to
choose to call it.

**We chose position, and accepted model compliance as the cost.**

Rejected alternatives:

- **Provenance panel on every `search` result** — genuinely guaranteed,
  but renders above the answer, and with no collapse or hide mechanism a
  multi-call turn stacks several full-size blocks. Guaranteeing the
  receipt by making it unreadable fails the actual goal, which is an
  intelligible receipt.
- **Hybrid (per-call panels plus a final aggregated one)** — would have
  been the best of both if per-call results could be hidden from the
  transcript, but constraint 7 rules that out. It reduces to the worst
  noise of both options.
- **Better prompting / tool descriptions alone** — the status quo that
  prompted this work.

Supporting decisions:

- **Markdown is the primary artifact**; a `ui://` panel added later
  renders it. Panel rendering is gated by a per-app permission prompt,
  remote feature flags and the "Nonessential services" toggle, and shows
  only a stub when blocked. Text-first degrades readably, and works in
  clients that render no panels — which is how Claude Code gets the
  feature for free despite constraint 3.
- **The accumulator lives in the stdio proxy, not the daemon.** The proxy
  is one process per MCP client session, so its lifetime already *is* the
  session scope; the shared daemon would need invented session identity.
- **Zero required arguments.** Since compliance is the soft spot, the call
  is made as cheap as possible, and the model reconstructing what it saw
  would be a confabulation surface the server can already answer exactly.
- **Evidence, not attribution.** The receipt groups snippets by document;
  it does not map claims to sources. Claim-level attribution would have to
  be asserted by the model rather than derived from the data, and a
  confabulated mapping launders a guess into an audit trail.
- **"Since the last call" is the turn boundary**, per constraint 5. A turn
  that skips the call leaves its entries to be over-reported by the next
  receipt — visible and self-correcting, unlike dropping them on a guessed
  boundary.

## Consequences

- Provenance can still be absent when the model doesn't call the tool.
  `provenance_pending` on search/`query_db` results makes that omission
  conspicuous rather than silent. This is weaker than a guarantee and is
  the accepted residual risk.
- Claude Desktop gets the panel; other clients get the Markdown.
- MCP Apps behavior sits behind flags Anthropic can change remotely, so
  the text path must stay self-sufficient permanently, not just until the
  panel ships.
- Widget rendering is macOS-only in practice; Windows reports the widget
  never mounting (ext-apps#671).
