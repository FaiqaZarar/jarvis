# Getting started

This guide takes you from download to asking Claude about your files. It takes about ten minutes, plus however long your first sync takes.

You need:

- A computer running **Windows 10 or 11**, **macOS on Apple silicon** (M1 or newer), or **Ubuntu 22.04 or newer** (Linux support is experimental).
- An **[OpenRouter](https://openrouter.ai) account** with a little credit. Jarvis uses it to turn your text into search vectors and to name topics. A library of a few hundred documents typically costs cents.
- **[Claude Desktop](https://claude.ai/download)**, if you want Claude to use your files. Jarvis works on its own without it.

## 1. Install

Download the file for your computer from the [latest release](https://github.com/FaiqaZarar/jarvis/releases):

| Computer | File |
| --- | --- |
| Windows | `JarvisSetup.exe` |
| Mac | `Jarvis.dmg` |
| Ubuntu / Debian | `Jarvis.deb` |

Jarvis's installers are not code-signed yet, so your computer warns you the first time. This is expected for a preview release.

**Windows**

1. Run `JarvisSetup.exe`.
2. If you see **Windows protected your PC**, click **More info**, then **Run anyway**.
3. Follow the installer. It installs for your user only and needs no administrator password.

**Mac**

1. Open `Jarvis.dmg` and drag **Jarvis** into **Applications**.
2. In **Applications**, right-click **Jarvis** and choose **Open**, then click **Open** in the dialog.
3. If macOS still refuses, open **System Settings → Privacy & Security**, scroll down, and click **Open Anyway** next to the message about Jarvis.

Always run Jarvis from **Applications**. Run from the disk image or Downloads, it can't update itself or connect to Claude Desktop.

**Ubuntu / Debian**

```bash
sudo apt install ./Jarvis.deb
```

Then open **Jarvis** from your applications menu, or run `jarvis` in a terminal.

## 2. Add your API key

1. Create a key at [openrouter.ai/keys](https://openrouter.ai/keys) and copy it.
2. Open Jarvis. The first time, it asks: **Jarvis needs an API key to read your files.**
3. Paste the key and click **Save**.

The key is saved on your computer, in your Jarvis library folder (see [Where your library lives](#where-your-library-lives)). You can change it later in **Settings**.

## 3. Add your files

1. On the **Sources** page, click **Add a source**.
2. Click **Folder** and pick a folder. Jarvis reads Markdown, text, Word, PDF, CSV and HTML files (`.md`, `.txt`, `.docx`, `.pdf`, `.csv`, `.html`) and skips everything else.
3. Jarvis shows **Syncing**, with the folder's name and a count of files read. You can keep using the app while it works.
4. When it's finished you'll see **All caught up.**

After that, Jarvis keeps the folder current: new and changed files are picked up on the next sync, and removing a source never touches the files themselves.

To try Jarvis before pointing it at your own files, any folder of notes or documents works. Twenty or more files give the topic view enough to group.

## 4. Search and browse

- **Search:** type a few keywords. Short, literal keywords work better than a whole sentence.
- **Topics:** Jarvis groups your files into topics automatically and keeps the grouping stable as you add more. With very few files (under about 15 passages), the groups are provisional.
- **Activity:** what synced, when, and anything that needs attention.

## 5. Connect Claude Desktop

1. Install and open [Claude Desktop](https://claude.ai/download) at least once.
2. In Jarvis, on the **Sources** page, click **Connect** next to **Connect Claude Desktop**.
3. Click **Connect automatically**. Jarvis adds itself to Claude Desktop's settings and leaves everything else there as it is.
4. **Quit Claude Desktop completely and reopen it.** On Windows, quit it from the system tray icon too: closing the window isn't enough.
5. Ask Claude about your files, for example: *"Search my files for the launch plan and summarize it."* Claude searches your library and says which files the answer came from.

If Claude says it can't find anything, check that Jarvis has finished syncing, and that Claude Desktop was fully restarted after connecting.

## Google Drive (optional)

In this preview, Google Drive needs a Google sign-in client of your own. It's free and takes about ten minutes:

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable the **Google Drive API**.
2. Under **Google Auth Platform**, set up the consent screen as **External**, and add your own Google account as a **test user**.
3. Create an **OAuth client** of type **Desktop app**. Copy its **client ID** and **client secret**.
4. Open your Jarvis library folder (see below), open the file named `.env` in a text editor, and add two lines:

   ```
   GOOGLE_OAUTH_CLIENT_ID=your-client-id
   GOOGLE_OAUTH_CLIENT_SECRET=your-client-secret
   ```

5. Quit Jarvis completely and reopen it.
6. Click **Add a source**, then **Google Drive**, sign in in the browser window that opens, and pick a folder.

While your consent screen is in Google's "Testing" mode, Google asks you to sign in again about once a week. Jarvis shows **Google Drive needs you to sign in again** when that happens; click **Reconnect Drive**.

## Where your library lives

Everything Jarvis stores is in one folder on your computer:

| Computer | Library folder |
| --- | --- |
| Windows | `%APPDATA%\Jarvis\Jarvis` (paste it into File Explorer's address bar) |
| Mac | `~/Library/Application Support/Jarvis` (in Finder, **Go → Go to Folder…**) |
| Linux | `~/.local/share/Jarvis/Jarvis` |

It holds the search database, the topic tree and your settings (`.env`, including your API key). Your original files are never copied into it or changed.

Logs are kept separately, in `%LOCALAPPDATA%\Jarvis\Logs` on Windows, `~/Library/Logs/Jarvis` on a Mac, and `~/.local/state/jarvis` on Linux.

## Updating

Jarvis checks for a new version when it starts and shows **Jarvis {version} is available.** Click **Update**. On Linux, download the new `.deb` from the [releases page](https://github.com/FaiqaZarar/jarvis/releases) and install it the same way.

## Troubleshooting

| You see | What to do |
| --- | --- |
| **Paused.** Jarvis needs a valid API key… | Click **Fix API key** and paste a working key. Check your OpenRouter account has credit. |
| **The background service isn't running.** | Click **Restart service**. If it keeps happening, quit Jarvis and reopen it. |
| **Mostly done.** {n} files couldn't be read. | Usually damaged or password-protected files. Click **Retry**; the rest of your library is fine. |
| **Stopped.** or **Interrupted last time.** | Click **Retry**. Jarvis picks up where it left off. |
| **The Claude connection needs repair.** | Click **Repair**, then fully restart Claude Desktop. This happens if Jarvis moved, for example after reinstalling. |
| Claude doesn't use your files | Fully quit and reopen Claude Desktop (including the tray icon on Windows), and check Jarvis shows **All caught up**. |
| The Mac says Jarvis is damaged or can't be opened | Follow the Mac steps in [Install](#1-install), using right-click **Open**. |

Still stuck? Ask in [Discussions](https://github.com/FaiqaZarar/jarvis/discussions) or [open an issue](https://github.com/FaiqaZarar/jarvis/issues/new/choose). The log files (see [Where your library lives](#where-your-library-lives)) help us find the problem; check them for anything private, such as file names, before attaching them.

## Uninstalling

- **Windows:** **Settings → Apps → Installed apps → Jarvis → Uninstall**.
- **Mac:** drag **Jarvis** from **Applications** to the Bin.
- **Linux:** `sudo apt remove jarvis`.

Uninstalling leaves your library folder in place, so reinstalling picks up where you left off. To remove everything, delete the library folder too. If you connected Claude Desktop, also remove the `jarvis` entry from Claude Desktop's settings (**Settings → Developer → Edit Config**).
