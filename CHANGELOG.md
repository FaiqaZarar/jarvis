# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - Unreleased

First public release.

### Added

- Desktop app (Windows, macOS, Linux) to add local folders and Google Drive folders, sync them, search them and browse them by topic.
- Hybrid keyword and semantic search over Markdown, text, Word, PDF, CSV and HTML files.
- Automatic topic hierarchy that stays stable across re-syncs.
- MCP server with `search`, `get_hierarchy`, `browse`, `related`, `get_document` and `show_provenance`, plus one-click Claude Desktop setup.
- Remove a source from the library without touching the files on disk or in Drive.
- Failures shown as banners with the one action that fixes them.
- Optional, privacy-filtered telemetry (OpenTelemetry and PostHog) for official builds, with an opt-out in Settings.
- Optional cloud server for sign-in, feedback and error reports; builds without one run fully offline from it.
