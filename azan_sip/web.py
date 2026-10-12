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

from . import __version__, rtp, tomlw, updater
from .auth import COOKIE, SESSION_SECONDS, Auth, LoginError
from .env import FROZEN, app_dir
from .sipreg import RegistrationManager
from .app import ADHAN_PRAYERS, AzanApp, Clip, Shared, build_targets, load_config
from .praytimes import METHODS, PRAYERS, SHAFAQ, get_zone

log = logging.getLogger("azan-sip")

DEFAULT_PORT = 8765
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".mp4"}
STATIC = Path(__file__).with_name("static")

DEFAULT_CONFIG = {
    "general": {"configured": False, "scheduler_enabled": True, "port": DEFAULT_PORT},
    "location": {"name": "London, United Kingdom", "latitude": 51.5074, "longitude": -0.1278,
                 "timezone": "local", "elevation": 0},
    "calculation": {"method": "MWL", "asr": "standard", "high_latitude": "angle"},
    "offsets": {p: 0 for p in ADHAN_PRAYERS},
    "prayers": {"enabled": list(ADHAN_PRAYERS), "reminder_minutes": 0, "skip_days": []},
    "audio": {"azan": "tone", "volume_db": 0, "lead_in_ms": 400},
    "network": {"local_ip": "", "multicast_ttl": 4},
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

    def configured(self) -> bool:
        return bool(self.cfg.get("general", {}).get("configured", True))

    def start_box(self):
        """Box mode: until first-time setup is done, a one-time code (QR on the screen) lets a phone in."""
        self.box = True
        if not self.configured():
            self.auth.new_setup_code()
        if not self.auth.has_password():
            log.warning("No password is set on this box - set one with: python -m azan_sip box-password --factory")

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
            st = {"version": __version__, "error": self.error, "restarting": self.restarting,
                  "update": {"available": bool(ui.get("available")), "latest_version": ui.get("latest_version")}, "running": bool(self.thread and self.thread.is_alive()),
                  "configured": self.cfg.get("general", {}).get("configured", True),
                  "location": self.cfg.get("location", {}).get("name") or "", "log": list(RING.lines)[-200:],
                  "log_id": RING.counter, "registrations": self.regs.info()}
            if app is None:
                return st
            now = app.now()
            today = app.times.for_date(now.date())
            events = app.events_for(now.date())
            nxt = app.next_event()
            azan_today = {p: w for w, p, k in events if k == "azan"}
            rows = []
            for name in PRAYERS:
                t = today[name]
                if name in azan_today:
                    t = azan_today[name]  # includes Jumu'ah override
                status = ""
                if name in ADHAN_PRAYERS:
                    if (t.date(), name, "azan") in app.fired:
                        status = "played"
                    elif t <= now:
                        status = "passed"
                if nxt and nxt[1] == name and nxt[0].date() == now.date() and nxt[2] == "azan":
                    status = "next"
                rows.append({"name": name, "time": f"{t:%H:%M}", "azan": name in ADHAN_PRAYERS,
                             "enabled": name in app.enabled, "status": status,
                             "jumuah": name == "dhuhr" and now.weekday() == 4 and bool(app.jumuah_time)})
            st.update({
                "now": now.isoformat(timespec="seconds"), "date": f"{now:%A %d %B %Y}",
                "clock": f"{now:%H:%M:%S}", "timezone": app.times.tz_name,
                "method": METHODS.get(app.settings.method, {}).get("name", app.settings.method),
                "asr": app.settings.asr, "today": rows, "playing": app.now_playing,
                "skip_today": now.strftime("%a").lower()[:3] in app.skip_days,
                "next": {"prayer": nxt[1], "kind": nxt[2], "time": f"{nxt[0]:%H:%M}",
                         "day": "today" if nxt[0].date() == now.date() else
                         ("tomorrow" if nxt[0].date() == now.date() + timedelta(days=1) else f"{nxt[0]:%A}"),
                         "seconds": int((nxt[0] - now).total_seconds())} if nxt else None,
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
        info = dict(self.update_info or {"current_version": __version__, "repo": updater.REPO_URL,
                                         "current_commit": updater._installed(self.base).get("commit")})
        info["backups"] = updater.backups(self.base)
        info["mode"] = updater.mode(self.base)
        info["auto"] = self.cfg.get("general", {}).get("check_updates", True)
        if info["mode"] == "docker":
            info["note"] = updater.DOCKER_NOTE
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
                "folder": str(self.base)}

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
        return {"week": rows, "timezone": app.times.tz_name}

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
        threading.Thread(target=app.broadcast, args=(clip, req.get("label") or "Test", targets),
                         daemon=True).start()
        return {"ok": True}


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

    def _need_login(self):
        return self._json({"ok": False, "login": True, "error": "Please enter the password to change settings"}, 401)

    def _session_info(self) -> dict:
        svc = self.service
        return {"box": svc.box, "logged_in": self._authed(), "has_password": svc.auth.has_password() if svc.box else False,
                "custom_password": svc.auth.custom_password() if svc.box else False,
                "configured": svc.configured(), "setup": bool(svc.box and svc.auth.setup_code)}

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
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
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if p == "/api/ping":
                return self._json({"app": "azan-sip", "version": __version__})
            if p == "/api/session":
                return self._json(self._session_info())
            if p == "/api/state":
                st = svc.state()
                if not self._authed():  # prayer times are public, the log is not
                    st.update(log=[], history=[])
                return self._json(st)
            if p == "/api/setup-code":  # for the QR code on the box's own screen only
                if not (svc.box and self._client_local() and svc.auth.setup_code):
                    return self._send(404, b"Not found", "text/plain")
                hostport = "" if svc.extra_servers else f":{svc.server.server_address[1]}"
                code = svc.auth.setup_code
                return self._json({"code": code, "urls": [f"http://{ip}{hostport}/?setup={code}"
                                                          for ip in local_ips()]})
            if not self._authed():
                return self._need_login()
            if p == "/api/config":
                return self._json({"config": svc.cfg, "meta": svc.meta()})
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
                svc.auth.end_setup()  # setup is finished: the QR setup code stops working
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
    log.info("Azan SIP %s - control panel at %s", __version__, url)
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
