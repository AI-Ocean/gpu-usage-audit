"""Default filesystem locations for gua runtime state."""

from __future__ import annotations

from pathlib import Path

DEFAULT_STATE_DIR = Path.home() / ".gua"
DEFAULT_DB_PATH = DEFAULT_STATE_DIR / "gua.db"
DEFAULT_PID_PATH = DEFAULT_STATE_DIR / "gua.pid"
DEFAULT_LOG_PATH = DEFAULT_STATE_DIR / "gua.log"


def expand_path(path: str | Path) -> Path:
    """Expand user-facing filesystem path arguments."""
    return Path(path).expanduser()


def is_default_db_path(path: str | Path) -> bool:
    """Return whether a path points at gua's default history database."""
    return expand_path(path) == DEFAULT_DB_PATH
