# Security policy

## Reporting a vulnerability

Please **don't** report security issues in public issues, discussions or pull requests.

Instead, use GitHub's private vulnerability reporting: on this repository, open the **Security** tab and choose **Report a vulnerability**. Only the maintainers can see the report.

Please include:

- what the issue is and what an attacker could do with it;
- steps to reproduce, or a proof of concept;
- the version or commit you tested, and your operating system.

We aim to acknowledge a report within a few days, keep you updated while we work on a fix, and credit you in the release notes if you'd like.

## Supported versions

Security fixes go into the latest release. Please check that an issue still exists in the latest release or on `main` before reporting it.

## Scope

Of particular interest:

- the local background service, which listens on `127.0.0.1` only and rejects requests with a foreign `Host` or any `Origin` header;
- anything that could send a user's file contents, file names or search text off their machine;
- the handling of stored credentials (the Google Drive token and API keys in `.env`);
- the optional cloud server in `jarvis_cloud/`.
