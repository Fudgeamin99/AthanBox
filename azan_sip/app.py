"""Config loading, broadcasting to targets and the daily scheduler."""
from __future__ import annotations

import logging
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from . import audio, ramadan, rtp, sipserver
from .praytimes import PrayerTimes, Settings
from .sip import SipCall, SipTarget

log = logging.getLogger("azan-sip")

ADHAN_PRAYERS = ["fajr", "dhuhr", "asr", "maghrib", "isha"]


# ------------------------------------------------------------------- audio
class Clip:
    """An audio file (or the built-in test chime) prepared on demand per codec."""

    def __init__(self, source: str, base: Path, volume_db: float = 0.0, lead_in_ms: int = 400,
                 max_seconds: float | None = None, tone_seconds: float = 6.0, repeat_seconds: float | None = None):
        self.source = source
        self.repeat_seconds = repeat_seconds
        self.tone_seconds = tone_seconds
        self.path = None if source == "tone" else (base / source).resolve()
        self.volume_db, self.lead_in_ms, self.max_seconds = volume_db, lead_in_ms, max_seconds
        self._cache: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def get(self, codec: str) -> bytes:
        with self._lock:
            if codec not in self._cache:
                body = (audio.tone(self.tone_seconds, codec, self.volume_db) if self.path is None
                        else audio.load(self.path, codec, self.volume_db))
                if self.max_seconds:
                    body = body[: int(self.max_seconds * audio.SAMPLE_RATE)]
                if self.repeat_seconds and body:   # play it again and again for this long
                    want = int(self.repeat_seconds * audio.SAMPLE_RATE)
                    body = (body * (want // len(body) + 1))[:want]
                self._cache[codec] = audio.silence(self.lead_in_ms, codec) + body + audio.silence(600, codec)
            return self._cache[codec]

    def seconds(self) -> float:
        return len(self.get("PCMU")) / audio.SAMPLE_RATE

    def check(self):
        if self.path is not None and not self.path.is_file():
            raise FileNotFoundError(f"Audio file not found: {self.path}")

    def __repr__(self):
        return self.source if self.path is None else self.path.name


# ----------------------------------------------------------------- targets
@dataclass
class MulticastTarget:
    name: str
    groups: list[tuple[str, int]]
    codec: str = "PCMU"
    ttl: int = 4
    interface: str | None = None
    repeat: int = 1

    def play(self, clip: Clip, stop: threading.Event) -> bool:
        payload = clip.get(self.codec)
        for _ in range(max(1, self.repeat)):
            if stop.is_set():
                break
            n = rtp.stream(payload, self.groups, audio.CODECS[self.codec], stop, ttl=self.ttl,
                           interface=self.interface)
            log.info("[%s] sent %d packets to %s", self.name, n,
                     ", ".join(f"{h}:{p}" for h, p in self.groups))
        return True


@dataclass
class SipCallTarget:
    name: str
    targets: list[SipTarget] = field(default_factory=list)

    def play(self, clip: Clip, stop: threading.Event) -> bool:
        if len(self.targets) == 1:
            return SipCall(self.targets[0], clip.get, stop).run()
        with ThreadPoolExecutor(max_workers=min(32, len(self.targets))) as ex:
            results = list(ex.map(lambda t: SipCall(t, clip.get, stop).run(), self.targets))
        log.info("[%s] %d/%d phones answered", self.name, sum(results), len(results))
        return any(results)


def _hostport(s: str, default_port: int) -> tuple[str, int]:
    host, _, port = str(s).partition(":")
    return host.strip(), int(port) if port else default_port


def build_targets(cfg: dict, require: bool = True) -> list:
    net = cfg.get("network", {})
    local_ip = net.get("local_ip") or None
    out = []
    for i, t in enumerate(cfg.get("targets", []), 1):
        if not t.get("enabled", True):
            continue
        kind = t.get("type", "").lower()
        name = t.get("name", f"target {i}")
        codec = t.get("codec", "PCMU").upper()
        if kind == "multicast":
            addrs = t.get("addresses") or [t.get("address")]
            groups = [_hostport(a, int(t.get("port", 10000))) for a in addrs if a]
            if not groups:
                raise ValueError(f"{name}: multicast target needs 'address'")
            out.append(MulticastTarget(name, groups, codec, int(t.get("ttl", net.get("multicast_ttl", 4))),
                                       t.get("interface") or local_ip, int(t.get("repeat", 1))))
        elif kind == "unicast_rtp":  # raw RTP to phones/speakers listening on a fixed port
            addrs = t.get("addresses") or [t.get("address")]
            groups = [_hostport(a, int(t.get("port", 10000))) for a in addrs if a]
            out.append(MulticastTarget(name, groups, codec, 1, local_ip, int(t.get("repeat", 1))))
        elif kind == "sip":
            uris = t.get("uris") or [t.get("uri")]
            codecs = t.get("codecs") or ([codec] + [c for c in ("PCMU", "PCMA") if c != codec])
            sips = [SipTarget(uri=u if u.startswith("sip") else f"sip:{u}",
                              username=t.get("username", ""), password=t.get("password", ""),
                              auth_username=t.get("auth_username", ""),
                              from_user=t.get("from_user", t.get("username") or "azan"),
                              display_name=t.get("display_name", "Azan"),
                              auto_answer=t.get("auto_answer", True), codecs=[c.upper() for c in codecs],
                              ring_timeout=float(t.get("ring_timeout") or net.get("ring_seconds") or 20),
                              local_ip=local_ip,
                              extra_headers=t.get("extra_headers", []))
                    for u in uris if u]
            out.append(SipCallTarget(name, sips))
        elif kind == "server":  # phones and phone systems that sign in to Azan SIP's own SIP server
            out.append(sipserver.ServerTarget(name, codec, float(t.get("ring_timeout") or net.get("ring_seconds") or 20),
                                              local_ip))
        else:
            raise ValueError(f"{name}: unknown target type {kind!r} (use multicast, sip, unicast_rtp or server)")
    if not out and require:
        raise ValueError("No enabled targets in config")
    return out


# ---------------------------------------------------------------- the app
def load_config(path: str | Path) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


class Shared:
    """Playback state that survives reloading the settings (so saving never cuts off an azan
    or forgets which prayers already played today)."""

    def __init__(self):
        self.busy = threading.Lock()
        self.halt = threading.Event()
        self.now_playing: str | None = None
        self.fired: set[tuple] = set()
        self.history: list[dict] = []
        # for TV screens: what is playing (with its start time) and how the last one ended
        self.seq = 0
        self.playing: dict | None = None
        self.last: dict | None = None
        self.screens_listening = lambda: False   # set by the control panel


class AzanApp:
    def __init__(self, config_path: str | Path, cfg: dict | None = None, require_targets: bool = True,
                 shared: Shared | None = None):
        self.config_path = Path(config_path).resolve()
        self.base = self.config_path.parent
        self.cfg = cfg if cfg is not None else load_config(self.config_path)
        loc, calc = self.cfg.get("location", {}), self.cfg.get("calculation", {})
        timetable = calc.get("timetable")
        self.settings = Settings(
            latitude=float(loc["latitude"]), longitude=float(loc["longitude"]),
            timezone=loc.get("timezone", "local"), elevation=float(loc.get("elevation", 0) or 0),
            method=calc.get("method", "MWL"), fajr_angle=calc.get("fajr_angle"),
            isha_angle=calc.get("isha_angle"), isha_minutes=calc.get("isha_minutes"),
            maghrib_angle=calc.get("maghrib_angle"), maghrib_minutes=float(calc.get("maghrib_minutes", 0) or 0),
            asr=calc.get("asr", "standard"), high_latitude=calc.get("high_latitude", "angle"),
            shafaq=calc.get("shafaq", "general"),
            offsets=self.cfg.get("offsets", {}),
            timetable=str((self.base / timetable).resolve()) if timetable else None,
        )
        if not -90 <= self.settings.latitude <= 90 or not -180 <= self.settings.longitude <= 180:
            raise ValueError("Latitude must be -90..90 and longitude -180..180")
        self.times = PrayerTimes(self.settings)
        self.targets = build_targets(self.cfg, require=require_targets)

        a = self.cfg.get("audio", {})
        vol, lead = float(a.get("volume_db", 0) or 0), int(a.get("lead_in_ms", 400) or 0)
        mx = a.get("max_seconds") or None
        mk = lambda src, m=None: Clip(src, self.base, vol, lead, m) if src else None  # noqa: E731
        self.default_clip = mk(a.get("azan") or "tone", mx)
        self.clips = {p: mk(a.get(p), mx) for p in ADHAN_PRAYERS if a.get(p)}
        self.reminder_clip = mk(a.get("reminder"))
        self.jumuah_clip = mk(a.get("jumuah"), mx)

        p = self.cfg.get("prayers", {})
        self.enabled = [x.lower() for x in p.get("enabled", ADHAN_PRAYERS)]
        self.reminder_minutes = int(p.get("reminder_minutes", 0) or 0)
        self.skip_days = [d.lower()[:3] for d in p.get("skip_days", [])]
        self.jumuah_time = p.get("jumuah_time") or None  # e.g. "13:15" replaces dhuhr on Fridays
        if self.jumuah_time:
            hh, mm = (int(x) for x in self.jumuah_time.split(":"))
            if not (0 <= hh < 24 and 0 <= mm < 60):
                raise ValueError("Jumu'ah time must be HH:MM")

        # Ramadan: dates, extra sounds and the Isha time during the month
        self.ramadan = ramadan.Ramadan(self.cfg.get("ramadan"))
        r = self.ramadan.s
        for k in ("qiyam_time", "isha_time", "eid_prayer1", "eid_prayer2"):
            if r.get(k) and not ramadan.parse_time(str(r[k])):
                raise ValueError(f"Ramadan: {k.replace('_', ' ')} must be HH:MM")
        self.ramadan_clips = {}
        for name, key in (("suhoor", "suhoor"), ("wakeup", "wakeup"), ("iftar_reminder", "before_iftar"),
                          ("taraweeh", "taraweeh"), ("qiyam", "qiyam")):
            if r.get(f"{key}_on") and r.get(f"{key}_sound"):
                self.ramadan_clips[name] = mk(r[f"{key}_sound"])
        if r.get("takbeer_on") and r.get("takbeer_sound") and r.get("eid_prayer1"):
            self.ramadan_clips["takbeer"] = Clip(r["takbeer_sound"], self.base, vol, lead,
                                                 repeat_seconds=max(1, self.ramadan.num("takbeer_minutes")) * 60)
        self.iftar_clip = mk(r["iftar_sound"], mx) if r.get("iftar_on") and r.get("iftar_sound") else None

        self.sh = shared or Shared()
        self._busy, self._halt = self.sh.busy, self.sh.halt
        self.stop = threading.Event()

    now_playing = property(lambda self: self.sh.now_playing, lambda self, v: setattr(self.sh, "now_playing", v))
    fired = property(lambda self: self.sh.fired, lambda self, v: setattr(self.sh, "fired", v))
    history = property(lambda self: self.sh.history, lambda self, v: setattr(self.sh, "history", v))

    # ------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return self.times.now()

    def clip_for(self, prayer: str, when: date | None = None) -> Clip:
        if prayer == "dhuhr" and when and when.weekday() == 4 and self.jumuah_clip:
            return self.jumuah_clip
        if prayer == "maghrib" and when and self.iftar_clip and self.ramadan.fasting(when):
            return self.iftar_clip
        return self.clips.get(prayer) or self.default_clip

    def all_clips(self) -> list[Clip]:
        return [c for c in [self.default_clip, self.reminder_clip, self.jumuah_clip, self.iftar_clip,
                            *self.clips.values(), *self.ramadan_clips.values()] if c]

    def day_times(self, day: date) -> dict[str, datetime]:
        """The prayer times for a day, with the Ramadan Isha time when one is set."""
        times = self.times.for_date(day)
        r = self.ramadan
        if r.s["isha_mode"] != "calc" and r.fasting(day):
            if r.s["isha_mode"] == "fixed" and ramadan.parse_time(str(r.s["isha_time"])):
                hh, mm = ramadan.parse_time(str(r.s["isha_time"]))
                times["isha"] = self.times.localize(datetime.combine(day, dtime(hh, mm)))
            elif r.s["isha_mode"] == "after":
                times["isha"] = times["maghrib"] + timedelta(minutes=r.num("isha_after"))
        return times

    def sehri(self, day: date) -> datetime:
        return self.times.for_date(day)["fajr"] - timedelta(minutes=self.ramadan.num("suhoor_minutes"))

    def check(self):
        for c in self.all_clips():
            c.check()

    def events_for(self, day: date) -> list[tuple[datetime, str, str]]:
        """[(when, prayer, kind)] where kind is 'azan', 'reminder' or 'ramadan' (prayer is then the
        Ramadan sound: suhoor, wakeup, iftar_reminder, taraweeh, qiyam or takbeer)."""
        if day.strftime("%a").lower()[:3] in self.skip_days:
            return []
        times = self.day_times(day)
        if self.jumuah_time and day.weekday() == 4:
            hh, mm = (int(x) for x in self.jumuah_time.split(":"))
            times["dhuhr"] = self.times.localize(datetime.combine(day, dtime(hh, mm)))
        ev = []
        for p in ADHAN_PRAYERS:
            if p not in self.enabled:
                continue
            ev.append((times[p], p, "azan"))
            if self.reminder_minutes and self.reminder_clip:
                ev.append((times[p] - timedelta(minutes=self.reminder_minutes), p, "reminder"))
        ev += self.ramadan_events(day, times)
        return sorted(ev)

    def ramadan_events(self, day: date, times: dict) -> list[tuple[datetime, str, str]]:
        r, clips, ev = self.ramadan, self.ramadan_clips, []
        if r.fasting(day):
            mins = lambda key: timedelta(minutes=r.num(key))  # noqa: E731
            if "suhoor" in clips:
                ev.append((times["fajr"] - mins("suhoor_minutes"), "suhoor", "ramadan"))
            if "wakeup" in clips:
                ev.append((times["fajr"] - mins("wakeup_minutes"), "wakeup", "ramadan"))
            if "iftar_reminder" in clips:
                ev.append((times["maghrib"] - mins("before_iftar_minutes"), "iftar_reminder", "ramadan"))
            if "taraweeh" in clips:
                ev.append((times["isha"] + mins("taraweeh_minutes"), "taraweeh", "ramadan"))
            k = r.day_number(day)
            hm = ramadan.parse_time(str(r.s["qiyam_time"]))
            if "qiyam" in clips and hm and k and k >= 21:   # the last ten nights (the night before day 21 onwards)
                t = self.times.localize(datetime.combine(day, dtime(*hm)))
                if t < times["fajr"]:
                    ev.append((t, "qiyam", "ramadan"))
        if "takbeer" in clips and r.eid_day(day):
            hm = ramadan.parse_time(str(r.s["eid_prayer1"]))
            if hm:
                t = self.times.localize(datetime.combine(day, dtime(*hm))) - timedelta(minutes=r.num("takbeer_minutes"))
                ev.append((t, "takbeer", "ramadan"))
        return ev

    def event_clip(self, when: datetime, prayer: str, kind: str) -> Clip | None:
        if kind == "ramadan":
            return self.ramadan_clips.get(prayer)
        return self.reminder_clip if kind == "reminder" else self.clip_for(prayer, when.date())

    def next_event(self) -> tuple[datetime, str, str] | None:
        now = self.now()
        for d in range(0, 8):
            for e in self.events_for(now.date() + timedelta(days=d)):
                if e[0] > now:
                    return e
        return None

    def broadcast(self, clip: Clip, label: str = "", targets: list | None = None,
                  screens: bool | None = None) -> bool:
        """Play a clip on the phones (all of them unless targets is given) and, when screens is
        true (the default when playing to all phones), on the TV screens too."""
        screens = targets is None if screens is None else screens
        targets = self.targets if targets is None else targets
        on_screens = screens and self.sh.screens_listening()
        if not targets and not on_screens:
            log.warning("No phones/targets are enabled; nothing to play for %s", label)
            return False
        if not self._busy.acquire(blocking=False):
            log.warning("Already playing; skipping %s", label)
            return False
        self._halt.clear()
        self.now_playing = label or "audio"
        self.sh.seq += 1
        seq = self.sh.seq
        ok = False
        try:
            for codec in {"PCMU"} | {getattr(t, "codec", "PCMU") for t in targets}:
                clip.get(codec)   # prepare the audio first, so the phones and screens start together
            self.sh.playing = {"seq": seq, "label": label or "audio", "clip": clip, "started": time.time(),
                               "screens": bool(screens)}
            where = f"{len(targets)} target(s)" + (" and the TV screens" if on_screens else "")
            log.info("Playing %s (%s) to %s", label or "audio", clip, where if targets else "the TV screens")
            start = time.monotonic()
            if targets:
                with ThreadPoolExecutor(max_workers=len(targets)) as ex:
                    results = list(ex.map(lambda t: _safe_play(t, clip, self._halt), targets))
                ok = any(results) or on_screens
            else:   # only TV screens: they play it themselves, so just wait until it's over
                self._halt.wait(clip.seconds())
                ok = True
            log.info("Finished %s in %.0f s%s", label or "broadcast", time.monotonic() - start,
                     " (stopped)" if self._halt.is_set() else "")
            return ok
        except Exception:
            log.exception("Playing %s failed", label or "audio")
            return False
        finally:
            self.sh.last = {"seq": seq, "stopped": self._halt.is_set()}
            self.sh.playing = None
            self.history = (self.history + [{"label": label, "at": self.now().isoformat(timespec="seconds"),
                                             "ok": ok}])[-20:]
            self.now_playing = None
            self._busy.release()

    def stop_audio(self):
        self._halt.set()

    def shutdown(self, halt_audio: bool = True):
        self.stop.set()
        if halt_audio:
            self._halt.set()

    # ---------------------------------------------------------- scheduler
    def run_forever(self):
        try:
            self.check()
        except FileNotFoundError as exc:
            log.error("%s - that prayer will fall back to nothing until fixed", exc)
        grace = timedelta(minutes=2)
        log.info("Azan scheduler started (%s, %s)", self.settings.method, self.times.tz_name)
        self._log_today()
        last_day = None
        while not self.stop.is_set():
            now = self.now()
            if now.date() != last_day:
                if last_day is not None:
                    self._log_today()
                last_day = now.date()
                self.fired = {f for f in self.fired if f[0] >= now.date() - timedelta(days=1)}
            try:
                upcoming = self.events_for(now.date()) + self.events_for(now.date() + timedelta(days=1))
            except Exception:
                log.exception("Could not work out prayer times")
                self.stop.wait(60)
                continue
            pending = [e for e in upcoming if (e[0].date(), e[1], e[2]) not in self.fired and e[0] + grace > now]
            if not pending:
                self.stop.wait(30)
                continue
            when, prayer, kind = pending[0]
            wait = (when - now).total_seconds()
            if wait > 0:
                self.stop.wait(min(wait, 30))  # re-check regularly (clock changes, DST)
                continue
            self.fired.add((when.date(), prayer, kind))
            clip = self.event_clip(when, prayer, kind)
            label = ramadan.event_label(prayer, kind)
            if clip is None:
                continue
            threading.Thread(target=self.broadcast, args=(clip, label), daemon=True).start()
        log.info("Azan scheduler stopped")

    def _log_today(self):
        today = self.now().date()
        t = self.day_times(today)
        log.info("Today %s: %s", today, "  ".join(f"{k.title()} {v:%H:%M}" for k, v in t.items()))
        if self.ramadan.fasting(today):
            extra = [f"{ramadan.LABELS[n]} {w:%H:%M}" for w, n, k in self.ramadan_events(today, t)]
            k = self.ramadan.day_number(today)
            log.info("Ramadan%s · Sehri ends %s%s", f" day {k}" if k else "", f"{self.sehri(today):%H:%M}",
                     (" · Ramadan sounds today: " + ", ".join(extra)) if extra else "")


def _safe_play(target, clip, stop) -> bool:
    try:
        return target.play(clip, stop)
    except Exception:
        log.exception("[%s] failed", getattr(target, "name", target))
        return False
