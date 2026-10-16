"""Kids mode: a friendlier prayer-times screen for TV screens in a child's room.

Settings are [kids] in config.toml (see settings()). Only the TV screens listed in
kids.screens change; every other screen, the phones and the speakers are left as they are.

Stars ("Did you pray Asr?", and "Did you fast today?" in Ramadan) are kept per screen in kids.json next to config.toml, for the last
few weeks only. The chart starts again every Monday; in Ramadan the screen shows the month's fasts.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, timedelta
from pathlib import Path

PRAYERS = ("fajr", "dhuhr", "asr", "maghrib", "isha")
MARKS = PRAYERS + ("fast",)   # a star for each prayer, and one for each fast in Ramadan
PALETTES = ("sky", "garden", "space")
SOUNDS = ("azan", "chime", "none")
KEEP_DAYS = 35   # (more than a month, for Ramadan's fasts)
MAX_SCREENS = 100

DEFAULTS = {
    "enabled": False,
    "palette": "sky",      # colours: "sky", "garden" or "space"
    "wudu_minutes": 10,    # "Time for wudu" this many minutes before each azan (0 = off)
    "stars": True,         # "Did you pray?" after each azan, and the week's star chart
    "sound": "azan",       # at azan time kids screens play the "azan", a gentle "chime" or "none"
    "volume": 100,         # the loudest kids screens play, percent
    "quiet_fajr": True,    # no sound at Fajr, so nobody is woken
    "screens": [],         # ids of the TV screens that are kids screens
}


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def screen_id(v) -> str:
    """The same cleaning as screens.Screens.touch, so ids always match."""
    return "".join(c for c in str(v) if c.isalnum() or c in "-_")[:40]


def settings(cfg: dict) -> dict:
    k = cfg.get("kids") if isinstance(cfg.get("kids"), dict) else {}
    D = DEFAULTS
    ids = k.get("screens") if isinstance(k.get("screens"), list) else []
    return {
        "enabled": bool(k.get("enabled", D["enabled"])),
        "palette": k.get("palette") if k.get("palette") in PALETTES else D["palette"],
        "wudu_minutes": _int(k.get("wudu_minutes"), D["wudu_minutes"], 0, 30),
        "stars": bool(k.get("stars", D["stars"])),
        "sound": k.get("sound") if k.get("sound") in SOUNDS else D["sound"],
        "volume": _int(k.get("volume"), D["volume"], 0, 100),
        "quiet_fajr": bool(k.get("quiet_fajr", D["quiet_fajr"])),
        "screens": list(dict.fromkeys(s for s in (screen_id(x) for x in ids) if s))[:MAX_SCREENS],
    }


def check(cfg: dict):
    k = cfg.get("kids")
    if k is not None and not isinstance(k, dict):
        raise ValueError("[kids] must be a table")


def is_kids(cfg: dict, sid: str) -> bool:
    s = settings(cfg)
    return s["enabled"] and screen_id(sid) in s["screens"]


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())   # Monday


class Stars:
    """{screen id: {"2026-10-16": ["fajr", "asr"], ...}} in kids.json."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data: dict[str, dict[str, list[str]]] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data = {screen_id(s): {d: [p for p in v if p in MARKS] for d, v in days.items()
                                            if isinstance(v, list)}
                             for s, days in raw.items() if isinstance(days, dict)}
        except (OSError, ValueError):
            pass

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, sid: str, day: date, prayer: str) -> bool:
        sid = screen_id(sid)
        if not sid or prayer not in MARKS:
            return False
        with self.lock:
            days = self.data.setdefault(sid, {})
            got = days.setdefault(day.isoformat(), [])
            if prayer not in got:
                got.append(prayer)
            oldest = (day - timedelta(days=KEEP_DAYS)).isoformat()
            for d in [d for d in days if d < oldest]:
                del days[d]
            while len(self.data) > MAX_SCREENS:   # never let the file grow without end
                del self.data[min(self.data, key=lambda s: max(self.data[s] or [""]))]
            self._save()
        return True

    def week(self, sid: str, day: date) -> dict:
        """This week's chart, Monday first: {"days": [["fajr", ...], ...7], "count": n, "today": [...]}."""
        start = week_start(day)
        with self.lock:
            days = self.data.get(screen_id(sid)) or {}
            out = [list(days.get((start + timedelta(days=i)).isoformat(), [])) for i in range(7)]
        return {"days": out, "count": sum(len([p for p in d if p in PRAYERS]) for d in out), "today": out[day.weekday()]}

    def fasts(self, sid: str, start: date, n: int) -> list[int]:
        """The days of Ramadan (1 to n) this screen's child fasted, from Ramadan's first day."""
        with self.lock:
            days = self.data.get(screen_id(sid)) or {}
            return [i + 1 for i in range(n) if "fast" in days.get((start + timedelta(days=i)).isoformat(), [])]
