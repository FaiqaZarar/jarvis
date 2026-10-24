# Working in this repo

Instructions for AI coding agents (and a useful checklist for people too).

## Before writing or reviewing code

Read `docs/coding-guides/`:

- `architecture.md`: the connector layer (`jarvis/server/jarvis_server/connectors/`, `jarvis/server/jarvis_server/pipeline.py`): the `Connector` interface, why it's an ABC, the package layout, and how to add a connector.
- `database-access.md`: the storage layer (`jarvis/server/jarvis_server/internal/db/`): one package per table, the `read_query`/`write_query` middleware, connection and commit conventions, and how to add a table.
- `comments.md`: when a comment earns its place, versus when the code should speak for itself.

Follow the conventions already established there rather than introducing new patterns. In particular: no back-compat wrappers when restructuring code, package splits re-export through `__init__.py` so call sites don't change, and rows are tombstoned, never hard-deleted.

Before writing or changing any UI (`jarvis/modules/desktop_app/`), read `DESIGN.md` at the repo root: the design system's tokens, components, and the voice and vocabulary of user-facing text.

## Setup and tests

```bash
pip install -r requirements.txt
pip install -e ./jarvis/server
python -m pytest tests/
```

The suite runs offline with no API keys. Add or update tests for every behaviour change.

## Rules that matter

- **Privacy:** never send file contents, file names, paths or search text off the machine, except to the user's configured embedding provider. Telemetry goes through the allow-list in `jarvis/server/jarvis_server/internal/telemetry/privacy.py`.
- **Secrets:** never commit `.env`, API keys, tokens or a populated `_bundled_credentials.py`; CI fills that file at build time.
- **User-facing text:** follows `DESIGN.md` §5: plain words ("sync", "background service"), sentence case, no jargon such as "ingest", "daemon" or "embedding".
