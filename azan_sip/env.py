"""Where the app lives."""
from __future__ import annotations

from pathlib import Path


def app_dir() -> Path:
    """Folder that holds config.toml, audio/, azan.log and backups/."""
    return Path(__file__).resolve().parent.parent
