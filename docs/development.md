# Development guide

Everything you need to work on Jarvis from a source checkout. For what Jarvis is and how to use it, start with the [README](../README.md).

## Repo layout

Two Python packages, split along the client/server boundary (see [local-server-architecture-DESIGN.md](local-server-architecture-DESIGN.md)):

- **`jarvis/`**: the client side. Every runnable entrypoint (`jarvis/cmd/`), the background service's HTTP client and the PySide6 desktop app. Not pip-installed; run as `python -m jarvis.cmd.<name>` from the repo root.
- **`jarvis/server/jarvis_server/`**: the server side. The background service, the ingestion pipeline, connectors, database access, the MCP tool implementations and the organize engine. An installable package: `pip install -e ./jarvis/server` is required, since every client entrypoint imports from it.

```
jarvis/
  cmd/                     runnable entrypoints, one file each
    desktop_app.py         the desktop app (and the packaged executable's argv dispatch)
    daemon.py              the background service (normally started automatically)
    mcp_server.py          MCP server over stdio, a proxy to the background service
    filesystem_ingest.py   CLI: sync a local folder
    drive_ingest.py        CLI: sign in to Google Drive and sync a folder
    migrate.py             apply database migrations
    debug_ui.py            local MCP debug console
  modules/
    daemon/                typed HTTP client; starts the background service on demand
    desktop_app/           PySide6 app: main_window.py, views/, theme.py, dialogs
    cloud/                 optional cloud server client (sign-in, feedback)
  server/jarvis_server/
    daemon/server.py       the background service: owns jarvis.db and the graph file
    pipeline.py            sync -> chunk -> embed -> index -> organize -> reconcile
    connectors/            filesystem/, drive/, notion.py (stub), the Connector interface
    mcp_tools/             search, hierarchy, document, provenance, sql_query
    organize/              UMAP/HDBSCAN clustering, LLM topic naming, reconciliation
    internal/              db/, graph_db/, normalizer/, chunk/, embed/, llm/, telemetry/
    migrations/            numbered .sql files, applied automatically on startup
jarvis_cloud/              optional hosted server: magic-link sign-in, feedback to GitHub
docs/                      design notes and coding guides
eval/                      retrieval-accuracy gold set and harness
packaging/                 PyInstaller specs, Windows installer script, .deb packaging
tests/
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt      # a few minutes: umap-learn, hdbscan and numba pull compiled wheels
pip install -e ./jarvis/server
cp .env.example .env                 # then set EMBEDDING_API_KEY
```

**Windows:** clone into a short path such as `C:\src\jarvis`. Some PySide6 files sit deep inside the virtual environment, and in a deeply nested folder their full paths pass Windows' 260-character limit, so `pip install` fails with `WinError 206` or "No such file or directory". Alternatively, [enable long path support](https://pip.pypa.io/warnings/enable-long-paths).

`.env` sits at the repo root and is loaded from there whatever your working directory. `EMBEDDING_API_KEY` (an OpenRouter key) is the only value needed for syncing local folders, search and topics. The other settings are documented in `.env.example`.

The database schema is created automatically the first time the background service starts. To apply migrations by hand:

```bash
python -m jarvis.cmd.migrate
```

## Running the tests

The whole suite runs offline; no API key is needed.

```bash
python -m pytest tests/
```

Live-API and slow cross-process tests are skipped by default; enable them with `--run-live` and `--run-slow`. The optional cloud server has its own suite under `jarvis_cloud/tests/`.

Before writing code, read the guides in [coding-guides/](coding-guides/): the connector architecture, the database conventions and when a comment earns its place. UI changes follow the [design system](../DESIGN.md).

## Running it

A local background service owns `jarvis.db` and the topic graph file. Every client (the desktop app, the CLI, the MCP server) talks to it over local HTTP and starts it automatically if it isn't running.

**Desktop app:**

```bash
python -m jarvis.cmd.desktop_app
```

**Local folder, from the CLI:**

```bash
python -m jarvis.cmd.filesystem_ingest /path/to/a/folder
```

### Google Drive

Official builds include a shared Google OAuth client. From source, create your own:

1. In Google Cloud, create an OAuth client of type **Desktop app** (no redirect URI needed).
2. On the consent screen, add yourself as a test user. The scope is `drive.readonly`.
3. Put both values in `.env` as `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`.

Then use the desktop app's **Add a source → Google Drive**, or the CLI:

```bash
python -m jarvis.cmd.drive_ingest
```

The first run opens a browser for consent; the token is then cached in `.google_drive_token.json` (gitignored). Testing-mode tokens expire after about a week, and the app asks you to sign in again.

### Search tips

- The keyword side has no stemming and matches every query term, so a few literal words that appear in your files work better than a whole sentence.
- Under about 15 passages, topic groupings are provisional; the organize step logs a warning and marks the result low-confidence.

## MCP server

The desktop app's **Connect Claude Desktop** action writes this for you. To set it up by hand, run the MCP server over stdio:

```bash
python -m jarvis.cmd.mcp_server
```

and register it in Claude Desktop's config file:

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`. Microsoft Store builds of Claude Desktop keep it under `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\` instead.
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "jarvis": {
      "command": "C:\\path\\to\\jarvis\\.venv\\Scripts\\python.exe",
      "args": ["-m", "jarvis.cmd.mcp_server"],
      "cwd": "C:\\path\\to\\jarvis",
      "env": { "PYTHONPATH": "C:\\path\\to\\jarvis" }
    }
  }
}
```

`env.PYTHONPATH` is required: `jarvis` isn't pip-installed, and not every Claude Desktop version honours `cwd` for module resolution.

Registered tools: `search`, `get_hierarchy`, `browse`, `related`, `get_document` and `show_provenance` (call it after `search` to see which files, topics and passages backed an answer). `query_db`, read-only SQL, is available only through the debug console (`python -m jarvis.cmd.debug_ui`).

## Optional cloud server

`jarvis_cloud/` is a small hosted service for magic-link sign-in and for turning in-app feedback and recurring errors into GitHub issues. It is entirely optional: a build without `JARVIS_CLOUD_BASE_URL` (or a bundled address) simply has no sign-in, feedback or error reporting. To run your own, see `jarvis_cloud/.env.example`.

## Packaging

The desktop app, the background service and the CLI freeze with PyInstaller into one executable per platform: a Windows installer (Inno Setup), a macOS `.dmg` and a Linux `.deb`. See `packaging/` and `.github/workflows/build.yml`. CI bakes optional values into the build from repository secrets: the shared Google OAuth client, an embedding key, the telemetry key and the cloud server address. Each one is optional; without it, that feature is simply off.

## Make commands (Windows)

A `Makefile` wraps the common commands. Its recipes run under `cmd.exe` and call `.venv\Scripts\python.exe` directly.

| Command | What it does |
|---|---|
| `make setup` | create `.venv`, install everything, offer to create `.env` |
| `make config` | print the `.env` location, required keys and the Claude Desktop config paths |
| `make migrate` | apply database migrations |
| `make run` | start the MCP server on stdio |
| `make debug-ui` | start the debug console at http://127.0.0.1:8765 |
| `make ingest FOLDER=C:\path\to\docs` | sync one folder |
| `make test` | run the test suite |
| `make clean` | delete `.venv`, caches and the local database (asks you to type `YES` first) |

Install `make` with `winget install --id GnuWin32.Make`, then add `C:\Program Files (x86)\GnuWin32\bin` to your PATH.
