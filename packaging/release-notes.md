**This is a preview release.** Jarvis works end to end, but expect rough edges. Please report what you find in [Issues](https://github.com/FaiqaZarar/jarvis/issues) or ask in [Discussions](https://github.com/FaiqaZarar/jarvis/discussions).

## Download

| Platform | File |
| --- | --- |
| Windows 10/11 | `JarvisSetup.exe` |
| macOS (Apple silicon) | `Jarvis.dmg` |
| Ubuntu 22.04+ / Debian (experimental) | `Jarvis.deb` |

## Before you install

**The installers are not code-signed yet**, so your system warns you the first time:

- **Windows:** SmartScreen shows "Windows protected your PC". Click **More info**, then **Run anyway**.
- **macOS:** open the `.dmg`, drag Jarvis to Applications, then right-click Jarvis in Applications and choose **Open**. Click **Open** again in the dialog. If macOS still refuses, go to **System Settings → Privacy & Security** and click **Open Anyway**.
- **Linux:** `sudo apt install ./Jarvis.deb`

## First run

The [getting started guide](https://github.com/FaiqaZarar/jarvis/blob/main/docs/getting-started.md) covers every step below in more detail, plus troubleshooting.

1. **Add your own API key.** Jarvis asks for an [OpenRouter](https://openrouter.ai/keys) key on first launch. It is used to embed your text for search and to name topics. Your key stays on your machine; only the text being embedded is sent to OpenRouter.
2. **Add a source.** Local folders work out of the box.
3. **Connect Claude Desktop** from Jarvis's setup steps, restart Claude, then ask it about your files.

**Google Drive** needs your own Google OAuth client in this preview; see [Google Drive](https://github.com/FaiqaZarar/jarvis/blob/main/docs/getting-started.md#google-drive-optional) in the guide.

## What's in it

- Add local folders and Google Drive folders, sync them, and search them by keyword and meaning.
- Browse your files by an automatic topic tree that stays stable across syncs.
- Use your library from Claude Desktop (or any MCP client) through six tools: `search`, `get_hierarchy`, `browse`, `related`, `get_document` and `show_provenance`.
- Remove a source without touching your files.
- Clear error banners, each with the one action that fixes the problem.

See the [changelog](https://github.com/FaiqaZarar/jarvis/blob/main/CHANGELOG.md) for details.
