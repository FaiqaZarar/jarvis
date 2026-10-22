# Working in this repo

Before writing or reviewing code, read `docs/coding-guides/`:

- `architecture.md` — the connector layer (`jarvis/modules/connectors/`, `jarvis/modules/pipeline.py`): the `Connector` interface, why it's an ABC, package layout, and how to add a new connector.
- `database-access.md` — the storage layer (`jarvis/internal/db/`): package-per-table layout, the `read_query`/`write_query` middleware, connection/commit conventions, and how to add a new table.
- `comments.md` — when a comment earns its place versus when the code should just speak for itself.

Follow the conventions already established there rather than introducing new patterns — in particular, no back-compat wrappers when restructuring code, and package splits re-export through `__init__.py` so call sites don't change.

Before writing or changing any UI (`jarvis/modules/desktop_app/`), read `DESIGN.md` at the repo root: the design system (tokens, components, voice and copy). Scope and decision records live in `docs/design/`.
