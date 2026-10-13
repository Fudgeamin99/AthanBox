"""Ramadan mode: the dates of the month, its extra sounds and what the display shows.

Settings live in config.toml under [ramadan] (flat keys, see DEFAULTS). Ramadan starts on the
expected date from the tabular Islamic calendar unless the masjid sets the first day itself
after the moonsighting.
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

MODES = ("auto", "on", "off")
ISHA_MODES = ("calc", "fixed", "after")

DEFAULTS = {
    "mode": "auto",               # auto: on for the dates below · on: on now · off
    "start": "",                  # first day of Ramadan, YYYY-MM-DD (empty: the expected date)
    "days": 30,                   # 29 or 30
    "suhoor_on": True, "suhoor_minutes": 10, "suhoor_sound": "tone",
    "wakeup_on": False, "wakeup_minutes": 45, "wakeup_sound": "tone",
    "iftar_on": True, "iftar_sound": "",            # empty: the normal Maghrib azan
    "before_iftar_on": False, "before_iftar_minutes": 5, "before_iftar_sound": "tone",
    "taraweeh_on": True, "taraweeh_minutes": 15, "taraweeh_sound": "tone",
    "qiyam_on": False, "qiyam_time": "02:30", "qiyam_sound": "tone",
    "isha_mode": "calc", "isha_time": "19:30", "isha_after": 90,
    "theme_on": True, "countdown_on": True, "dua_on": True, "counter_on": True, "eid_on": True,
    "eid_prayer1": "", "eid_prayer2": "",
    "takbeer_on": True, "takbeer_minutes": 30, "takbeer_sound": "",
}

# Sounds on the phones, in the order of the day
EVENTS = ("qiyam", "wakeup", "suhoor", "iftar_reminder", "taraweeh", "takbeer")
LABELS = {"qiyam": "Qiyam", "wakeup": "Suhoor wake-up", "suhoor": "Suhoor ends",
          "iftar_reminder": "Before Iftar", "taraweeh": "Taraweeh", "takbeer": "Takbeer"}

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def event_label(name: str, kind: str) -> str:
    if kind == "ramadan":
        return LABELS.get(name, name.title())
    return f"{name.title()} {'reminder' if kind == 'reminder' else 'azan'}"


def parse_time(s: str):
    m = _TIME.match((s or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


# ------------------------------------------------------------------ calendar
# The tabular (arithmetical) Islamic calendar. It can differ from the moonsighting by a day,
# which is why the first day can be set in the control panel.
def islamic_to_jd(y: int, m: int, d: int) -> float:
    return d + math.ceil(29.5 * (m - 1)) + (y - 1) * 354 + (3 + 11 * y) // 30 + 1948439.5 - 1


def to_jd(day: date) -> float:
    return day.toordinal() + 1721424.5


def from_jd(jd: float) -> date:
    return date.fromordinal(int(math.floor(jd - 1721424.5 + 0.5)))


def to_islamic(day: date) -> tuple[int, int, int]:
    jd = math.floor(to_jd(day)) + 0.5
    y = math.floor((30 * (jd - 1948439.5) + 10646) / 10631)
    m = min(12, math.ceil((jd - (29 + islamic_to_jd(y, 1, 1))) / 29.5) + 1)
    d = int(jd - islamic_to_jd(y, m, 1)) + 1
    return int(y), int(m), d


def expected_start(today: date, days: int = 30) -> date:
    """The first day of the Ramadan that hasn't finished yet (its Eid day counts as not finished)."""
    y = to_islamic(today)[0]
    for yy in (y - 1, y, y + 1, y + 2):
        start = from_jd(islamic_to_jd(yy, 9, 1))
        if start + timedelta(days=days) >= today:
            return start
    return from_jd(islamic_to_jd(y + 1, 9, 1))


# ------------------------------------------------------------------ settings
def settings(section: dict | None) -> dict:
    s = dict(DEFAULTS)
    if isinstance(section, dict):
        s.update({k: v for k, v in section.items() if k in DEFAULTS})
    return s


def check(section: dict | None):
    """Refuse settings that can't be right, with a message for the person saving them."""
    if section in (None, {}):
        return
    if not isinstance(section, dict):
        raise ValueError("[ramadan] must be a table")
    s = settings(section)
    if s["mode"] not in MODES:
        raise ValueError("Ramadan mode must be automatic, on or off")
    if s["start"]:
        try:
            date.fromisoformat(str(s["start"]))
        except ValueError:
            raise ValueError("The first day of Ramadan must be a date") from None
    if int(s["days"]) not in (29, 30):
        raise ValueError("Ramadan has 29 or 30 days")
    if s["isha_mode"] not in ISHA_MODES:
        raise ValueError("Unknown Isha setting for Ramadan")
    for k in ("qiyam_time", "isha_time"):
        if s[k] and not parse_time(str(s[k])):
            raise ValueError(f"{k.replace('_', ' ').capitalize()} must be a time like 02:30")
    for k in ("eid_prayer1", "eid_prayer2"):
        if s[k] and not parse_time(str(s[k])):
            raise ValueError("Eid prayer times must look like 08:30")
    for k in ("suhoor_minutes", "wakeup_minutes", "before_iftar_minutes", "taraweeh_minutes",
              "isha_after", "takbeer_minutes"):
        try:
            v = int(s[k])
        except (TypeError, ValueError):
            raise ValueError("Minutes must be whole numbers") from None
        if not 0 <= v <= 240:
            raise ValueError("Minutes must be between 0 and 240")


class Ramadan:
    """Answers "is this a fasting day?", "which day of Ramadan?" and so on for a date."""

    def __init__(self, section: dict | None):
        self.s = settings(section)
        self.mode = self.s["mode"] if self.s["mode"] in MODES else "auto"
        self.days = 29 if int(self.s.get("days") or 30) == 29 else 30
        try:
            self.start_set = date.fromisoformat(str(self.s["start"])) if self.s["start"] else None
        except ValueError:
            self.start_set = None

    def num(self, key: str) -> int:
        try:
            return int(self.s[key])
        except (TypeError, ValueError):
            return int(DEFAULTS[key])

    def period(self, today: date) -> tuple[date, int, bool]:
        """(first day, number of days, expected) of the Ramadan that hasn't finished by `today`."""
        if self.start_set and self.start_set + timedelta(days=self.days) >= today:
            return self.start_set, self.days, False
        return expected_start(today, self.days), self.days, True

    def day_number(self, day: date) -> int | None:
        start, n, _ = self.period(day)
        k = (day - start).days + 1
        return k if 1 <= k <= n else None

    def fasting(self, day: date) -> bool:
        if self.mode == "off":
            return False
        return self.mode == "on" or self.day_number(day) is not None

    def eid_day(self, day: date) -> bool:
        if self.mode == "off":
            return False
        start, n, _ = self.period(day)
        return day == start + timedelta(days=n)

    def hijri_year(self, day: date) -> int:
        start, _, _ = self.period(day)
        return to_islamic(start + timedelta(days=10))[0]

    def hijri(self, day: date) -> str | None:
        k = self.day_number(day)
        if k and self.fasting(day):
            return f"{k} Ramadan {self.hijri_year(day)}"
        if self.eid_day(day):
            return f"1 Shawwal {self.hijri_year(day - timedelta(days=1))}"
        return None
