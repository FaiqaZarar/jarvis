# Contributing to Jarvis

Thanks for your interest in improving Jarvis. Bug reports, ideas, documentation fixes and code are all welcome.

By taking part you agree to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting bugs and asking for features

- **Bugs:** open an issue with the **Bug report** template. Include your operating system, how you installed Jarvis, what you did and what happened. Jarvis writes logs to `%LOCALAPPDATA%\Jarvis\Logs` (Windows), `~/Library/Logs/Jarvis` (macOS) or `~/.local/state/jarvis` (Linux); please check a log excerpt for anything private before attaching it.
- **Features:** open an issue with the **Feature request** template and describe the problem you want solved before the solution.
- **Security issues:** please don't open a public issue. See [SECURITY.md](SECURITY.md).

## Development setup

Follow the [development guide](docs/development.md) to set up a source checkout and run the tests. The whole test suite runs offline, without any API key.

## Making a change

1. **Discuss larger changes first.** For anything beyond a small fix, open an issue so we can agree on the approach before you write code.
2. **Branch from `main`** with a short descriptive name, for example `fix/drive-token-refresh`.
3. **Follow the existing conventions.** Read [docs/coding-guides/](docs/coding-guides/) before writing code:
   - [architecture.md](docs/coding-guides/architecture.md) for connectors and the pipeline;
   - [database-access.md](docs/coding-guides/database-access.md) for storage (one package per table, the `read_query`/`write_query` middleware, never hard-delete);
   - [comments.md](docs/coding-guides/comments.md) for when a comment earns its place.
   UI changes follow the design system in [DESIGN.md](DESIGN.md): its tokens, components and the voice and vocabulary of user-facing text.
4. **Add tests** for the behaviour you change, and run the full suite: `python -m pytest tests/`.
5. **Open a pull request** against `main` using the template. Describe what changed and why, and how you tested it. Keep each pull request focused on one change.

## Pull request review

A maintainer will review your pull request. CI must pass, and we may ask for changes; that is a normal part of the process. Once approved, a maintainer merges it.

## Privacy rules for contributions

Jarvis's promise is that users' files stay on their computer. Changes must keep it:

- No new network calls carrying file contents, file names, paths or search text, except to the embedding provider the user configured.
- Telemetry goes through the privacy filter in `jarvis_server/internal/telemetry/privacy.py`. New event properties or span attributes must be added to its allow-list deliberately, and must never carry user content.

## License

By contributing, you agree that your contributions are licensed under the project's [MIT License](LICENSE).
