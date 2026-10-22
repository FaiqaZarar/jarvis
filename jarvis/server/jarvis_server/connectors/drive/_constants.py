"""Constants shared across more than one drive submodule."""

CONNECTOR_NAME = "drive"

# googleapiclient's own backoff/retry handles the actual sleep logic; this
# just sets the retry budget.
DRIVE_API_NUM_RETRIES = 5

# Shared because both fetch.py (resolves it) and sync.py (excludes it from
# fast-skip/rename) need it.
_SHORTCUT = "application/vnd.google-apps.shortcut"
