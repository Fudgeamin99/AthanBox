"""Theme and layout settings for the prayer-times display and the control panel.

Stored in config.toml as [display] and [panel]. Anything missing or invalid falls back to the
defaults, so an old or hand-edited settings file never breaks a screen.
"""
from __future__ import annotations

import re

DISPLAY_THEMES = ("green", "night", "navy", "maroon", "sand", "custom")
PANEL_ACCENTS = ("green", "blue", "navy", "maroon", "purple", "brown", "custom")
PARTS = ("clock", "date", "arabic", "countdown", "sunrise", "status", "pattern")

DISPLAY_DEFAULTS = {
    "theme": "green", "background": "#0d4a3f", "accent": "#f3e2b4",
    "layout": "cards",            # prayer times as "cards" along the bottom or a "list" at the side
    "scale": 100,                 # text size, percent
    "title": "",                  # masjid name in the header (empty: the location)
    "message": "",                # notice line along the bottom
    "hide": [],                   # parts left off the screen (see PARTS)
    "buttons": "always",          # "always" or "auto" (only after the mouse moves or the screen is tapped)
    "live_layout": "list",        # prayer times beside Makkah live: a "list" at the side or "cards" below
    "live_side": "left",          # Makkah live picture on the "left" or "right"
    "live_width": 75,             # most of the width the picture may take, percent
    "live_start": False,          # screens that haven't chosen start with Makkah live
}
PANEL_DEFAULTS = {"mode": "auto", "accent": "green", "color": "#146356"}

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _color(v, default: str) -> str:
    return v if isinstance(v, str) and _HEX.match(v) else default


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def _text(v, limit: int) -> str:
    return " ".join(str(v).split())[:limit] if isinstance(v, (str, int, float)) else ""


def display(cfg: dict) -> dict:
    d = cfg.get("display") if isinstance(cfg.get("display"), dict) else {}
    D = DISPLAY_DEFAULTS
    hide = d.get("hide") if isinstance(d.get("hide"), list) else []
    return {
        "theme": d.get("theme") if d.get("theme") in DISPLAY_THEMES else D["theme"],
        "background": _color(d.get("background"), D["background"]),
        "accent": _color(d.get("accent"), D["accent"]),
        "layout": d.get("layout") if d.get("layout") in ("cards", "list") else D["layout"],
        "scale": _int(d.get("scale"), D["scale"], 70, 160),
        "title": _text(d.get("title", ""), 80),
        "message": _text(d.get("message", ""), 400),
        "hide": [p for p in PARTS if p in hide],
        "buttons": d.get("buttons") if d.get("buttons") in ("always", "auto") else D["buttons"],
        "live_layout": d.get("live_layout") if d.get("live_layout") in ("cards", "list") else D["live_layout"],
        "live_side": d.get("live_side") if d.get("live_side") in ("left", "right") else D["live_side"],
        "live_width": _int(d.get("live_width"), D["live_width"], 50, 85),
        "live_start": bool(d.get("live_start", D["live_start"])),
    }


def panel(cfg: dict) -> dict:
    p = cfg.get("panel") if isinstance(cfg.get("panel"), dict) else {}
    P = PANEL_DEFAULTS
    return {
        "mode": p.get("mode") if p.get("mode") in ("auto", "light", "dark") else P["mode"],
        "accent": p.get("accent") if p.get("accent") in PANEL_ACCENTS else P["accent"],
        "color": _color(p.get("color"), P["color"]),
    }


def check(cfg: dict):
    """Refuse settings that can't be right, with a message for the person saving them."""
    for section, keys in (("display", ("background", "accent")), ("panel", ("color",))):
        sec = cfg.get(section) or {}
        if not isinstance(sec, dict):
            raise ValueError(f"[{section}] must be a table")
        for k in keys:
            v = sec.get(k)
            if v not in (None, "") and not (isinstance(v, str) and _HEX.match(v)):
                raise ValueError(f"The {section} {k} colour must look like #1a2b3c")
