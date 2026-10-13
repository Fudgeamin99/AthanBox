"""The azan-sip service with its built-in control panel (served on http://127.0.0.1:8765).

Runs the scheduler in the background and serves a local web page to manage everything:
location, calculation, audio, phones, tests and logs. Only reachable from this computer.

Box mode (`azan-sip run --box`, used on the Azan SIP Box): the panel is also reachable from the
network (port 8765 and port 80, e.g. http://azan.local) and changing anything needs the box's
password. Viewing the prayer times does not.
"""
from __future__ import annotations

import collections
import copy
import http.cookies
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import sys
import threading
import time
import tomllib
import urllib.request
import webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, live, look, rtp, tomlw, updater
from .auth import COOKIE, SESSION_SECONDS, Auth, LoginError
from .env import FROZEN, app_dir
from .sipreg import Registration, RegistrationManager
from .app import ADHAN_PRAYERS, AzanApp, Clip, Shared, build_targets, load_config
from .praytimes import METHODS, PRAYERS, SHAFAQ, get_zone

log = logging.getLogger("azan-sip")

DEFAULT_PORT = 8765
# The version shown in the panel: the Box's own (BOX_CHANGELOG.md), or the desktop app's without it.
VERSION = updater.box_version() or __version__
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".mp4"}
STATIC = Path(__file__).with_name("static")
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".woff2": "font/woff2", ".svg": "image/svg+xml", ".png": "image/png"}
PAGES = {"/setup": "setup.html", "/display": "display.html"}
USB_ROOTS = ("/media", "/run/media")  # where Debian mounts USB sticks

DEFAULT_CONFIG = {
    "general": {"configured": False, "scheduler_enabled": True, "port": DEFAULT_PORT},
    "location": {"name": "London, United Kingdom", "latitude": 51.5074, "longitude": -0.1278,
                 "timezone": "local", "elevation": 0},
    "calculation": {"method": "MWL", "asr": "standard", "high_latitude": "angle"},
    "offsets": {p: 0 for p in ADHAN_PRAYERS},
    "prayers": {"enabled": list(ADHAN_PRAYERS), "reminder_minutes": 0, "skip_days": []},
    "audio": {"azan": "tone", "volume_db": 0, "lead_in_ms": 400},
    "network": {"local_ip": "", "multicast_ttl": 4},
    # Makkah live stream (empty = Saudi Quran TV). direct: screens fetch it themselves; fallback: when a screen
    # can't, it goes through the box (off: the box never relays it, so it uses no data for it)
    "live": {"url": "", "direct": False, "fallback": True},
    "targets": [{"name": "All phones (multicast)", "type": "multicast", "address": "224.5.6.20:10008",
                 "codec": "PCMU", "enabled": True}],
}


# ------------------------------------------------------------------ logging
class RingHandler(logging.Handler):
    def __init__(self, n=500):
        super().__init__()
        self.lines: collections.deque = collections.deque(maxlen=n)
        self.counter = 0

    def emit(self, record):
        try:
            self.counter += 1
            self.lines.append({"id": self.counter, "t": time.strftime("%Y-%m-%d %H:%M:%S",
                                                                      time.localtime(record.created)),
                               "level": record.levelname, "msg": record.getMessage()})
        except Exception:  # never let logging break the app
            pass


RING = RingHandler()


def setup_logging(base: Path, verbose: bool = False):
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in list(root.handlers):
        root.removeHandler(h)
    RING.setLevel(logging.INFO)
    root.addHandler(RING)
    try:
        fh = logging.handlers.RotatingFileHandler(base / "azan.log", maxBytes=1_000_000, backupCount=3,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    if sys.stderr:  # not present under pythonw
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


# ------------------------------------------------------------------ helpers
def local_ips() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.add(rtp.local_ip_for())
    return sorted(ip for ip in ips if not ip.startswith("127."))


def box_ip() -> str:
    """The address phones and PCs reach this box on (the one with the default route)."""
    ip = rtp.local_ip_for()
    return ip if not ip.startswith("127.") else (local_ips() or ["127.0.0.1"])[0]


def qr_svg(text: str) -> bytes:
    from .qrcodegen import QrCode
    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    n, b = qr.get_size(), 2
    d = "".join(f"M{x + b},{y + b}h1v1h-1z" for y in range(n) for x in range(n) if qr.get_module(x, y))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n + 2 * b} {n + 2 * b}" '
            f'shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#fff"/>'
            f'<path d="{d}" fill="#1d2622"/></svg>').encode()


def find_usb_audio(roots=USB_ROOTS, max_depth: int = 4, limit: int = 20) -> list[Path]:
    """Audio files on mounted USB sticks, leaving out hidden folders and files over 100 MB."""
    found: list[Path] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("/").count("/")
        for folder, dirs, files in os.walk(root):
            depth = folder.rstrip("/").count("/") - base_depth
            dirs[:] = sorted(d for d in dirs if not d.startswith(".")) if depth < max_depth else []
            for f in sorted(files):
                path = Path(folder) / f
                if not f.startswith(".") and path.suffix.lower() in AUDIO_EXT:
                    try:
                        if 0 < path.stat().st_size <= 100_000_000:
                            found.append(path)
                    except OSError:
                        pass
                if len(found) >= limit:
                    return found
    return found


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def clean_config(cfg: dict) -> dict:
    """Drop empty optional values so the TOML stays tidy."""
    out = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            out[k] = {kk: vv for kk, vv in v.items() if vv is not None and vv != ""
                      or kk in ("local_ip", "timezone")}
        elif isinstance(v, list):
            out[k] = [{kk: vv for kk, vv in t.items() if vv is not None and vv != ""} if isinstance(t, dict)
                      else t for t in v]
        else:
            out[k] = v
    return out


def windows_startup_path() -> Path | None:
    if os.name != "nt" or not os.environ.get("APPDATA"):
        return None
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/Azan SIP.vbs"


def set_windows_startup(enabled: bool, base: Path):
    path = windows_startup_path()
    if path is None:
        raise RuntimeError("Start with Windows is only available on Windows")
    if not enabled:
        path.unlink(missing_ok=True)
        return
    q = lambda s: str(s).replace('"', '""')  # noqa: E731
    if FROZEN:  # AzanSIP.exe
        cmd = f'"""{q(Path(sys.executable).resolve())}"" --background"'
    else:       # Python + "Azan SIP.pyw"
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        exe = pyw if pyw.exists() else exe
        cmd = f'"""{q(exe)}"" ""{q(base / "Azan SIP.pyw")}"" --background"'
    path.write_text(
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.CurrentDirectory = "{q(base)}"\r\n'
        f'sh.Run {cmd}, 0, False\r\n', encoding="utf-8")


def message_box(text: str, title: str = "Azan SIP"):
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
            return
        except Exception:
            pass
    if sys.stderr:
        print(text, file=sys.stderr)


# ------------------------------------------------------------------ service
class Service:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path).resolve()
        self.base = self.config_path.parent
        self.audio_dir = self.base / "audio"
        self.lock = threading.RLock()
        self.app: AzanApp | None = None
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.cfg: dict = {}
        self.server: ThreadingHTTPServer | None = None
        self.shared = Shared()
        self.regs = RegistrationManager()
        self.update_info: dict | None = None
        self.update_lock = threading.Lock()
        self.restarting = False
        self.started = time.time()
        self.box = False
        self.auth = Auth(self.base / "auth")
        self.extra_servers: list[ThreadingHTTPServer] = []
        self.wizard = {"rev": 0, "state": None, "driver": ""}  # first-time setup, shared by the screen and phones
        self.live = live.LiveRelay(lambda: self.live_url)
        self.load()

    # -- config
    def load(self):
        if not self.config_path.exists():
            log.info("No config yet - creating %s with default settings", self.config_path.name)
            self.write(DEFAULT_CONFIG)
            cfg = copy.deepcopy(DEFAULT_CONFIG)
        else:
            cfg = load_config(self.config_path)
            cfg.setdefault("general", {}).setdefault("configured", True)
        try:
            self.apply(cfg, save=False)
        except Exception as exc:  # keep the panel usable so the user can fix it
            self.error = f"Settings problem: {exc}"
            self.cfg = cfg
            log.error(self.error)

    def write(self, cfg: dict):
        text = tomlw.dumps(clean_config(cfg))
        tomllib.loads(text)  # round-trip check before replacing the file
        if self.config_path.exists():
            shutil.copyfile(self.config_path, self.config_path.with_suffix(".toml.bak"))
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.config_path)

    def apply(self, cfg: dict, save: bool = True):
        live.check_url((cfg.get("live") or {}).get("url", ""))   # raises with a friendly message
        look.check(cfg)
        with self.lock:
            app = AzanApp(self.config_path, cfg, require_targets=False, shared=self.shared)  # raises if invalid
            if save:
                self.write(cfg)
            self._stop_scheduler(halt_audio=False)  # an azan that is playing carries on
            self.app, self.cfg, self.error = app, cfg, None
            self.regs.sync(cfg)  # stay registered on the phone system (shows as Idle)
            g = cfg.get("general", {})
            if g.get("scheduler_enabled", True) and g.get("configured", True):
                self.thread = threading.Thread(target=app.run_forever, name="scheduler", daemon=True)
                self.thread.start()

    @property
    def live_url(self) -> str:
        return ((self.cfg.get("live") or {}).get("url") or "").strip() or live.DEFAULT_URL

    def live_screen(self) -> dict:
        """Where screens get Makkah live: through the box, or (direct) from the stream itself, so the box
        uses no data for it. The link is only shared with screens when they need it."""
        lv = self.cfg.get("live") or {}
        if lv.get("direct") is True:
            return {"direct": True, "url": self.live_url, "fallback": lv.get("fallback") is not False}
        return {"direct": False}

    @property
    def relay_off(self) -> bool:
        """Screens must fetch Makkah live themselves: the box doesn't relay it at all."""
        info = self.live_screen()
        return info["direct"] and not info["fallback"]

    def configured(self) -> bool:
        return bool(self.cfg.get("general", {}).get("configured", True))

    def setup_manual(self) -> bool:
        """First-time setup is being done in the control panel instead of the setup guide."""
        return not self.configured() and bool((self.wizard.get("state") or {}).get("manual"))

    def start_box(self):
        """Box mode: until first-time setup is done, a one-time code (QR on the screen) lets a phone in."""
        self.box = True
        if not self.configured():
            self.auth.new_setup_code()
        if not self.auth.has_password():
            log.warning("No password is set on this box - set one with: python -m azan_sip box-password --factory")
        if updater.DOCKER:
            updater.ensure_helper_token(self.base)

    def factory_reset(self):
        """Back to how the box left the factory: settings, recordings, timetable and own password removed."""
        with self.lock:
            if self.app and self.app.now_playing:
                self.app.stop_audio()
            self._stop_scheduler()
            self.regs.stop_all()
            for name in ("config.toml", "config.toml.bak", "config.tmp", "timetable.csv"):
                (self.base / name).unlink(missing_ok=True)
            if self.audio_dir.is_dir():
                for f in self.audio_dir.iterdir():
                    if f.is_file() and f.name != ".gitkeep":
                        f.unlink(missing_ok=True)
            self.auth.reset_to_factory()
            self.wizard = {"rev": self.wizard["rev"] + 1, "state": None, "driver": ""}
            self.app, self.cfg, self.error = None, {}, None
            self.load()
            if self.box:
                self.auth.new_setup_code()
        log.warning("Factory reset done - settings, recordings and the owner's password were removed")

    def _stop_scheduler(self, halt_audio: bool = True):
        if self.app:
            self.app.shutdown(halt_audio)
        if self.thread:
            self.thread.join(timeout=3)
        self.thread = None

    def shutdown(self):
        self._stop_scheduler()
        self.regs.stop_all()

    # -- state for the panel
    def state(self) -> dict:
        with self.lock:
            app = self.app
            ui = self.update_info or {}
            st = {"version": VERSION, "error": self.error, "restarting": self.restarting,
                  "update": {"available": bool(ui.get("available")), "latest_version": ui.get("latest_version")}, "running": bool(self.thread and self.thread.is_alive()),
                  "configured": self.cfg.get("general", {}).get("configured", True),
                  "setup_manual": self.setup_manual(),
                  "look": look.display(self.cfg),  # theme and layout of the prayer-times screen
                  "live": self.live_screen(),
                  "location": self.cfg.get("location", {}).get("name") or "", "log": list(RING.lines)[-200:],
                  "log_id": RING.counter, "registrations": self.regs.info()}
            if app is None:
                return st
            now = app.now()
            nxt = app.next_event()
            next_key = (nxt[0].date(), nxt[1]) if nxt and nxt[2] == "azan" else None
            rows = self.rows_for(app, now.date(), now, next_key)
            # After the last azan of the day the prayer-times screen shows tomorrow's times
            na = self.next_info(app, "azan")
            if na and na["day"] == "tomorrow":
                day = now.date() + timedelta(days=1)
                st["tomorrow"] = {"label": f"Tomorrow · {day:%A} {day.day} {day:%B}",
                                  "rows": self.rows_for(app, day, now, (day, na["prayer"]))}
            st.update({
                "now": now.isoformat(timespec="seconds"), "date": f"{now:%A %d %B %Y}",
                "clock": f"{now:%H:%M:%S}", "timezone": app.times.tz_name,
                "method": METHODS.get(app.settings.method, {}).get("name", app.settings.method),
                "asr": app.settings.asr, "today": rows, "playing": app.now_playing,
                "skip_today": now.strftime("%a").lower()[:3] in app.skip_days,
                "next": self.next_info(app), "next_azan": self.next_info(app, "azan"),
                "history": app.history[::-1],
                "warnings": self.warnings(app),
                "targets": len(app.targets),
            })
            return st

    # -- updates
    def check_updates(self) -> dict:
        info = updater.check(self.base)
        self.update_info = info
        if info.get("available"):
            log.info("Update available: version %s (%s)", info.get("latest_version"), info.get("latest_message", ""))
        return info

    def update_status(self) -> dict:
        info = dict(self.update_info or {"current_version": VERSION, "repo": updater.REPO_URL,
                                         "current_commit": updater._installed(self.base).get("commit")})
        info["backups"] = updater.backups(self.base)
        info["mode"] = updater.mode(self.base)
        info["auto"] = self.cfg.get("general", {}).get("check_updates", True)
        if info["mode"] == "docker":
            h = updater.docker_helper(self.base)
            info["helper"] = bool(h and h.get("ok"))
            if info["helper"]:
                info["current_version"] = h.get("current") or info.get("current_version")
                if h.get("last_error"):
                    info["error"] = f"The last update didn't work, so the old version was put back: {h['last_error']}"
            else:
                info["note"] = updater.DOCKER_NOTE
                if h and h.get("error"):
                    info["error"] = h["error"]
        return info

    def install(self, action: str, name: str | None = None) -> dict:
        if not self.update_lock.acquire(blocking=False):
            raise RuntimeError("An update is already running")
        try:
            if self.app and self.app.now_playing:
                raise RuntimeError(f"Please wait until '{self.app.now_playing}' has finished playing")
            res = updater.apply(self.base) if action == "apply" else updater.rollback(self.base, name)
        finally:
            self.update_lock.release()
        if not res.get("docker"):  # in Docker the updater container replaces this one
            self.restart()
        return res

    def restart(self):
        """Start a fresh copy of the app (which waits for our port) and stop this one."""
        self.restarting = True
        spawned = updater.spawn_replacement(self.base)
        log.info("Restarting Azan SIP%s", "" if spawned else " (service manager will start it again)")
        threading.Timer(1.0, self.server.shutdown).start()

    def auto_update_checker(self):
        time.sleep(90)
        while True:
            if self.cfg.get("general", {}).get("check_updates", True) and not self.restarting:
                try:
                    self.check_updates()
                except Exception as exc:
                    log.debug("Automatic update check failed: %s", exc)
            time.sleep(24 * 3600)

    def warnings(self, app: AzanApp) -> list[str]:
        w = []
        if not self.cfg.get("general", {}).get("configured", True):
            w.append("Welcome! Check your location, calculation method and phones, then press Save.")
        if not app.targets:
            w.append("No phones are switched on - add or enable one on the Phones page.")
        for c in app.all_clips():
            if c.path is not None and not c.path.is_file():
                w.append(f"Audio file missing: {c.path.name} - choose another on the Audio page.")
            elif c.path is not None and c.path.suffix.lower() != ".wav" and not shutil.which("ffmpeg"):
                w.append(f"{c.path.name} is not a WAV file and ffmpeg isn't installed. "
                         "Re-add it with the Upload button (it is converted automatically).")
        for r in self.regs.info():
            if r["status"] == "failed":
                w.append(f"Extension {r['extension']} is not registered on the phone system: {r['detail']}")
        if (self.update_info or {}).get("available"):
            w.append(f"A new version of Azan SIP ({self.update_info.get('latest_version')}) is available - "
                     "install it from Settings > Updates.")
        tzname = self.cfg.get("location", {}).get("timezone", "local")
        if app.times.tz is None and str(tzname).lower() not in ("", "local", "system"):
            w.append(f"Time zone '{tzname}' isn't available on this PC, so the PC's own time zone is used.")
        return w

    def meta(self) -> dict:
        try:
            import zoneinfo
            zones = sorted(zoneinfo.available_timezones())
        except Exception:
            zones = []
        files = sorted(p.name for p in self.audio_dir.glob("*") if p.suffix.lower() in AUDIO_EXT) \
            if self.audio_dir.is_dir() else []
        sp = windows_startup_path()
        def detail(v):
            if v.get("seasonal"):
                return ("Fajr and Isha follow the committee's seasonal curve for your latitude (18° limit). "
                        "Includes the method's Dhuhr +5 and Maghrib +3 minute margins - set the offsets "
                        "below to -5 / -3 to match apps that leave them out.")
            return f"Fajr {v['fajr']}°, " + (f"Isha {v['isha']}°" if "isha" in v
                                             else f"Isha {v['isha_minutes']} min after Maghrib")
        return {"methods": [{"id": k, "name": v["name"], "detail": detail(v), "seasonal": bool(v.get("seasonal"))}
                            for k, v in METHODS.items()],
                "shafaq": SHAFAQ,
                "zones": zones, "audio_files": files, "ips": local_ips(),
                "windows": os.name == "nt", "startup": bool(sp and sp.exists()),
                "ffmpeg": bool(shutil.which("ffmpeg")), "config_path": str(self.config_path),
                "live_default": live.DEFAULT_URL,
                "folder": str(self.base)}

    @staticmethod
    def rows_for(app: AzanApp, day, now, next_key: tuple | None) -> list[dict]:
        """One day's times for the cards: played / passed / next (next_key is (date, prayer))."""
        times = app.times.for_date(day)
        azan_times = {p: w for w, p, k in app.events_for(day) if k == "azan"}
        rows = []
        for name in PRAYERS:
            t = azan_times.get(name, times[name])  # includes the Jumu'ah time on Fridays
            status = ""
            if name in ADHAN_PRAYERS:
                if (t.date(), name, "azan") in app.fired:
                    status = "played"
                elif t <= now:
                    status = "passed"
            if next_key == (t.date(), name):
                status = "next"
            rows.append({"name": name, "time": f"{t:%H:%M}", "azan": name in ADHAN_PRAYERS,
                         "enabled": name in app.enabled, "status": status,
                         "jumuah": name == "dhuhr" and day.weekday() == 4 and bool(app.jumuah_time)})
        return rows

    @staticmethod
    def next_info(app: AzanApp, kind: str | None = None) -> dict | None:
        """The next event (azan or reminder), or the next one of `kind`."""
        now = app.now()
        nxt = next((e for d in range(8) for e in app.events_for(now.date() + timedelta(days=d))
                    if e[0] > now and kind in (None, e[2])), None)
        if not nxt:
            return None
        return {"prayer": nxt[1], "kind": nxt[2], "time": f"{nxt[0]:%H:%M}",
                "day": "today" if nxt[0].date() == now.date() else
                ("tomorrow" if nxt[0].date() == now.date() + timedelta(days=1) else f"{nxt[0]:%A}"),
                "seconds": int((nxt[0] - now).total_seconds())}

    def preview(self, cfg: dict, days: int = 7, start: str | None = None) -> dict:
        app = AzanApp(self.config_path, cfg, require_targets=False)
        first = date.fromisoformat(start) if start else app.now().date()
        rows = []
        for i in range(max(1, min(int(days), 400))):
            d = first + timedelta(days=i)
            if start and d.year != first.year:
                break
            t = app.times.for_date(d)
            rows.append({"date": f"{d:%a %d %b}", "iso": d.isoformat(), **{k: f"{v:%H:%M}" for k, v in t.items()}})
        return {"week": rows, "timezone": app.times.tz_name, "next": self.next_info(app, "azan")}

    def test(self, req: dict) -> dict:
        app = self.app
        if app is None:
            raise RuntimeError(self.error or "Settings not loaded")
        a = self.cfg.get("audio", {})
        vol, lead = float(a.get("volume_db", 0) or 0), int(a.get("lead_in_ms", 400) or 0)
        g = self.cfg.get("general", {})
        clamp = lambda v, lo, hi, d: max(lo, min(hi, float(v if v not in (None, "") else d)))  # noqa: E731
        chime_s = clamp(g.get("test_chime_seconds"), 2, 120, 6)
        chime_db = clamp(g.get("test_chime_volume_db"), -12, 12, 0)
        src = req.get("source", "tone")
        if req.get("seconds") == "default":  # Test buttons: use the length chosen in Settings (0 = whole recording)
            req["seconds"] = clamp(g.get("test_azan_seconds"), 0, 3600, 20) or None
        if src == "tone":
            clip = Clip("tone", app.base, chime_db, lead, tone_seconds=chime_s)
        elif src in ADHAN_PRAYERS:
            clip = app.clip_for(src)
        elif src == "reminder":
            clip = app.reminder_clip or Clip("tone", app.base, vol, lead)
        elif src == "jumuah":
            clip = app.jumuah_clip or app.default_clip
        elif src.startswith("file:"):
            clip = Clip("audio/" + Path(src[5:]).name, app.base, vol, lead)
        else:
            clip = app.default_clip
        if req.get("seconds") and src != "tone":
            clip = Clip(clip.source, app.base, vol, lead, float(req["seconds"]))
        clip.check()
        targets = None
        if req.get("target") is not None:  # test one phone entry (possibly unsaved)
            t = dict(req["target"], enabled=True)
            targets = build_targets({"network": self.cfg.get("network", {}), "targets": [t]})
        if app.now_playing:
            raise RuntimeError(f"Already playing: {app.now_playing}")
        if req.get("wait"):  # play it now and say whether it worked (setup wizard)
            first = RING.counter
            if not app.broadcast(clip, req.get("label") or "Test", targets):
                problems = [x["msg"] for x in list(RING.lines) if x["id"] > first and x["level"] in ("WARNING", "ERROR")]
                raise RuntimeError(problems[-1] if problems else "the test chime didn't play")
            return {"ok": True}
        threading.Thread(target=app.broadcast, args=(clip, req.get("label") or "Test", targets),
                         daemon=True).start()
        return {"ok": True}

    # -- first-time setup wizard (setup.html)
    def set_wizard(self, state, driver: str) -> int:
        with self.lock:
            self.wizard = {"rev": self.wizard["rev"] + 1, "state": state, "driver": driver}
            return self.wizard["rev"]

    def setup_usb(self) -> dict:
        """Copy azan recordings from a USB stick plugged into the box."""
        if not self.box or updater.DOCKER:
            return {"available": False, "found": [], "copied": []}
        found, copied = find_usb_audio(), []
        if found:
            self.audio_dir.mkdir(exist_ok=True)
        for src in found:
            name = re.sub(r"[^\w\-. ()]+", "_", src.name).strip() or "azan.mp3"
            dest = self.audio_dir / name
            try:
                if not (dest.is_file() and dest.stat().st_size == src.stat().st_size):
                    shutil.copyfile(src, dest)
                    log.info("Copied %s from the USB stick", name)
                copied.append(name)
            except OSError as exc:
                log.warning("Couldn't copy %s from the USB stick: %s", src, exc)
        return {"available": True, "found": [str(f) for f in found], "copied": copied,
                "files": self.meta()["audio_files"]}

    def setup_connect(self, target: dict) -> dict:
        """Sign in to the phone system as the extension (if it has one), then play the test chime."""
        from .sip import parse_uri
        t = dict(target, enabled=True)
        info = None
        if t.get("type") == "sip" and t.get("username") and t.get("register", True):
            _, host, port = parse_uri(t.get("uri", ""))
            same = (host, port, t["username"], t.get("password", ""), t.get("auth_username") or t["username"])
            with self.regs.lock:
                live = [r for r in self.regs.regs.values() if r.status == "registered"
                        and (r.host, r.port, r.username, r.password, r.auth_username) == same]
            if live:  # already signed in as this extension (running the guide again): don't sign in twice
                info = live[0].info()
        if info is None and t.get("type") == "sip" and t.get("username") and t.get("register", True):
            reg = Registration(t.get("name") or "Phone system", host, port, t["username"], t.get("password", ""),
                               t.get("auth_username", ""), self.cfg.get("network", {}).get("local_ip") or None)
            reg.start()
            deadline = time.monotonic() + 12
            while reg.status == "starting" and time.monotonic() < deadline:
                time.sleep(0.2)
            info = reg.info()
            reg.stop()  # signs out again; Finish keeps it signed in for good
            if info["status"] != "registered":
                return {"ok": False, "registration": info,
                        "error": info["detail"] or f"No reply from {host} - check the address"}
        try:
            self.test({"source": "tone", "target": t, "label": "Test chime", "wait": True})
        except RuntimeError as exc:
            return {"ok": False, "registration": info, "error": (f"Signed in, but the test call didn't work: {exc}"
                                                                 if info else str(exc))}
        return {"ok": True, "registration": info}


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    service: Service = None  # set by serve()
    server_version = "azan-sip"

    def log_message(self, fmt, *args):  # quiet
        pass

    # -- security: local panel only, no cross-site requests
    def _host_ok(self) -> bool:
        raw = (self.headers.get("Host") or "").strip().lower()
        host = raw[1:raw.index("]")] if raw.startswith("[") and "]" in raw else raw.split(":")[0]
        if host in ("127.0.0.1", "localhost", "::1"):
            return True
        if not self.service.box:
            return False
        # Box mode: only names that can't point a web page somewhere else (DNS rebinding):
        # the box's IP addresses, and .local names (mDNS, only answered on this network).
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        return host.endswith(".local") or host == socket.gethostname().lower()

    def _token(self) -> str | None:
        try:
            c = http.cookies.SimpleCookie(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return None
        return c[COOKIE].value if COOKIE in c else None

    def _authed(self) -> bool:
        return not self.service.box or self.service.auth.valid(self._token())

    def _client_local(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _setup_keep(self) -> str | None:
        """Whose setup-code session survives the end of setup: a phone or PC, never the box's own screen."""
        return None if self._client_local() else self._token()

    def _need_login(self):
        return self._json({"ok": False, "login": True, "error": "Please enter the password to change settings"}, 401)

    def _session_info(self) -> dict:
        svc = self.service
        return {"box": svc.box, "logged_in": self._authed(), "has_password": svc.auth.has_password() if svc.box else False,
                "custom_password": svc.auth.custom_password() if svc.box else False,
                "configured": svc.configured(), "setup": bool(svc.box and svc.auth.setup_code),
                "local": self._client_local(), "docker": updater.DOCKER, "ip": box_ip(), "version": VERSION,
                "panel": look.panel(svc.cfg)}

    def _setup_urls(self) -> list[str]:
        """Links for the setup QR code: the address this page was opened on first, then the box's own."""
        svc = self.service
        hostport = "" if svc.extra_servers else f":{svc.server.server_address[1]}"
        raw = (self.headers.get("Host") or "").split(":")[0]
        ips = [raw] if raw and raw != "127.0.0.1" and re.fullmatch(r"[\d.]+", raw) else []
        return [f"http://{ip}{hostport}/setup?setup={svc.auth.setup_code}"
                for ip in dict.fromkeys(ips + [box_ip(), *local_ips()])]

    def _may_see_setup_code(self) -> bool:
        """The box's own screen, or someone already signed in (to carry on from their phone)."""
        svc = self.service
        return bool(svc.box and svc.auth.setup_code and (self._client_local() or self._authed()))

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None,
              cache: bool = False, frame: str = "DENY"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", frame)  # the Display page previews the prayer-times screen in a frame
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _body(self, limit=1_000_000) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("Upload too large")
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"Forbidden", "text/plain")
        svc = self.service
        url = urlparse(self.path)
        p = url.path
        try:
            if p in ("/", "/index.html"):
                if svc.box and not svc.configured() and not svc.setup_manual():  # first-time setup (keeps ?setup=)
                    return self._send(302, b"", "text/plain", {"Location": "/setup" + (f"?{url.query}" if url.query else "")})
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if p in PAGES:
                if p == "/setup" and not (svc.box and (not svc.configured() or "again" in parse_qs(url.query, keep_blank_values=True))):
                    return self._send(302, b"", "text/plain", {"Location": "/display" if self._client_local() else "/"})
                return self._send(200, (STATIC / PAGES[p]).read_bytes(), "text/html; charset=utf-8",
                                  frame="SAMEORIGIN" if p == "/display" else "DENY")
            if p.startswith("/static/"):
                f = STATIC / Path(unquote(p[8:])).name
                if f.suffix.lower() in STATIC_TYPES and f.is_file():
                    return self._send(200, f.read_bytes(), STATIC_TYPES[f.suffix.lower()], cache=True)
                return self._send(404, b"Not found", "text/plain")
            if p == "/api/ping":
                return self._json({"app": "azan-sip", "version": __version__})
            if p == "/api/session":
                return self._json(self._session_info())
            if p == "/api/state":
                st = svc.state()
                if not self._authed():  # prayer times are public, the log is not
                    st.update(log=[], history=[])
                return self._json(st)
            if p in ("/live/stream.m3u8", "/live/x") and svc.relay_off:
                return self._json({"ok": False, "error": "Makkah live through the box is turned off on the Display page"}, 403)
            if p == "/live/stream.m3u8":  # Makkah live, relayed so any screen can play it
                try:
                    ctype, body = svc.live.entry(self.client_address[0])
                except Exception as exc:
                    return self._json({"ok": False, "error": live.friendly(exc)}, 502)
                return self._send(200, body, ctype)
            if p == "/live/x":  # only links the relay signed itself
                q = parse_qs(url.query)
                target, sig = q.get("u", [""])[0], q.get("k", [""])[0]
                if not svc.live.allowed(target, sig):
                    return self._send(403, b"Forbidden", "text/plain")
                try:
                    ctype, body = svc.live.get(target)
                except Exception as exc:
                    return self._json({"ok": False, "error": live.friendly(exc)}, 502)
                return self._send(200, body, ctype)
            if p == "/api/setup-code":  # for the setup QR code (box's own screen, or signed in)
                if not self._may_see_setup_code():
                    return self._send(404, b"Not found", "text/plain")
                return self._json({"code": svc.auth.setup_code, "urls": self._setup_urls()})
            if p == "/api/setup-qr.svg":
                if not self._may_see_setup_code():
                    return self._send(404, b"Not found", "text/plain")
                return self._send(200, qr_svg(self._setup_urls()[0]), "image/svg+xml")
            if not self._authed():
                return self._need_login()
            if p == "/api/config":
                return self._json({"config": svc.cfg, "meta": svc.meta()})
            if p == "/api/setup/state":
                return self._json(svc.wizard)
            if p == "/api/update":
                return self._json(svc.update_status())
            if p.startswith("/audio/"):
                f = svc.audio_dir / Path(unquote(p[7:])).name
                if f.is_file() and f.suffix.lower() in AUDIO_EXT:
                    ctype = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
                             ".m4a": "audio/mp4"}.get(f.suffix.lower(), "application/octet-stream")
                    return self._send(200, f.read_bytes(), ctype)
            return self._send(404, b"Not found", "text/plain")
        except Exception as exc:
            log.exception("panel error")
            return self._json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Azan-Panel") != "1":
            return self._send(403, b"Forbidden", "text/plain")
        svc = self.service
        url = urlparse(self.path)
        p = url.path
        try:
            if p == "/api/login":
                if not svc.box:
                    return self._json({"ok": True})
                req = json.loads(self._body(10_000) or b"{}")
                token = svc.auth.login(self.client_address[0], str(req.get("password") or ""),
                                       str(req.get("setup_code") or ""))
                log.info("Signed in to the control panel from %s", self.client_address[0])
                return self._send(200, json.dumps({"ok": True}).encode(), extra={"Set-Cookie": (
                    f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}")})
            if p == "/api/logout":
                svc.auth.logout(self._token())
                return self._send(200, json.dumps({"ok": True}).encode(), extra={"Set-Cookie": (
                    f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")})
            if not self._authed():
                return self._need_login()
            if p == "/api/upload":
                q = parse_qs(url.query)
                name = Path(q.get("name", ["audio.wav"])[0]).name
                name = re.sub(r"[^\w\-. ()]+", "_", name).strip() or "audio.wav"
                kind = q.get("kind", ["audio"])[0]
                data = self._body(limit=200_000_000)
                if kind == "timetable":
                    dest = svc.base / "timetable.csv"
                    text = data.decode("utf-8-sig", errors="replace")
                    if "fajr" not in text.splitlines()[0].lower():
                        raise ValueError("The first line must be: date,fajr,sunrise,dhuhr,asr,maghrib,isha")
                    dest.write_text(text, encoding="utf-8")
                    log.info("Timetable uploaded (%d lines)", len(text.splitlines()) - 1)
                    return self._json({"ok": True, "name": dest.name})
                if Path(name).suffix.lower() not in AUDIO_EXT:
                    raise ValueError("Please choose an audio file (mp3, wav, m4a, ogg ...)")
                svc.audio_dir.mkdir(exist_ok=True)
                (svc.audio_dir / name).write_bytes(data)
                log.info("Audio file added: %s (%.1f MB)", name, len(data) / 1e6)
                return self._json({"ok": True, "name": name, "files": svc.meta()["audio_files"]})

            req = json.loads(self._body() or b"{}")
            if p == "/api/config":
                cfg = req["config"]
                cfg.setdefault("general", {})["configured"] = True
                svc.apply(cfg, save=True)
                svc.auth.end_setup(self._setup_keep())  # setup is finished: the QR setup code stops working
                log.info("Settings saved")
                return self._json({"ok": True, "config": svc.cfg})
            if p == "/api/password":
                if not svc.box:
                    raise ValueError("Passwords are only used on the Azan SIP Box")
                if not svc.auth.setup_code and not svc.auth.verify(str(req.get("current") or "")):
                    raise ValueError("The current password is wrong")
                svc.auth.set_password(str(req.get("new") or ""))
                svc.auth.logout_others(self._token())
                log.info("Control panel password changed")
                return self._json({"ok": True})
            if p == "/api/factory-reset":
                if not svc.box:
                    raise ValueError("Factory reset is only available on the Azan SIP Box")
                if req.get("confirm") != "RESET":
                    raise ValueError("Type RESET to confirm")
                svc.factory_reset()
                return self._send(200, json.dumps({"ok": True}).encode(), extra={"Set-Cookie": (
                    f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")})
            if p == "/api/setup/state":
                return self._json({"ok": True, "rev": svc.set_wizard(req.get("state"), str(req.get("client") or ""))})
            if p == "/api/setup/password":
                if not (svc.box and svc.auth.setup_code):
                    raise ValueError("Setup is already finished - change the password under Settings")
                if req.get("sticker"):
                    svc.auth.use_factory_password()
                else:
                    svc.auth.set_password(str(req.get("new") or ""))
                log.info("First-time setup: %s", "keeping the factory password" if req.get("sticker")
                         else "own password chosen")
                return self._json({"ok": True})
            if p == "/api/setup/usb":
                return self._json({"ok": True, **svc.setup_usb()})
            if p == "/api/setup/connect":
                return self._json(svc.setup_connect(req["target"]))
            if p == "/api/setup/finish":
                again = svc.configured()
                if again and not req.get("again"):
                    raise ValueError("Setup is already finished")
                cfg = req["config"]
                cfg.setdefault("general", {})["configured"] = True
                svc.apply(cfg, save=True)
                svc.auth.end_setup(self._setup_keep())
                svc.set_wizard({"finished": True}, "")
                log.info("Setup guide finished" if again else "First-time setup finished")
                return self._json({"ok": True})
            if p == "/api/live/test":
                return self._json({"ok": True, **svc.live.test(str(req.get("url") or ""))})
            if p == "/api/preview":
                return self._json({"ok": True, **svc.preview(req["config"], req.get("days", 7), req.get("start"))})
            if p == "/api/test":
                return self._json(svc.test(req))
            if p == "/api/stop":
                if svc.app:
                    svc.app.stop_audio()
                return self._json({"ok": True})
            if p == "/api/scheduler":
                cfg = copy.deepcopy(svc.cfg)
                cfg.setdefault("general", {})["scheduler_enabled"] = bool(req.get("enabled"))
                svc.apply(cfg, save=True)
                log.info("Automatic azan %s", "switched ON" if req.get("enabled") else "PAUSED")
                return self._json({"ok": True})
            if p == "/api/prayer":
                cfg = copy.deepcopy(svc.cfg)
                pr = cfg.setdefault("prayers", {})
                enabled = [x for x in pr.get("enabled", ADHAN_PRAYERS) if x != req["prayer"]]
                if req.get("enabled"):
                    enabled.append(req["prayer"])
                pr["enabled"] = [x for x in ADHAN_PRAYERS if x in enabled]
                svc.apply(cfg, save=True)
                log.info("%s azan %s", req["prayer"].title(), "on" if req.get("enabled") else "off")
                return self._json({"ok": True})
            if p == "/api/update/check":
                svc.check_updates()
                return self._json({"ok": True, **svc.update_status()})
            if p == "/api/update/apply":
                return self._json(svc.install("apply"))
            if p == "/api/update/rollback":
                return self._json(svc.install("rollback", req.get("name")))
            if p == "/api/update/auto":
                cfg = copy.deepcopy(svc.cfg)
                cfg.setdefault("general", {})["check_updates"] = bool(req.get("enabled"))
                svc.apply(cfg, save=True)
                return self._json({"ok": True})
            if p == "/api/startup":
                set_windows_startup(bool(req.get("enabled")), svc.base)
                log.info("Start with Windows %s", "on" if req.get("enabled") else "off")
                return self._json({"ok": True})
            if p == "/api/delete-audio":
                f = svc.audio_dir / Path(req["name"]).name
                in_use = [k for k, v in svc.cfg.get("audio", {}).items() if isinstance(v, str)
                          and Path(v).name == f.name]
                if in_use:
                    raise ValueError(f"{f.name} is in use ({', '.join(in_use)}); choose another file first")
                f.unlink(missing_ok=True)
                return self._json({"ok": True, "files": svc.meta()["audio_files"]})
            if p == "/api/quit":
                if svc.box:
                    raise ValueError("The box can't be quit from the control panel")
                self._json({"ok": True})
                log.info("Quit requested from the control panel")
                threading.Thread(target=svc.server.shutdown, daemon=True).start()
                return
            return self._json({"ok": False, "error": "unknown action"}, 404)
        except LoginError as exc:
            return self._json({"ok": False, "login": True, "error": str(exc)}, 403)
        except Exception as exc:
            if not isinstance(exc, (ValueError, KeyError, RuntimeError, FileNotFoundError)):
                log.exception("panel error")
            return self._json({"ok": False, "error": str(exc) or exc.__class__.__name__}, 400)

    do_HEAD = do_GET


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows reuse would let two copies share the port

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            return json.loads(r.read()).get("app") == "azan-sip"
    except Exception:
        return False


def serve(config_path: str | Path = "config.toml", open_browser: bool = True, verbose: bool = False,
          box: bool = False):
    config_path = Path(config_path).resolve()
    setup_logging(config_path.parent, verbose)
    port = DEFAULT_PORT
    try:
        if config_path.exists():
            port = int(load_config(config_path).get("general", {}).get("port", DEFAULT_PORT))
    except Exception:
        pass
    url = f"http://127.0.0.1:{port}/"
    wait_for_port = os.environ.pop("AZAN_WAIT_FOR_PORT", "") == "1"  # set when restarting after an update
    if os.environ.pop("AZAN_NO_BROWSER", "") == "1":
        open_browser = False
    server = None
    deadline = time.monotonic() + (45 if wait_for_port else 0)
    while server is None:
        try:
            server = PanelServer(("0.0.0.0" if box else "127.0.0.1", port), Handler)
        except OSError:
            if time.monotonic() < deadline:
                time.sleep(0.5)
                continue
            break
    if server is None:
        if _already_running(port):
            log.info("Azan SIP is already running - opening its control panel")
            if open_browser:
                webbrowser.open(url)
            return 0
        message_box(f"Azan SIP could not start: port {port} is used by another program.\n"
                    f"Change [general] port in {config_path.name}.")
        return 1

    svc = Service(config_path)
    svc.server = server
    Handler.service = svc
    if box:
        svc.start_box()
        try:  # port 80 as well, so http://azan.local works without a port number
            web = PanelServer(("0.0.0.0", 80), Handler)
            svc.extra_servers.append(web)
            threading.Thread(target=web.serve_forever, kwargs={"poll_interval": 0.5}, name="http-80",
                             daemon=True).start()
        except OSError as exc:
            log.warning("Could not open port 80 (%s) - the panel is on port %d only", exc, port)
        log.info("Box mode: control panel open to the network, password needed to change settings")
    log.info("Azan SIP%s %s - control panel at %s", " Box" if updater.box_version() else "", VERSION, url)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    threading.Thread(target=svc.auto_update_checker, name="update-check", daemon=True).start()
    threading.Thread(target=updater.cleanup_after_update, name="cleanup", daemon=True).start()
    try:  # if Start with Windows is on, keep it pointing at this copy (e.g. after switching to the .exe)
        if windows_startup_path() and windows_startup_path().exists():
            set_windows_startup(True, svc.base)
    except Exception:
        pass
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for extra in svc.extra_servers:
            extra.shutdown()
            extra.server_close()
        svc.shutdown()
        server.server_close()
        log.info("Azan SIP stopped")
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="Azan SIP")
    ap.add_argument("-c", "--config", default=None)
    ap.add_argument("--background", action="store_true", help="don't open the browser (used at Windows start-up)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    cfg = args.config or str(app_dir() / "config.toml")
    try:
        sys.exit(serve(cfg, open_browser=not args.background, verbose=args.verbose))
    except Exception as exc:
        logging.getLogger("azan-sip").exception("fatal error")
        message_box(f"Azan SIP stopped because of an error:\n\n{exc}\n\nSee azan.log for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()
