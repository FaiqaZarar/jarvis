import uuid
from pathlib import Path

_FILE_NAME = ".jarvis_install_id"


def get_install_id(home: Path) -> str:
    """A random, anonymous ID for this install. The desktop app and the
    daemon resolve the same `home`, so both report under one ID. If `home`
    isn't writable this falls back to a per-process ID rather than
    failing - telemetry must never block startup."""
    path = home / _FILE_NAME
    existing = _read(path)
    if existing:
        return existing
    new_id = str(uuid.uuid4())
    try:
        home.mkdir(parents=True, exist_ok=True)
        # "x" so the desktop app and a daemon starting at the same moment
        # can't each write a different ID; the loser reads the winner's.
        with path.open("x", encoding="utf-8") as f:
            f.write(new_id)
    except FileExistsError:
        return _read(path) or new_id
    except OSError:
        pass
    return new_id


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
