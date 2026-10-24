# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0-preview] - 2026-10-24

First public preview. Installers are not code-signed yet, and you add your own OpenRouter API key on first run.

### Added

- Desktop app (Windows, macOS, Linux) to add local folders and Google Drive folders, sync them, search them and browse them by topic.
- Hybrid keyword and semantic search over Markdown, text, Word, PDF, CSV and HTML files.
- Automatic topic hierarchy that stays stable across re-syncs.
- MCP server with `search`, `get_hierarchy`, `browse`, `related`, `get_document` and `show_provenance`, plus one-click Claude Desktop setup.
- Remove a source from the library without touching the files on disk or in Drive.
- Failures shown as banners with the one action that fixes them.
- Optional, privacy-filtered telemetry (OpenTelemetry and PostHog) for official builds, with an opt-out in Settings.
- Optional cloud server for sign-in, feedback and error reports; builds without one run fully offline from it.
- Release workflow that builds all three installers without any repository secrets and publishes them to this repo's Releases page, tagged `vMAJOR.MINOR.PATCH[-pre]`. The in-app update check compares those versions numerically; pre-release builds are also offered newer pre-releases.
