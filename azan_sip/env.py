"""Where the app lives, whether it runs from source (Python) or as a packaged AzanSIP.exe."""
from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))  # True inside the PyInstaller-built AzanSIP.exe
EXE_NAME = "AzanSIP.exe"


def app_dir() -> Path:
    """Folder that holds config.toml, audio/, azan.log and backups/.

    For the .exe this is the folder the .exe is in (not PyInstaller's temporary unpack folder)."""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
