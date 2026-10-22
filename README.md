# Jarvis

**Jarvis keeps your files and notes in one place, so Claude can use them.**

Jarvis is a desktop app that reads your local folders and Google Drive, keeps them in a private library on your own computer, and organizes them into topics automatically. Claude Desktop (or any MCP client) can then search that library, browse its topics and cite exactly which files an answer came from.

<!-- Screenshot: docs/images/sources.png (the Sources page with a few synced folders). -->

## Features

- **Your files stay on your machine.** The library, the search index and the topic tree live in a local database. Nothing is uploaded except the text sent to the embedding provider you choose.
- **Local folders and Google Drive.** Add a folder or pick a Drive folder; Jarvis reads it once, then keeps it current with fast incremental syncs.
- **Hybrid search.** Keyword (BM25) and semantic (vector) search combined, over Markdown, text, Word, PDF, CSV and HTML.
- **Automatic topics.** Files are clustered into a topic hierarchy derived from the content itself, which stays stable across re-syncs.
- **Built for Claude.** An MCP server exposes `search`, `get_hierarchy`, `browse`, `related`, `get_document` and `show_provenance`, and the app can connect Claude Desktop for you in one click.
- **Calm by design.** Plain-language progress, failures shown with the one action that fixes them, and a Remove action that never touches your files.

## How it works

```
Local folders ─┐                                      ┌─ Jarvis desktop app
               ├─► sync ► chunk ► embed ► organize ───┤
Google Drive ──┘        (local background service)    └─ Claude Desktop / any MCP client
                                    │
                        jarvis.db + topic graph (on your computer)
```

A small background service owns the library. The desktop app, the command-line tools and the MCP server all talk to it over `127.0.0.1`; it starts automatically when something needs it. See [docs/coding-guides/architecture.md](docs/coding-guides/architecture.md) for the details.

## Getting started

### Install

Download the latest installer for Windows, macOS or Linux from the [Releases](https://github.com/FaiqaZarar/jarvis/releases) page.

Or run it from source (Python 3.11 or newer):

```bash
git clone https://github.com/FaiqaZarar/jarvis.git
cd jarvis
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e ./jarvis/server
cp .env.example .env                 # Windows: Copy-Item .env.example .env
```

On Windows, clone into a short path such as `C:\src\jarvis`; a deeply nested folder can make `pip install` fail on Windows' path-length limit (see the [development guide](docs/development.md#setup)).

Put an [OpenRouter](https://openrouter.ai) API key in `.env` as `EMBEDDING_API_KEY`. It is used to embed your text for search and to name topics.

### Use it

```bash
python -m jarvis.cmd.desktop_app
```

1. **Add a source:** choose a folder, or sign in to Google and pick a Drive folder.
2. **Wait for the first sync.** Progress shows on the source's row; you can keep using the app.
3. **Search** your files, or browse them by **Topic**.
4. **Connect Claude Desktop** from the setup checklist or Settings, then restart Claude Desktop. Ask Claude about your files and it will search your library.

Google Drive from source needs your own OAuth client; see [docs/development.md](docs/development.md#google-drive).

## Privacy and telemetry

Your files, searches and file names never leave your computer through Jarvis's own telemetry.

Official builds share anonymous error reports and usage counts (for example "a sync finished in 34 s" or which error code occurred) to help fix problems. This can be turned off in **Settings → Privacy**, or with `JARVIS_TELEMETRY=0`. Builds from source send nothing unless you configure a telemetry key yourself. The full design, including exactly which fields are allowed, is in [docs/observability-DESIGN.md](docs/observability-DESIGN.md).

## Documentation

- [Development guide](docs/development.md): repo layout, running the tests, the CLI, manual MCP setup, packaging
- [Architecture](docs/coding-guides/architecture.md) and [database access](docs/coding-guides/database-access.md)
- [Design system](DESIGN.md): the UI's tokens, components and voice
- [Glossary](CONTEXT.md)

## Known limitations

- Small libraries (under about 15 passages) produce provisional topic groupings.
- Search works best with a few literal keywords rather than a whole sentence.
- Supported file types: `.md`, `.txt`, `.docx`, `.pdf`, `.csv`, `.html`. Others are skipped.
- The Notion connector is not implemented yet.

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) and our [Code of Conduct](CODE_OF_CONDUCT.md) first. To report a security issue, see [SECURITY.md](SECURITY.md).

## Maintainers

Jarvis is built and maintained by:

<table>
  <tr>
    <td align="center"><a href="https://github.com/ashar-shaikh"><img src="https://github.com/ashar-shaikh.png" width="80" alt=""><br><b>Ashar Shaikh</b></a></td>
    <td align="center"><a href="https://github.com/FaiqaZarar"><img src="https://github.com/FaiqaZarar.png" width="80" alt=""><br><b>Faiqa Zarar</b></a></td>
    <td align="center"><a href="https://github.com/mazeem147"><img src="https://github.com/mazeem147.png" width="80" alt=""><br><b>Mohammad Azeem</b></a></td>
  </tr>
</table>

Everyone who contributes is credited in [AUTHORS](AUTHORS) and on GitHub's contributors page.

## License

[MIT](LICENSE)
