# Storage Access Guideline

This records how `jarvis/internal/db/` is shaped and why, and how to extend it. Read this before adding a table, a query, or a migration. See `docs/coding-guides/architecture.md` for the equivalent guide to the connector layer — this package follows the same discipline (package-per-concern, `__init__.py` re-exports, no back-compat wrappers).

## The problem this replaced

Every table's reads and writes used to live in one 651-line `db.py`, and every one of its 29 functions hand-rolled the same ritual: `try: ... conn.execute(...); conn.commit() except sqlite3.Error: conn.rollback(); logger.exception(...); raise`. Understanding "how are topics stored" meant scrolling past seven other tables' code first, and a bug in the ritual itself (commit ordering, what gets logged) had to be checked in 29 places, not one.

A second pass swept up raw `conn.execute()` calls that had grown up outside `db.py` entirely — in `pipeline.py`, `organize/clustering.py`, `organize/reconcile.py`, `graph_db/sync.py`, `graph_db/staged_build.py`, `debug_ui/server.py`, and `cmd/mcp_server.py` — some duplicating a query the package already had a function for. A third pass did the same for `mcp_tools/hierarchy.py`, `mcp_tools/search.py`, and `mcp_tools/_common.py`, including `search()`'s dynamically-built hybrid-ranking queries. **This package is the only place that runs SQL against `jarvis.db`.** If you're about to write `conn.execute(...)` anywhere else, that query belongs in a table module here instead — even a one-line `SELECT`, so logging, metrics, or a future query-optimization pass only has to be added once. The two deliberate exceptions are `cmd/migrate.py` (raw DDL, has to run before this package's tables exist) and `mcp_tools/sql_query.py` (arbitrary user-submitted SQL over its own read-only connection — a different safety story than a fixed, parameterized function).

### Dynamically-built queries: construct outside, execute inside

A query whose shape varies per call (an optional filter that adds a `JOIN`/`WHERE`, a variable-length `IN (...)`) still only executes inside this package - but the *construction* logic can live with its caller if it depends on a type this package shouldn't import. `chunks.get_candidate_chunk_ids(conn, extra_joins, extra_conditions, extra_params)` is the example: it owns the fixed base query and appends whatever fragments it's given, while `mcp_tools/search.py`'s `_build_filter_conditions` is a separate, readable function that translates `SearchFilters` (an MCP wire type) into those fragments. This keeps the db package from importing a type from a higher layer, without moving `conn.execute` out of it.

## Package layout

One module per table (or per group of tables that are only ever written together):

- **`connection.py`** — `DEFAULT_DB_PATH`, `get_connection()`. Connection lifecycle only; not a table module, so it doesn't use the middleware below (there's no existing `conn` to wrap yet).
- **`_middleware.py`** — `read_query` / `write_query`, the shared ritual every table module's functions are decorated with.
- **`documents.py`**, **`document_sources.py`**, **`connector_state.py`**, **`chunks.py`**, **`topics.py`**, **`topic_events.py`**, **`organize_state.py`** — one table each.
- **`embedding.py`** — `chunk_vectors`, `chunks_fts`, and `sync_state` together, not split further. `embed_and_index_chunks` writes all three in one atomic commit; three separate files would just recreate that coordination problem across file boundaries instead of within one. Split a shared module like this only when the tables inside it are genuinely always written together — otherwise default to one file per table.
- **`__init__.py`** re-exports every public name. Callers do `from jarvis.internal import db` then `db.get_connection(...)`, `db.insert_document(...)`, etc. — unaffected by which file a function actually lives in.

## The middleware

`_middleware.py` has two decorators:

```python
@read_query
def get_topic(conn, topic_id): ...

@write_query
def insert_topic(conn, topic_id, name, ...): ...
```

- **`read_query`** — logs and re-raises on `sqlite3.Error`. Never commits or rolls back; a read never wrote anything.
- **`write_query`** — commits on success, rolls back + logs + re-raises on `sqlite3.Error`. Every `write_query`-decorated function gains a `commit: bool = True` keyword argument automatically, even though its own `def` doesn't declare one.

A table function's body is just the SQL and, where needed, shaping the result — no `try`, no `conn.commit()`, no logging.

### The `commit` parameter

Pass `commit=False` to fold several writes into one atomic transaction, then commit once yourself:

```python
db.insert_chunk_vector(conn, chunk_id, blob, commit=False)
db.insert_chunk_fts(conn, chunk_id, text, commit=False)
db.bump_sync_state(conn, sync_id, commit=False)
conn.commit()
```

If any call in a `commit=False` batch raises, `write_query`'s own `rollback()` unwinds everything since the last real commit — nothing in the batch was ever committed, so there's nothing partial to worry about. This is the only mechanism for cross-write transactions in this package; there's no separate "transaction" type or context manager, and one hasn't been needed yet — reach for one only once a real second case shows up.

## Connection rationale

`get_connection()` hands out one `sqlite3.Connection` that every table module shares, with the `sqlite-vec` extension loaded on it unconditionally — not just for the embedding-specific calls. That way vector operations work anywhere the rest of the app already opens a connection, instead of needing a second, inconsistent connection path. There is no per-table or per-concern connection variant; don't add one without a second concrete case that needs it.

No FK enforcement — relationships between tables are enforced at the application layer, not the engine (see migration `0001`'s header for why).

## Write conventions

- **Upsert, not insert-or-fail**, for anything keyed by a natural id that a re-sync might see again (`INSERT ... ON CONFLICT DO UPDATE`) — see `insert_document_source`, `set_connector_state`, `set_organize_state`.
- **Tombstone, never hard-delete** — `tombstoned = 1`, not `DELETE`. The row and its lineage stay for cheap re-appearance (spec §5.2/§6). See `tombstone_document_source`, `mark_topic_tombstoned`.

## Adding a new table

1. Write the migration (`jarvis/migrations/00NN_*.sql`).
2. Add `jarvis/internal/db/<table_name>.py` with its reads decorated `@read_query` and writes decorated `@write_query` — no hand-written `try`/`commit`/`rollback`.
3. Re-export the new names from `jarvis/internal/db/__init__.py` (both the `from ... import ...` line and `__all__`).
4. Add tests to `tests/test_db.py` — this package keeps one flat test file per the rest of the codebase's convention (`test_drive_connector.py` stayed one file when `connectors/drive.py` became a 5-module package); don't split tests to mirror the production module split.
5. If the new table is only ever written together with an existing one in the same commit, put it in that existing module instead of a new one (see `embedding.py`).
