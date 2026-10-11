"""Prayer time calculation.

Standard astronomical approach (solar declination + equation of time),
with the common calculation methods, Asr juristic setting, high-latitude
rules, per-prayer offsets and an optional mosque timetable (CSV) override.
"""
from __future__ import annotations

import csv
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

LOCAL_TZ_NAMES = ("", "local", "system")


def get_zone(name: str | None):
    """ZoneInfo for an IANA name, or None meaning "this computer's time zone".

    Windows has no built-in IANA database (unless the `tzdata` package is installed),
    so "local" uses the operating system's own time zone and daylight-saving rules."""
    if not name or name.strip().lower() in LOCAL_TZ_NAMES:
        return None
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError):
        log.warning("Time zone %r not available on this computer; using the computer's own time zone "
                    "(install the 'tzdata' package to use named zones)", name)
        return None

ITERATIONS = 1
PRAYERS = ["fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"]

# fajr / isha are sun depression angles in degrees; isha_minutes means a
# fixed interval after maghrib; maghrib (angle) is only used by some methods.
METHODS: dict[str, dict] = {
    "MWL":       {"name": "Muslim World League",                   "fajr": 18,   "isha": 17},
    "ISNA":      {"name": "Islamic Society of North America",      "fajr": 15,   "isha": 15},
    "Egypt":     {"name": "Egyptian General Authority of Survey",  "fajr": 19.5, "isha": 17.5},
    "Makkah":    {"name": "Umm al-Qura, Makkah",                   "fajr": 18.5, "isha_minutes": 90},
    "Karachi":   {"name": "University of Islamic Sciences, Karachi", "fajr": 18, "isha": 18},
    "Tehran":    {"name": "Institute of Geophysics, Tehran",       "fajr": 17.7, "isha": 14, "maghrib": 4.5},
    "Jafari":    {"name": "Shia Ithna-Ashari (Qum)",               "fajr": 16,   "isha": 14, "maghrib": 4},
    "Gulf":      {"name": "Gulf Region",                           "fajr": 19.5, "isha_minutes": 90},
    "Kuwait":    {"name": "Kuwait",                                "fajr": 18,   "isha": 17.5},
    "Qatar":     {"name": "Qatar",                                 "fajr": 18,   "isha_minutes": 90},
    "Singapore": {"name": "Singapore / MUIS",                      "fajr": 20,   "isha": 18},
    "Turkey":    {"name": "Diyanet, Turkey",                       "fajr": 18,   "isha": 17},
    "France":    {"name": "UOIF, France (12°)",                    "fajr": 12,   "isha": 12},
    # Moonsighting Committee Worldwide (Khalid Shaukat): 18° but Fajr/Isha follow a seasonal
    # curve based on latitude, never further from sunrise/sunset than the curve allows.
    # Dhuhr +5 min and Maghrib +3 min are part of the method.
    "MoonsightingCommittee": {"name": "Moonsighting Committee Worldwide", "fajr": 18, "isha": 18,
                              "seasonal": True, "adjust": {"dhuhr": 5, "maghrib": 3}},
}

SHAFAQ = {"general": "General (default)", "ahmer": "Ahmer (red twilight)", "abyad": "Abyad (white twilight)"}


def _days_since_solstice(day: date, latitude: float) -> int:
    leap = day.year % 4 == 0 and (day.year % 100 != 0 or day.year % 400 == 0)
    doy = day.timetuple().tm_yday
    days_in_year = 366 if leap else 365
    if latitude >= 0:
        d = doy + 10
        if d >= days_in_year:
            d -= days_in_year
    else:
        d = doy - (173 if leap else 172)
        if d < 0:
            d += days_in_year
    return d


def _seasonal(a: float, b: float, c: float, d: float, dyy: int) -> float:
    if dyy < 91:
        return a + (b - a) / 91.0 * dyy
    if dyy < 137:
        return b + (c - b) / 46.0 * (dyy - 91)
    if dyy < 183:
        return c + (d - c) / 46.0 * (dyy - 137)
    if dyy < 229:
        return d + (c - d) / 46.0 * (dyy - 183)
    if dyy < 275:
        return c + (b - c) / 46.0 * (dyy - 229)
    return b + (a - b) / 91.0 * (dyy - 275)


def mcw_fajr_minutes(latitude: float, day: date) -> float:
    """Minutes before sunrise for Fajr (Moonsighting Committee seasonal curve)."""
    lat = abs(latitude)
    a = 75 + 28.65 / 55.0 * lat
    b = 75 + 19.44 / 55.0 * lat
    c = 75 + 32.74 / 55.0 * lat
    d = 75 + 48.10 / 55.0 * lat
    return _seasonal(a, b, c, d, _days_since_solstice(day, latitude))


def mcw_isha_minutes(latitude: float, day: date, shafaq: str = "general") -> float:
    """Minutes after sunset for Isha (Moonsighting Committee seasonal curve)."""
    lat = abs(latitude)
    if shafaq == "ahmer":
        a, b, c, d = 62 + 17.40 / 55.0 * lat, 62 - 7.16 / 55.0 * lat, 62 + 5.12 / 55.0 * lat, 62 + 19.44 / 55.0 * lat
    elif shafaq == "abyad":
        a, b, c, d = 75 + 25.60 / 55.0 * lat, 75 + 7.16 / 55.0 * lat, 75 + 36.84 / 55.0 * lat, 75 + 81.84 / 55.0 * lat
    else:
        a, b, c, d = 75 + 25.60 / 55.0 * lat, 75 + 2.050 / 55.0 * lat, 75 - 9.21 / 55.0 * lat, 75 + 6.14 / 55.0 * lat
    return _seasonal(a, b, c, d, _days_since_solstice(day, latitude))


# --- degree trig helpers -------------------------------------------------
def _sin(d): return math.sin(math.radians(d))
def _cos(d): return math.cos(math.radians(d))
def _tan(d): return math.tan(math.radians(d))
def _asin(x): return math.degrees(math.asin(x))
def _acos(x): return math.degrees(math.acos(x))
def _atan2(y, x): return math.degrees(math.atan2(y, x))
def _acot(x): return math.degrees(math.atan(1 / x))
def _fix(a, b): a = a - b * math.floor(a / b); return a + b if a < 0 else a
def _fixangle(a): return _fix(a, 360.0)
def _fixhour(h): return _fix(h, 24.0)


def _julian(y: int, m: int, d: int) -> float:
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5


def _sun_position(jd: float) -> tuple[float, float]:
    """Return (declination, equation of time in hours)."""
    d = jd - 2451545.0
    g = _fixangle(357.529 + 0.98560028 * d)
    q = _fixangle(280.459 + 0.98564736 * d)
    l = _fixangle(q + 1.915 * _sin(g) + 0.020 * _sin(2 * g))
    e = 23.439 - 0.00000036 * d
    ra = _atan2(_cos(e) * _sin(l), _cos(l)) / 15.0
    eqt = q / 15.0 - _fixhour(ra)
    decl = _asin(_sin(e) * _sin(l))
    return decl, eqt


@dataclass
class Settings:
    latitude: float
    longitude: float
    timezone: str = "UTC"
    elevation: float = 0.0
    method: str = "MWL"
    fajr_angle: float | None = None
    isha_angle: float | None = None
    isha_minutes: float | None = None
    maghrib_angle: float | None = None
    maghrib_minutes: float = 0.0
    asr: str = "standard"          # "standard" (Shafi'i, Maliki, Hanbali) or "hanafi"
    high_latitude: str = "angle"   # "angle", "seventh", "midnight", "none"
    shafaq: str = "general"        # Moonsighting Committee only: "general", "ahmer", "abyad"
    offsets: dict | None = None    # minutes per prayer, e.g. {"fajr": 2}
    timetable: str | None = None   # optional CSV path

    def params(self) -> dict:
        if self.method not in METHODS and self.method.lower() != "custom":
            raise ValueError(f"Unknown method {self.method!r}. Choose from: {', '.join(METHODS)} or 'custom'")
        p = dict(METHODS.get(self.method, {}))
        if self.fajr_angle is not None:
            p["fajr"] = self.fajr_angle
        if self.isha_angle is not None:
            p["isha"] = self.isha_angle
            p.pop("isha_minutes", None)
        if self.isha_minutes is not None:
            p["isha_minutes"] = self.isha_minutes
            p.pop("isha", None)
        if self.maghrib_angle is not None:
            p["maghrib"] = self.maghrib_angle
        if "fajr" not in p or ("isha" not in p and "isha_minutes" not in p):
            raise ValueError("Custom method needs fajr_angle and isha_angle (or isha_minutes)")
        return p


class PrayerTimes:
    def __init__(self, settings: Settings):
        self.s = settings
        self.tz = get_zone(settings.timezone)
        self.p = settings.params()
        self._timetable = self._load_timetable(settings.timetable) if settings.timetable else {}

    # ----------------------------------------------------------------- public
    def now(self) -> datetime:
        return datetime.now(self.tz) if self.tz else datetime.now().astimezone()

    def to_local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz) if self.tz else dt.astimezone()

    def localize(self, naive: datetime) -> datetime:
        return naive.replace(tzinfo=self.tz) if self.tz else naive.astimezone()

    @property
    def tz_name(self) -> str:
        return str(self.tz) if self.tz else "this computer's time zone"

    def for_date(self, day: date) -> dict[str, datetime]:
        """Prayer times for a date as timezone-aware local datetimes."""
        if day in self._timetable:
            times = dict(self._timetable[day])
        else:
            if self._timetable:
                log.warning("No timetable row for %s, falling back to calculation", day)
            times = self._calculate(day)
        offsets = self.s.offsets or {}
        return {k: v + timedelta(minutes=float(offsets.get(k, 0))) for k, v in times.items()}

    # ------------------------------------------------------------ calculation
    def _calculate(self, day: date) -> dict[str, datetime]:
        s, p = self.s, self.p
        lat, lng = s.latitude, s.longitude
        jdate = _julian(day.year, day.month, day.day) - lng / (15 * 24.0)

        def mid_day(t):
            _, eqt = _sun_position(jdate + t)
            return _fixhour(12 - eqt)

        def sun_angle_time(angle, t, ccw=False):
            decl, _ = _sun_position(jdate + t)
            noon = mid_day(t)
            x = (-_sin(angle) - _sin(decl) * _sin(lat)) / (_cos(decl) * _cos(lat))
            if x < -1 or x > 1:
                return math.nan  # sun never reaches this angle today
            h = _acos(x) / 15.0
            return noon - h if ccw else noon + h

        def asr_time(factor, t):
            decl, _ = _sun_position(jdate + t)
            angle = -_acot(factor + _tan(abs(lat - decl)))
            return sun_angle_time(angle, t)

        rise_set = 0.833 + 0.0347 * math.sqrt(max(s.elevation, 0))
        asr_factor = 2 if s.asr.lower() == "hanafi" else 1

        est = {"fajr": 5, "sunrise": 6, "dhuhr": 12, "asr": 13, "sunset": 18, "maghrib": 18, "isha": 18}
        for _ in range(ITERATIONS):  # refine using previous estimate
            f = {k: v / 24.0 for k, v in est.items()}
            t = {
                "fajr": sun_angle_time(p["fajr"], f["fajr"], ccw=True),
                "sunrise": sun_angle_time(rise_set, f["sunrise"], ccw=True),
                "dhuhr": mid_day(f["dhuhr"]),
                "asr": asr_time(asr_factor, f["asr"]),
                "sunset": sun_angle_time(rise_set, f["sunset"]),
                "maghrib": sun_angle_time(p.get("maghrib", rise_set), f["maghrib"]),
                "isha": sun_angle_time(p["isha"], f["isha"]) if "isha" in p else 18.0,
            }
            # NaN = the sun never reaches that angle today; keep it NaN (handled below)
            # but keep a usable estimate for the next refinement pass.
            est = {k: (est[k] if math.isnan(v) else v) for k, v in t.items()}

        if "maghrib" not in p:
            t["maghrib"] = t["sunset"] + s.maghrib_minutes / 60.0
        if "isha_minutes" in p:
            t["isha"] = t["maghrib"] + p["isha_minutes"] / 60.0

        if p.get("seasonal"):
            t = self._moonsighting(t, day)
        else:
            t = self._high_latitude(t, p)
        for k, minutes in p.get("adjust", {}).items():
            t[k] += minutes / 60.0

        out = {}
        base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        for name in PRAYERS:
            hours = t[name] - lng / 15.0  # local solar -> UTC
            dt = base + timedelta(hours=hours)
            dt = dt.replace(second=0, microsecond=0) + timedelta(minutes=1 if dt.second >= 30 else 0)
            out[name] = self.to_local(dt)
        return out

    def _moonsighting(self, t: dict, day: date) -> dict:
        lat = self.s.latitude
        night = _fixhour(t["sunrise"] - t["sunset"])
        if abs(lat) >= 55:  # the method uses 1/7 of the night above 55°
            t["fajr"] = t["sunrise"] - night / 7.0
            t["isha"] = t["sunset"] + night / 7.0
            return t
        safe_fajr = t["sunrise"] - mcw_fajr_minutes(lat, day) / 60.0
        if math.isnan(t["fajr"]) or safe_fajr > t["fajr"]:
            t["fajr"] = safe_fajr
        shafaq = (self.s.shafaq or "general").lower()
        safe_isha = t["sunset"] + mcw_isha_minutes(lat, day, shafaq) / 60.0
        if math.isnan(t["isha"]) or safe_isha < t["isha"]:
            t["isha"] = safe_isha
        return t

    def _high_latitude(self, t: dict, p: dict) -> dict:
        rule = (self.s.high_latitude or "none").lower()
        night = _fixhour(t["sunrise"] - t["sunset"])

        def portion(angle):
            if rule == "angle":
                return angle / 60.0 * night
            if rule == "seventh":
                return night / 7.0
            if rule == "midnight":
                return night / 2.0
            return None

        fp = portion(p["fajr"])
        if fp is not None and (math.isnan(t["fajr"]) or _fixhour(t["sunrise"] - t["fajr"]) > fp):
            t["fajr"] = t["sunrise"] - fp
        if "isha" in p:
            ip = portion(p["isha"])
            if ip is not None and (math.isnan(t["isha"]) or _fixhour(t["isha"] - t["sunset"]) > ip):
                t["isha"] = t["sunset"] + ip
        for k in ("fajr", "isha"):
            if math.isnan(t[k]):
                raise ValueError(f"{k} cannot be calculated at this latitude; set high_latitude rule")
        return t

    # -------------------------------------------------------------- timetable
    def _load_timetable(self, path: str) -> dict[date, dict[str, datetime]]:
        """CSV with header: date,fajr,sunrise,dhuhr,asr,maghrib,isha  (YYYY-MM-DD, HH:MM 24h).
        Columns other than date are optional; missing ones are calculated."""
        rows: dict[date, dict[str, datetime]] = {}
        with open(Path(path), newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                if not row.get("date"):
                    continue
                d = date.fromisoformat(row["date"])
                calc = None
                entry = {}
                for name in PRAYERS:
                    val = row.get(name, "")
                    if val:
                        hh, mm = (int(x) for x in val.split(":")[:2])
                        entry[name] = self.localize(datetime.combine(d, dtime(hh, mm)))
                    else:
                        calc = calc or self._calculate(d)
                        entry[name] = calc[name]
                rows[d] = entry
        log.info("Loaded %d timetable rows from %s", len(rows), path)
        return rows
