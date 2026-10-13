"""The volume of the computer the Box runs on (a laptop, or a Wyse with a TV on HDMI).

The TV app page's volume slider sets the computer's own sound card volume with amixer, so the
azan on the box's own screen gets quieter or louder like any other sound on that computer. Where
there is no volume control to set (Docker, no sound card, or an HDMI output without one), the box's
own screen turns the azan down itself instead.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from . import updater

log = logging.getLogger("azan-sip")

CONTROLS = ("Master", "Speaker", "PCM", "Headphone")


def cards() -> list[str]:
    try:
        text = Path("/proc/asound/cards").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return re.findall(r"^\s*(\d+)\s+\[", text, re.M)


def set_volume(percent: float) -> bool:
    """Set every volume control on the computer's sound cards; True if at least one was set."""
    amixer = shutil.which("amixer")
    if updater.DOCKER or not amixer:
        return False
    pct = f"{max(0, min(100, round(percent)))}%"
    done = []
    for card in cards():
        for ctl in CONTROLS:
            try:
                r = subprocess.run([amixer, "-c", card, "-M", "-q", "sset", ctl, pct],
                                   capture_output=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0:
                done.append(f"{card}:{ctl}")
    if done:
        log.info("Computer volume set to %s (%s)", pct, ", ".join(done))
    return bool(done)
