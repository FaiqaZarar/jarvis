"""Opens a GitHub issue for each feedback report - GITHUB_TOKEN/GITHUB_REPO
loaded from the environment, same fail-fast shape as jarvis_server's own
connectors/drive/auth.py._client_config_from_env(): a missing credential
is a clear, actionable RuntimeError, not a confusing failure three calls
deep.
"""

import os

import requests

from jarvis_cloud_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

_GITHUB_API_BASE = "https://api.github.com"


def _github_config_from_env() -> tuple[str, str]:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        raise RuntimeError(
            "GITHUB_TOKEN / GITHUB_REPO are not set. Create a fine-grained "
            "GitHub token with Issues: write access on the target repo, and "
            "put both values (repo as 'owner/name') in .env."
        )
    return token, repo


def create_github_issue(title: str, body: str) -> str:
    """Returns the created issue's html_url. Raises requests.HTTPError on
    a real API failure - the caller (the /feedback route) treats that as
    a 502, since the feedback report itself is safe (already committed to
    the feedback table) but the GitHub side-effect didn't happen."""
    token, repo = _github_config_from_env()
    response = requests.post(
        f"{_GITHUB_API_BASE}/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"title": title, "body": body},
        timeout=15,
    )
    response.raise_for_status()
    issue_url = response.json()["html_url"]
    logger.info("Opened GitHub issue for feedback: %s", issue_url)
    return issue_url
