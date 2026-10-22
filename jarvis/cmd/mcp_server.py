"""MCP server entrypoint - a thin stdio-to-daemon proxy.

Runs the Jarvis MCP server over **stdio** - the transport an MCP client
(Claude Desktop, `claude mcp add`, an IDE plugin) launches as a subprocess
and speaks to on stdin/stdout.

    python -m jarvis.cmd.mcp_server

This process no longer opens `jarvis.db` or the LadybugDB graph file
itself (see `docs/local-server-architecture-DESIGN.md`): every tool call
below is one of `jarvis.modules.daemon.client`'s typed wrappers, which
forward to the local server daemon, auto-starting one if none is
reachable yet, and handle a daemon that won't start or stops responding
mid-session (design doc §10 - three tiers, one retry) by returning an
`ErrorResponse` instead of letting an exception reach the MCP client -
the read tools already had that convention (see sql_query.py's docstring
on why raising loses the code/message distinction); this extends it to
daemon-unreachable failures too, not just application-level ones. The
actual `search()` / `get_hierarchy()` / `browse()` / `related()` logic is
unchanged and still lives in `jarvis_server.mcp_tools` - it now runs
inside the daemon process instead of this one, called exactly as it was
before (design doc §4).

**`query_db` is deliberately not registered as a tool here.** It's held
back from beta testers/Claude Desktop while it's still an internal
debugging surface, not a decision this file makes silently - `sql_query.
query_db()`, the daemon's own `/query_db` HTTP route, and
`daemon_client.query_db()` are all still fully intact for that internal
use; only the `@server.tool()` registration an external MCP client would
see is excluded. Re-adding it here (a one-line `@server.tool()` proxy,
identical in shape to `get_document` below) is the only change needed if
that decision changes later.

Deliberately NOT importing `jarvis_server.mcp_tools.search` /
`hierarchy` / `sql_query` here: each of those modules registers its
`@server.tool()` onto the same shared `server` singleton as a side
effect of import, which would double-register every tool name against
the proxy versions defined below. This file registers the *only* copy
of each tool this process ever sees.

Prerequisites, in order:
  1. `python -m jarvis.cmd.migrate`              - create the schema
  2. ingest content (daemon-side, once a client triggers it)
  3. a real EMBEDDING_API_KEY in <repo>/.env     - `search` embeds the query

stdio, not SSE / streamable-HTTP (design doc §6, resolved decision #3 -
stay on stdio for now, revisiting the transport itself is a separate
later item): an MCP client still owns *this* process's lifecycle exactly
as before - nothing changes from the client's point of view. What's new
is that this process is now a proxy, not the thing doing the work.

Client registration + shutdown (see jarvis_server.daemon.server's module
docstring for the full mechanism and the real bug it fixes): this
process registers itself as a daemon client once the daemon is confirmed
reachable, and asks the daemon to retire that registration - which only
actually shuts the daemon down if nothing else is still relying on it -
on any clean exit (KeyboardInterrupt or a normal `server.run()` return).
Mirrors exactly what `jarvis.modules.desktop_app.main_window.MainWindow`
does for the GUI - either one can be "the last client out."

Logging goes to stderr (Python's default), which is correct here: stdout
carries the JSON-RPC protocol frames and must not be written to by
anything else.
"""

import sys
from typing import Optional, Union

from dotenv import load_dotenv

# Load <repo>/.env before importing anything that reads os.environ - see
# the module docstring in the pre-daemon version of this file for why.
load_dotenv()

from jarvis_server.internal.logging.logging_config import get_logger  # noqa: E402
from jarvis.modules.daemon import client as daemon_client  # noqa: E402
from jarvis.modules.daemon.client import DaemonError, DaemonStartupError  # noqa: E402
from jarvis_server.mcp_tools.models import (  # noqa: E402
    BrowseResult,
    DocumentResult,
    ErrorResponse,
    HierarchyResult,
    RelatedResult,
    SearchFilters,
    SearchResult,
)
from jarvis_server.mcp_tools.provenance import ProvenanceAccumulator  # noqa: E402
from jarvis_server.mcp_tools.server import server  # noqa: E402

logger = get_logger(__name__)

# This process is spawned once per MCP client session, so its lifetime is
# the session scope answer provenance needs; the daemon is shared by every
# client and would need invented session identity (design doc §4).
_provenance = ProvenanceAccumulator()

_PENDING_MARKER = (
    "Recorded for answer provenance. Call `show_provenance` after writing "
    "your answer to render the receipt for this turn."
)


@server.tool()
def search(
    query: str,
    filters: Optional[SearchFilters] = None,
    limit: int = 10,
    cursor: Optional[str] = None,
) -> Union[SearchResult, ErrorResponse]:
    """Search the user's personal indexed document corpus (local files,
    Drive, Notion). Use this whenever the user asks about their own notes,
    files, or previously saved content. Hybrid BM25 + vector search with
    RRF fusion, hard pre-filters, and cursor pagination. Read-only.
    See mvp-spec.md §7.1-7.3. (Proxied to the daemon - the authoritative
    implementation is `jarvis_server.mcp_tools.search.search`.)

    Results are recorded for answer provenance; call `show_provenance`
    once, after writing your answer, to show the user which documents
    backed it."""
    result = daemon_client.search(query, filters, limit, cursor)
    if isinstance(result, SearchResult):
        _provenance.record_search(result)
        result.provenance_pending = _PENDING_MARKER
    return result


@server.tool()
def get_hierarchy(
    root: Optional[str] = None, depth: Optional[int] = None
) -> Union[HierarchyResult, ErrorResponse]:
    """The topic tree (spec §7.2). See
    `jarvis_server.mcp_tools.hierarchy.get_hierarchy` for the full
    contract - proxied to the daemon here."""
    return daemon_client.get_hierarchy(root, depth)


@server.tool()
def browse(
    topic_id: str, cursor: Optional[str] = None, limit: Optional[int] = None
) -> Union[BrowseResult, ErrorResponse]:
    """One topic in focus (spec §7.2). See
    `jarvis_server.mcp_tools.hierarchy.browse` for the full contract -
    proxied to the daemon here."""
    return daemon_client.browse(topic_id, cursor, limit)


@server.tool()
def related(id: str, limit: Optional[int] = None) -> Union[RelatedResult, ErrorResponse]:
    """Nearest neighbours (spec §7.2). See
    `jarvis_server.mcp_tools.hierarchy.related` for the full contract -
    proxied to the daemon here."""
    return daemon_client.related(id, limit)


@server.tool()
def get_document(
    document_id: Optional[str] = None, title: Optional[str] = None
) -> Union[DocumentResult, ErrorResponse]:
    """Read ONE document in full, by `document_id` (the id search results
    and provenance receipts report) or by `title`.

    Use this instead of `query_db` whenever you need a document's actual
    text: `query_db` truncates every cell, so it will silently hand you a
    cut-off document. A title lookup returns the most recently modified
    match and names the rest in `other_matches`.

    Proxied to the daemon - see
    `jarvis_server.mcp_tools.document.get_document`."""
    return daemon_client.get_document(document_id, title)


# query_db is deliberately NOT registered here - see the module docstring's
# "query_db is deliberately not registered as a tool here" paragraph above.
# That decision predates this merge and still applies: this file is what
# Claude Desktop and other beta-tester MCP clients actually see, and
# query_db stays an internal debugging surface until that's revisited.
# ProvenanceAccumulator.record_query_db still exists and works (see
# provenance.py) - re-adding a one-line `@server.tool()` proxy here,
# identical in shape to get_document above, is the only change needed if
# that decision changes later; nothing about show_provenance below
# depends on query_db being registered.


@server.tool()
def show_provenance() -> str:
    """Show the user the receipt for the answer you just gave: which
    documents, topics, queries and snippets from their corpus actually
    backed it.

    Call this ONCE at the very END of your turn, AFTER you have finished
    writing your answer, whenever that answer drew on `search`. Position
    matters - the receipt is meant to be read after the answer, and the
    only thing that puts it there is you calling this last. Takes no
    arguments: this server already recorded exactly what it served you,
    so there is nothing for you to reconstruct or summarise.

    Covers every `search` call made since the last time this was called,
    then resets.

    Write nothing after this call. Do not summarise, quote, reformat or
    comment on what it returns - it is already the finished receipt, and
    prose after it puts your words where the receipt was meant to end the
    turn.
    """
    # Returns the Markdown itself, not the ProvenanceResult: a model return
    # type is serialized as JSON, which would put the receipt in front of the
    # user as an escaped string. Read-and-move-on is the whole point.
    #
    # Deliberately no daemon round-trip: a receipt rendered at the end of a
    # turn must not fail on a daemon that has since gone away.
    return _provenance.snapshot_and_reset().markdown


def main() -> None:
    try:
        port = daemon_client.ensure_daemon_running()
    except DaemonStartupError as exc:
        # Same tier-1 failure the per-call proxies above would eventually
        # hit anyway - checked once, up front, so a client gets a clean
        # stderr line and a fast exit instead of a working stdio
        # handshake followed by every tool call failing individually.
        logger.error("%s", exc)
        sys.exit(1)

    client_id = None
    try:
        client_id = daemon_client.register_client()
    except DaemonError:
        logger.warning(
            "Could not register as a daemon client - this session won't be "
            "able to ask the daemon to retire on a clean exit, but every "
            "tool call above already works independently of this."
        )

    logger.info(
        "Starting Jarvis MCP server on stdio (proxy to daemon on 127.0.0.1:%d; "
        "tools: search, get_hierarchy, browse, related, get_document, "
        "show_provenance).",
        port,
    )
    try:
        server.run(transport="stdio")
    except KeyboardInterrupt:
        logger.info("Jarvis MCP server stopped (KeyboardInterrupt).")
    except Exception:
        logger.exception("Jarvis MCP server exited on an unhandled error.")
        raise
    finally:
        if client_id is not None:
            try:
                daemon_client.request_shutdown(client_id)
            except DaemonError:
                pass  # best-effort - see MainWindow.closeEvent's identical reasoning


if __name__ == "__main__":
    main()
