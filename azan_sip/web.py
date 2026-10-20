"""The azan-sip service with its built-in control panel (served on http://127.0.0.1:8765).

Runs the scheduler in the background and serves a local web page to manage everything:
location, calculation, audio, phones, tests and logs. Only reachable from this computer.

Box mode (`azan-sip run --box`, used on AthanBox): the panel is also reachable from the
network (port 8765 and port 80, e.g. http://azan.local) and changing anything needs AthanBox's
password. Viewing the prayer times does not.
"""
from __future__ import annotations

import collections
import copy
import html
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

from . import __version__, hostvol, kids, live, look, ramadan, rtp, screens, security, sipserver, tomlw, updater
from .auth import COOKIE, SESSION_SECONDS, Auth, LoginError
from .env import app_dir
from .sipreg import Registration, RegistrationManager
from .app import ADHAN_PRAYERS, AzanApp, Clip, Shared, build_targets, load_config
from .praytimes import METHODS, PRAYERS, SHAFAQ, get_zone

log = logging.getLogger("azan-sip")

DEFAULT_PORT = 8765
# The version shown in the panel: AthanBox's own (BOX_CHANGELOG.md), or the desktop app's without it.
VERSION = updater.box_version() or __version__
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".mp4"}
STATIC = Path(__file__).with_name("static")
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".woff2": "font/woff2", ".svg": "image/svg+xml", ".png": "image/png"}
PAGES = {"/setup": "setup.html", "/display": "display.html"}
USB_ROOTS = ("/media", "/run/media")  # where Debian mounts USB sticks

# The TV app's own routes. From other devices these follow "Let TV screens connect" only, so switching
# it off disconnects every TV screen. (The prayer-times screen itself stays open to everyone, as before.)
TV_PATHS = {"/tv", "/tv.webmanifest", "/api/tv", "/api/tv/star", "/apk"}
TV_PREFIXES = ("/tv/sound/", "/icons/")

# Added to the display when it is opened as the TV app (/tv): what an iPad needs to add it to the
# Home Screen as a full-screen app with its own icon.
TV_HEAD = b"""<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<link rel="manifest" href="/tv.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Azan TV">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0e4a40">
<link rel="apple-touch-icon" href="/icons/tv-180.png">"""
# The Android TV app (android/, built by each release). AthanBox's Docker image carries a copy
# (static/AzanTV.apk); otherwise it comes from the copy attached to AthanBox's latest release.
APK_FILE = STATIC / "AzanTV.apk"
APK_URL = "https://github.com/Fudgeamin99/azan-sip-box/releases/latest/download/AzanTV.apk"
TV_MANIFEST = {"name": "Azan SIP TV", "short_name": "Azan TV", "start_url": "/tv", "scope": "/tv",
               "display": "fullscreen", "orientation": "landscape", "background_color": "#0e4a40",
               "theme_color": "#0e4a40", "description": "Prayer times and the azan on a TV",
               "icons": [{"src": "/icons/tv-192.png", "sizes": "192x192", "type": "image/png"},
                         {"src": "/icons/tv-512.png", "sizes": "512x512", "type": "image/png"}]}

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
    # can't, it goes through AthanBox (off: AthanBox never relays it, so it uses no data for it)
    "live": {"url": "", "direct": True, "fallback": True},
    "targets": [{"name": "All phones (multicast)", "type": "multicast", "address": "224.5.6.20:10008",
                 "codec": "PCMU", "enabled": True}],
}


TIMETABLE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ramadan {year} timetable</title>
<style>
@page{{size:A4;margin:12mm}}
*{{box-sizing:border-box}}
body{{margin:0;background:#e9e5dc;color:#1d2622;font-family:"Segoe UI",system-ui,-apple-system,sans-serif}}
.sheet{{width:210mm;min-height:297mm;margin:16px auto;padding:12mm 13mm;background:#fff;box-shadow:0 2px 16px rgba(0,0,0,.12);display:flex;flex-direction:column}}
.head{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start}}
h1{{margin:0;font-size:30px;letter-spacing:-.4px}} h1 span{{font-family:"Noto Naskh Arabic","Traditional Arabic","Segoe UI",serif;color:#146356;font-size:24px;margin-left:12px}}
.sub{{margin-top:6px;font-weight:600}} .small{{font-size:12.5px;color:#4f5a55;margin-top:2px}}
.masjid{{text-align:right;font-weight:700;font-size:16px}}
table{{width:100%;border-collapse:collapse;margin-top:14px;font-size:13px;font-variant-numeric:tabular-nums}}
th{{background:#1d2622;color:#fff;font-size:12px;padding:7px 8px;text-align:right}} th:nth-child(-n+2){{text-align:left}}
th.k{{background:#146356}}
td{{padding:4px 8px;border-bottom:1px solid #ddd6c6;text-align:right}} td:nth-child(-n+2){{text-align:left}}
td.n{{font-weight:700}} td.k{{font-weight:800;background:rgba(20,99,86,.08)}}
tr:nth-child(even) td{{background-color:#faf8f3}} tr:nth-child(even) td.k{{background:rgba(20,99,86,.11)}}
tr.ten td{{background-color:#f3ecd9}} tr.ten td.k{{background:#eadfbf}}
.legend{{margin-top:10px;font-size:12px;color:#4f5a55;display:flex;gap:20px;flex-wrap:wrap}}
.legend i{{display:inline-block;width:14px;height:10px;background:#f3ecd9;border:1px solid #ddd6c6;margin-right:6px;vertical-align:-1px}}
.foot{{margin-top:auto;padding-top:10px;border-top:1px solid #ddd6c6;font-size:12px;color:#4f5a55;display:flex;justify-content:space-between;gap:16px}}
.bar{{width:210mm;margin:16px auto 0;display:flex;justify-content:flex-end;gap:10px}}
.bar button{{font:inherit;font-weight:600;padding:9px 16px;border-radius:10px;border:1px solid #146356;background:#146356;color:#fff;cursor:pointer}}
@media print{{body{{background:#fff}} .bar{{display:none}} .sheet{{margin:0;box-shadow:none;width:auto;min-height:0;padding:0}}
  th,td.k,tr.ten td{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}}}
@media (max-width:820px){{.sheet,.bar{{width:auto;margin:10px}} .sheet{{min-height:0;padding:16px}} table{{font-size:12px}} td,th{{padding:4px 5px}}}}
</style></head><body>
<div class="bar"><button onclick="window.print()">Print</button></div>
<div class="sheet">
  <div class="head"><div>
    <h1>Ramadan {year}<span lang="ar">رمضان {arabic_year}</span></h1>
    <div class="sub">{span}{loc}</div>
    <div class="small">{method} · Sehri ends {mins} minutes before Fajr{isha_note}</div></div>
    <div class="masjid" style="{title_style}">{title}</div></div>
  <table><thead><tr><th>Ramadan</th><th>Date</th><th class="k">Sehri ends</th><th>Fajr</th><th>Sunrise</th><th>Dhuhr</th><th>Asr</th><th class="k">Iftar</th><th>Isha</th></tr></thead>
  <tbody>
{rows}
  </tbody></table>
  <div class="legend"><span><i></i>Last ten nights</span><span><b>Fri</b> in bold: Jumu'ah</span><span>Taraweeh every night after Isha</span></div>
  <div class="foot"><span>{note}</span><span>Printed from AthanBox</span></div>
</div></body></html>"""


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
    if sys.stderr:
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
    """The address phones and PCs reach this AthanBox on (the one with the default route)."""
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


def message_box(text: str, title: str = "AthanBox"):
    if sys.stderr:
        print(text, file=sys.stderr)


# Azan recordings that come with AthanBox (azan_sip/sounds). Each is copied into audio/ once, so it can
# be picked on the Azan sound page; one the owner removes afterwards isn't added back.
SOUNDS = Path(__file__).with_name("sounds")


def add_bundled_sounds(audio_dir: Path) -> None:
    try:
        bundled = sorted(p for p in SOUNDS.glob("*") if p.suffix.lower() in AUDIO_EXT)
        if not bundled:
            return
        audio_dir.mkdir(parents=True, exist_ok=True)
        note = audio_dir / ".bundled.json"
        try:
            offered = set(json.loads(note.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            offered = set()
        added = []
        for src in bundled:
            if src.name in offered:
                continue
            if not (audio_dir / src.name).exists():
                shutil.copyfile(src, audio_dir / src.name)
                added.append(src.name)
            offered.add(src.name)
        note.write_text(json.dumps(sorted(offered)), encoding="utf-8")
        if added:
            log.info("Added %d azan recording(s) that come with AthanBox: %s", len(added), ", ".join(added))
    except OSError as exc:
        log.warning("Couldn't add the azan recordings that come with AthanBox: %s", exc)


# ------------------------------------------------------------------ service
class Service:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path).resolve()
        self.base = self.config_path.parent
        self.audio_dir = self.base / "audio"
        add_bundled_sounds(self.audio_dir)
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
        self.tvs = screens.Screens()       # TV screens that play the azan (the TV app)
        self.sounds = screens.Sounds()
        self.shared.screens_listening = self.tvs.listening
        self.stars = kids.Stars(self.base / "kids.json")   # kids mode: "Did you pray?" stars, per screen
        self._tv_next = (0.0, None, None)
        self.security: security.Security | None = None   # the Security page (box mode only)
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
        kids.check(cfg)
        ramadan.check(cfg.get("ramadan"))
        sipserver.check_targets(cfg.get("targets", []))
        with self.lock:
            app = AzanApp(self.config_path, cfg, require_targets=False, shared=self.shared)  # raises if invalid
            if save:
                self.write(cfg)
            self._stop_scheduler(halt_audio=False)  # an azan that is playing carries on
            self.app, self.cfg, self.error = app, cfg, None
            self.regs.sync(cfg)  # stay registered on the phone system (shows as Idle)
            sipserver.MANAGER.sync(cfg, self.base)  # phones and phone systems that sign in to AthanBox
            g = cfg.get("general", {})
            self._host_volume(g.get("tv_volume", 100))
            if g.get("scheduler_enabled", True) and g.get("configured", True):
                self.thread = threading.Thread(target=app.run_forever, name="scheduler", daemon=True)
                self.thread.start()

    def _host_volume(self, value):
        """The volume slider on the TV app page: set the computer's own volume (in the background)."""
        try:
            pct = max(0.0, min(100.0, float(value if value not in (None, "") else 100)))
        except (TypeError, ValueError):
            pct = 100.0
        if getattr(self, "_host_pct", None) == pct:
            return
        self._host_pct = pct

        def run():
            self.host_volume_set = hostvol.set_volume(pct)
        threading.Thread(target=run, name="host-volume", daemon=True).start()

    @property
    def live_url(self) -> str:
        return ((self.cfg.get("live") or {}).get("url") or "").strip() or live.DEFAULT_URL

    def live_screen(self) -> dict:
        """Where screens get Makkah live: through AthanBox, or (direct) from the stream itself, so AthanBox
        uses no data for it. The link is only shared with screens when they need it."""
        lv = self.cfg.get("live") or {}
        if lv.get("direct") is not False:   # on unless switched off
            return {"direct": True, "url": self.live_url, "fallback": lv.get("fallback") is not False}
        return {"direct": False}

    @property
    def relay_off(self) -> bool:
        """Screens must fetch Makkah live themselves: AthanBox doesn't relay it at all."""
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
        self.security = security.Security(self.base, docker=updater.DOCKER)
        if not self.configured():
            self.auth.new_setup_code()
        if not self.auth.has_password():
            log.warning("No password is set on this AthanBox - set one with: python -m azan_sip box-password --factory")
        if updater.DOCKER:
            updater.ensure_helper_token(self.base)

    def factory_reset(self):
        """Back to how AthanBox left the factory: settings, recordings, timetable and own password removed."""
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
            self.tvs.clear()   # TV screens can't connect until they're allowed again
            if self.security:
                self.security.reset()   # this network on the whitelist again, nothing blocked
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
        sipserver.MANAGER.stop()

    # -- TV screens (the TV app)
    @property
    def screens_on(self) -> bool:
        """Let TV screens connect: the TV app may open and play the azan, without the password."""
        return bool(self.cfg.get("general", {}).get("screens"))

    def set_screens(self, on: bool):
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("general", {})["screens"] = bool(on)
        self.apply(cfg, save=True)
        if not on:
            self.tvs.clear()   # they can't connect any more, so don't list them
        log.info("TV screens %s", "can connect" if on else "switched off - every TV screen is disconnected")

    def tv_links(self, host: str = "") -> dict:
        """Addresses for the TV app page: the one the page was opened on first (say azan.local), then AthanBox's
        own. The Android app gets AthanBox's IP address: it adds port 8765 by itself."""
        port = self.server.server_address[1] if self.server else DEFAULT_PORT
        here = "" if self.extra_servers else f":{port}"   # with port 80 open, no port is needed
        hosts = [host] if host else []
        hosts += [ip + here for ip in dict.fromkeys([box_ip(), *local_ips()])]
        urls = [f"http://{h}/" for h in dict.fromkeys(hosts)]
        on = self.screens_on
        ip = box_ip()
        return {"screens": on, "tv_urls": [u + "tv" for u in urls], "apk_urls": [u + "apk" for u in urls],
                "apk_here": APK_FILE.is_file(), "address": ip if port == DEFAULT_PORT else f"{ip}:{port}",
                "tv_qr": qr_svg(urls[0] + "tv").decode() if urls and on else None,
                "apk_qr": qr_svg(urls[0] + "apk").decode() if urls and on else None}

    def today(self) -> date:
        app = self.app
        return app.now().date() if app is not None else date.today()

    def kids_screen(self, sid: str) -> dict | None:
        """Kids mode for one TV screen, or None when it isn't a kids screen."""
        if not kids.is_kids(self.cfg, sid):
            return None
        k = kids.settings(self.cfg)
        del k["screens"]
        if k["stars"]:
            k["week"] = self.stars.week(sid, self.today())
            app = self.app
            if app is not None and app.ramadan.mode != "off" and app.ramadan.fasting(self.today()):
                start, n, _ = app.ramadan.period(self.today())
                k["fasts"] = {"day": app.ramadan.day_number(self.today()), "days": n,
                              "done": self.stars.fasts(sid, start, n)}
        return k

    def set_kids_screen(self, sid: str, on: bool):
        cfg = copy.deepcopy(self.cfg)
        k = cfg.setdefault("kids", {})
        ids = [s for s in kids.settings(cfg)["screens"] if s != kids.screen_id(sid)]
        if on:
            ids.append(kids.screen_id(sid))
        k["screens"] = ids
        self.apply(cfg, save=True)
        log.info("Kids mode %s on a TV screen", "switched on" if on else "switched off")

    def add_star(self, sid: str, prayer: str) -> dict:
        """A child pressed "Yes, give me my star" on a kids screen, after that prayer's time
        (or "Yes, I fasted!" at Iftar in Ramadan). Gives back the screen's chart: week, and fasts in Ramadan."""
        k = self.kids_screen(sid)
        if not k or not k["stars"]:
            raise ValueError("Stars aren't switched on for this screen")
        app = self.app
        if app is None or prayer not in kids.MARKS:
            raise ValueError("That isn't one of today's prayers")
        now = app.now()
        if prayer == "fast":
            if "fasts" not in k or now < app.day_times(now.date())["maghrib"]:
                raise ValueError("It isn't Iftar time yet")
        else:
            row = next((r for r in self.rows_for(app, now.date(), now, None) if r["name"] == prayer), None)
            if row is None or row["time"] > f"{now:%H:%M}":
                raise ValueError("It isn't time for that prayer yet")
        self.stars.add(sid, now.date(), prayer)
        k = self.kids_screen(sid)
        return {"week": k.get("week"), "fasts": k.get("fasts")}

    def tv_state(self, sid: str = "") -> dict:
        """What TV screens ask about twice a second: what's playing (and since when, so they join in at
        the right place), how the last one ended, and what plays next (to download it early)."""
        sh = self.shared
        try:  # AthanBox's own screen: the volume slider on the TV app page, 0-100 %. When the computer's
              # own volume was set instead (hostvol), the screen plays at full volume.
            vol = max(0.0, min(100.0, float(self.cfg.get("general", {}).get("tv_volume", 100)))) / 100
        except (TypeError, ValueError):
            vol = 1.0
        if getattr(self, "host_volume_set", False):
            vol = 1.0
        out = {"now": int(time.time() * 1000), "version": VERSION, "playing": None, "last": sh.last,
               "next": None, "volume": vol, "kids": self.kids_screen(sid) if sid else None}
        p = sh.playing
        if p and p["screens"]:
            out["playing"] = {"seq": p["seq"], "label": p["label"], "started": int(p["started"] * 1000),
                              "sound": self.sounds.add(p["clip"])}
        app = self.app
        if app is not None and self.thread is not None and self.thread.is_alive():
            at, key, nxt = self._tv_next
            if key != id(app) or time.monotonic() - at > 15:
                try:
                    nxt = app.next_event()
                except Exception:
                    nxt = None
                self._tv_next = (time.monotonic(), id(app), nxt)
            if nxt:
                when, prayer, kind = nxt
                clip = app.event_clip(when, prayer, kind)
                if clip is not None:
                    out["next"] = {"at": int(when.timestamp() * 1000), "label": ramadan.event_label(prayer, kind),
                                   "sound": self.sounds.add(clip)}
        return out

    # -- Ramadan
    @staticmethod
    def ramadan_display(app: AzanApp, now, cards_date, demo: bool = False) -> dict | None:
        """What the display shows in Ramadan: the day counter, Iftar and Suhoor countdowns, the dua at
        Iftar and the Eid screen. demo pretends today is the 13th of Ramadan (for the preview button)."""
        r = app.ramadan
        if demo:
            r = ramadan.Ramadan({**r.s, "mode": "on", "start": (now.date() - timedelta(days=12)).isoformat()})
        if r.mode == "off":
            return None
        s, today = r.s, now.date()
        tomorrow = today + timedelta(days=1)
        t = app.day_times(today)
        fast_today, fast_tomorrow, eid_today = r.fasting(today), r.fasting(tomorrow), r.eid_day(today)
        eve = not fast_today and fast_tomorrow and now >= t["maghrib"]
        if not (fast_today or eid_today or eve):
            return None
        mins = timedelta(minutes=r.num("suhoor_minutes"))
        sehri = lambda d: app.times.for_date(d)["fajr"] - mins  # noqa: E731
        hm = lambda x: f"{x:%H:%M}"  # noqa: E731
        start, n, expected = r.period(tomorrow if eve else today)
        k = r.day_number(today) if fast_today else None
        out = {"theme": bool(s["theme_on"]), "counter": bool(s["counter_on"]), "day": k, "days": n,
               "hijri": r.hijri(today), "last_ten": bool(k and k >= 21), "expected": expected,
               "starts_tomorrow": eve, "eid_in": max(0, (start + timedelta(days=n) - today).days),
               "fajr": hm(t["fajr"]), "maghrib": hm(t["maghrib"]), "isha": hm(t["isha"]),
               "sehri": hm(sehri(today)) if fast_today else None,
               "next_sehri": hm(sehri(tomorrow)) if fast_tomorrow else None,
               "cards_sehri": None, "phase": None, "seconds": None, "target": None}
        if r.fasting(cards_date):
            out["cards_sehri"] = hm(sehri(cards_date))
            out["cards_sehri_passed"] = cards_date == today and sehri(today) <= now
        phase = target = None
        if fast_today and s["countdown_on"] and sehri(today) <= now < t["maghrib"]:
            phase, target = "iftar", t["maghrib"]
        elif fast_today and s["dua_on"] and t["maghrib"] <= now < t["maghrib"] + timedelta(minutes=15):
            phase = "dua"
        elif fast_today and s["countdown_on"] and now < sehri(today):
            phase, target = "suhoor", sehri(today)
            out["target_fajr"] = hm(t["fajr"])
        elif fast_tomorrow and s["countdown_on"] and now >= t["isha"]:
            phase, target = "suhoor", sehri(tomorrow)
            out["target_fajr"] = hm(sehri(tomorrow) + mins)
        elif eid_today and s["eid_on"] and now < t["dhuhr"]:
            phase = "eid"
            out["eid"] = {"prayer1": s["eid_prayer1"], "prayer2": s["eid_prayer2"],
                          "year": r.hijri_year(today - timedelta(days=1))}
        out["phase"] = phase
        if target is not None:
            out.update(seconds=int((target - now).total_seconds()), target=hm(target))
        return out

    def ramadan_summary(self, app: AzanApp, now) -> dict:
        r, today = app.ramadan, now.date()
        start, n, expected = r.period(today)
        return {"mode": r.mode, "fasting": r.fasting(today), "day": r.day_number(today) if r.fasting(today) else None,
                "start": start.isoformat(), "days": n, "expected": expected,
                "expected_start": ramadan.expected_start(today, n).isoformat(),
                "eid": (start + timedelta(days=n)).isoformat(), "hijri_year": r.hijri_year(today),
                "today": today.isoformat()}

    def ramadan_timetable(self) -> str:
        """A printable A4 timetable for the month: Sehri, the five prayers and Iftar for every day."""
        app = self.app
        if app is None:
            raise RuntimeError(self.error or "Settings not loaded")
        r, today = app.ramadan, app.now().date()
        start, n, expected = r.period(today)
        year = r.hijri_year(today)
        mins = r.num("suhoor_minutes")
        esc = html.escape
        rows = []
        for i in range(n):
            d = start + timedelta(days=i)
            t = app.day_times(d)
            sehri = t["fajr"] - timedelta(minutes=mins)
            cls = " class='ten'" if i >= 20 else ""
            day = f"<b>{d:%a}</b>" if d.weekday() == 4 else f"{d:%a}"
            rows.append(f"<tr{cls}><td class='n'>{i + 1}</td><td>{day} {d.day} {d:%b}</td><td class='k'>{sehri:%H:%M}</td>"
                        + "".join(f"<td>{t[p]:%H:%M}</td>" for p in ("fajr", "sunrise", "dhuhr", "asr"))
                        + f"<td class='k'>{t['maghrib']:%H:%M}</td><td>{t['isha']:%H:%M}</td></tr>")
        end = start + timedelta(days=n - 1)
        span = f"{start:%B} – {end:%B %Y}" if start.month != end.month else f"{start:%B %Y}"
        if start.year != end.year:
            span = f"{start:%B %Y} – {end:%B %Y}"
        loc = self.cfg.get("location", {}).get("name") or ""
        title = look.display(self.cfg).get("title") or ""
        method = METHODS.get(app.settings.method, {}).get("name", app.settings.method)
        arabic_year = str(year).translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
        nice = f"{start:%A} {start.day} {start:%B %Y}"
        note = (f"Ramadan is expected to begin on {nice}. Dates depend on the moonsighting."
                if expected else f"Ramadan begins on {nice}.")
        isha_note = {"fixed": f" · Isha at {r.s['isha_time']} in Ramadan",
                     "after": f" · Isha {r.num('isha_after')} minutes after Maghrib in Ramadan"}.get(r.s["isha_mode"], "")
        return TIMETABLE_HTML.format(
            year=year, arabic_year=arabic_year, span=esc(span), loc=esc((" · " + loc) if loc else ""),
            method=esc(method), mins=mins, isha_note=esc(isha_note), title=esc(title),
            title_style="" if title else "display:none", rows="\n".join(rows), note=esc(note))


    # -- state for the panel
    def state(self, demo: bool = False) -> dict:
        """demo: the Ramadan screens as on the 13th of Ramadan (the Ramadan page's preview button)."""
        with self.lock:
            app = self.app
            ui = self.update_info or {}
            kid_ids = set(kids.settings(self.cfg)["screens"])
            st = {"version": VERSION, "error": self.error, "restarting": self.restarting,
                  "update": {"available": bool(ui.get("available")), "latest_version": ui.get("latest_version")}, "running": bool(self.thread and self.thread.is_alive()),
                  "configured": self.cfg.get("general", {}).get("configured", True),
                  "setup_manual": self.setup_manual(),
                  "look": look.display(self.cfg),  # theme and layout of the prayer-times screen
                  "live": self.live_screen(),
                  "location": self.cfg.get("location", {}).get("name") or "", "log": list(RING.lines)[-200:],
                  "log_id": RING.counter, "registrations": self.regs.info(),
                  "sipserver": sipserver.MANAGER.info(),   # phones and phone systems that sign in to AthanBox
                  "screens": {"on": self.screens_on, "list": [dict(s, kids=s["id"] in kid_ids) for s in self.tvs.info()]}}
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
                "ramadan": self.ramadan_summary(app, now),
                # what the prayer-times screen shows in Ramadan (countdowns, dua, Eid), for the day on its cards
                "ramadan_screen": self.ramadan_display(app, now, now.date() + timedelta(days=1) if "tomorrow" in st
                                                       else now.date(), demo),
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
        log.info("Restarting AthanBox%s", "" if spawned else " (service manager will start it again)")
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
        if not app.targets and not (self.screens_on and self.tvs.listening()):
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
            w.append(f"A new version of AthanBox ({self.update_info.get('latest_version')}) is available - "
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
                "ffmpeg": bool(shutil.which("ffmpeg")), "config_path": str(self.config_path),
                "live_default": live.DEFAULT_URL,
                "folder": str(self.base)}

    @staticmethod
    def rows_for(app: AzanApp, day, now, next_key: tuple | None) -> list[dict]:
        """One day's times for the cards: played / passed / next (next_key is (date, prayer))."""
        times = app.day_times(day)   # with the Ramadan Isha time when one is set
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
        return {"prayer": nxt[1], "kind": nxt[2], "label": ramadan.event_label(nxt[1], nxt[2]), "time": f"{nxt[0]:%H:%M}",
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
        targets, on_screens = None, None
        if req.get("screens_only"):   # TV app page: test the TV screens only
            if not self.tvs.listening():
                raise RuntimeError("No TV screen is ready to play. Open the TV app on the screen and tap it once to turn on its sound.")
            targets, on_screens = [], True
        elif req.get("target") is not None:  # test one phone entry (possibly unsaved)
            t = dict(req["target"], enabled=True)
            targets = build_targets({"network": self.cfg.get("network", {}), "targets": [t]})
            user = str(req.get("account") or "").strip().lower()
            if user:   # the Test button on one desk phone or SIP trunk account: ring only its sign-ins
                server = sipserver.MANAGER.server
                acct = server.accounts.get(user) if server else None
                if not (acct and server.active().get(user)):
                    raise RuntimeError(f"{(acct and acct.name) or user} isn't signed in to AthanBox"
                                       + ("" if acct else " yet - press Save changes first"))
                for x in targets:
                    if isinstance(x, sipserver.ServerTarget):
                        x.only = user
        if app.now_playing:
            raise RuntimeError(f"Already playing: {app.now_playing}")
        if req.get("wait"):  # play it now and say whether it worked (setup wizard)
            first = RING.counter
            if not app.broadcast(clip, req.get("label") or "Test", targets, on_screens):
                problems = [x["msg"] for x in list(RING.lines) if x["id"] > first and x["level"] in ("WARNING", "ERROR")]
                raise RuntimeError(problems[-1] if problems else "the test chime didn't play")
            return {"ok": True}
        threading.Thread(target=app.broadcast, args=(clip, req.get("label") or "Test", targets, on_screens),
                         daemon=True).start()
        return {"ok": True}

    # -- first-time setup wizard (setup.html)
    def set_wizard(self, state, driver: str) -> int:
        with self.lock:
            self.wizard = {"rev": self.wizard["rev"] + 1, "state": state, "driver": driver}
            return self.wizard["rev"]

    def setup_usb(self) -> dict:
        """Copy azan recordings from a USB stick plugged into AthanBox."""
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
        # AthanBox's IP addresses, and .local names (mDNS, only answered on this network).
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
        """Whose setup-code session survives the end of setup: a phone or PC, never AthanBox's own screen."""
        return None if self._client_local() else self._token()

    def _tv_allowed(self, p: str) -> bool:
        """The TV app follows "Let TV screens connect" (on the TV app page) on every device but AthanBox itself."""
        if not (p in TV_PATHS or p.startswith(TV_PREFIXES)):
            return True
        return self._client_local() or self.service.screens_on

    def _need_login(self):
        return self._json({"ok": False, "login": True, "error": "Please enter the password to change settings"}, 401)

    def _session_info(self) -> dict:
        svc = self.service
        return {"box": svc.box, "logged_in": self._authed(), "has_password": svc.auth.has_password() if svc.box else False,
                "custom_password": svc.auth.custom_password() if svc.box else False,
                "configured": svc.configured(), "setup": bool(svc.box and svc.auth.setup_code),
                "local": self._client_local(), "docker": updater.DOCKER, "ip": box_ip(), "version": VERSION,
                "panel": look.panel(svc.cfg)}

    def _page_host(self) -> str:
        """The address this page was opened on (an IP address or a .local name), if it is one other devices can use."""
        raw = (self.headers.get("Host") or "").strip()
        name = raw.split(":")[0].lower()
        if not name or name in ("127.0.0.1", "localhost") or not re.fullmatch(r"[a-z0-9.\-]+(:\d+)?", raw.lower()):
            return ""
        return raw

    def _setup_urls(self) -> list[str]:
        """Links for the setup QR code: the address this page was opened on first, then AthanBox's own."""
        svc = self.service
        hostport = "" if svc.extra_servers else f":{svc.server.server_address[1]}"
        raw = (self.headers.get("Host") or "").split(":")[0]
        ips = [raw] if raw and raw != "127.0.0.1" and re.fullmatch(r"[\d.]+", raw) else []
        return [f"http://{ip}{hostport}/setup?setup={svc.auth.setup_code}"
                for ip in dict.fromkeys(ips + [box_ip(), *local_ips()])]

    def _may_see_setup_code(self) -> bool:
        """AthanBox's own screen, or someone already signed in (to carry on from their phone)."""
        svc = self.service
        return bool(svc.box and svc.auth.setup_code and (self._client_local() or self._authed()))

    def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None,
              cache: bool | str = False, frame: str = "DENY"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache if isinstance(cache, str) else "max-age=86400" if cache else "no-store")
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
        if security.FILTER.blocked(self.client_address[0]):   # on the blacklist (Security page)
            return self._send(403, b"Forbidden", "text/plain")
        if not self._host_ok():
            return self._send(403, b"Forbidden", "text/plain")
        svc = self.service
        url = urlparse(self.path)
        p = url.path
        if not self._tv_allowed(p):
            return self._send(403, "TV screens can't connect. In AthanBox's control panel, go to TV app and "
                                   "switch on \u201cLet TV screens connect\u201d. Then try again.".encode("utf-8"),
                              "text/plain; charset=utf-8")
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
            if p == "/tv":   # the TV app: the prayer-times screen, which also plays the azan
                body = re.sub(rb'<meta name="viewport"[^>]*>\s*<!--tv-->', lambda m: TV_HEAD,
                              (STATIC / "display.html").read_bytes(), count=1)
                body = body.replace(b"<title>AthanBox \xc2\xb7 Prayer times</title>", b"<title>Azan SIP TV</title>", 1)
                return self._send(200, body, "text/html; charset=utf-8")
            if p == "/apk":   # the Android app (Azan SIP TV)
                if APK_FILE.is_file():
                    return self._send(200, APK_FILE.read_bytes(), "application/vnd.android.package-archive",
                                      extra={"Content-Disposition": 'attachment; filename="AzanTV.apk"'})
                return self._send(302, b"", "text/plain", extra={"Location": APK_URL})
            if p == "/tv.webmanifest":
                return self._send(200, json.dumps(TV_MANIFEST).encode(), "application/manifest+json",
                                  cache="public, max-age=3600")
            if p.startswith("/icons/"):
                f = STATIC / "icons" / Path(unquote(p[7:])).name
                if f.suffix == ".png" and f.is_file():
                    return self._send(200, f.read_bytes(), "image/png", cache="public, max-age=86400")
                return self._send(404, b"Not found", "text/plain")
            if p == "/api/tv":
                q = parse_qs(url.query)
                one = lambda k, n=40: q.get(k, [""])[0][:n]  # noqa: E731
                playing = one("playing", 12)
                svc.tvs.touch(one("id"), self.client_address[0], one("name"), one("sound") == "1", one("audio", 16),
                              int(playing) if playing.isdigit() else None)
                return self._json(svc.tv_state(one("id")))
            if p.startswith("/tv/sound/"):
                got = svc.sounds.body(p[10:], phone=parse_qs(url.query).get("phone", [""])[0] == "1")
                if got is None:
                    return self._send(404, b"Not found", "text/plain")
                return self._send(200, got[1], got[0], cache="private, max-age=3600")
            if p == "/ramadan/timetable":   # printable, like the prayer times themselves
                return self._send(200, svc.ramadan_timetable().encode("utf-8"), "text/html; charset=utf-8")
            if p == "/api/ping":
                return self._json({"app": "azan-sip", "version": __version__})
            if p == "/api/session":
                return self._json(self._session_info())
            if p == "/api/state":
                st = svc.state(demo=parse_qs(url.query).get("ramadan", [""])[0] == "demo")
                if not self._authed():  # prayer times are public, the log (and who is connected) is not
                    srv = st.get("sipserver")
                    st.update(log=[], history=[], screens={"on": st["screens"]["on"], "list": []},
                              sipserver=srv and {"status": srv["status"], "detail": srv["detail"],
                                                 "accounts": [{"registered": a["registered"], "kind": a["kind"]}
                                                              for a in srv["accounts"]]})
                return self._json(st)
            if p in ("/live/stream.m3u8", "/live/x") and svc.relay_off:
                return self._json({"ok": False, "error": "Makkah live through AthanBox is turned off on the Display page"}, 403)
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
            if p == "/api/tv-links":   # the TV app page: addresses and QR codes for TV screens
                return self._json(svc.tv_links(self._page_host()))
            if p == "/api/security":   # the Security page: fail2ban, whitelist and blacklist
                if not svc.security:
                    return self._json({"ok": False, "error": "Security settings are only on AthanBox"}, 404)
                return self._json(svc.security.state(self.client_address[0]))
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
        if security.FILTER.blocked(self.client_address[0]) or not self._host_ok() or self.headers.get("X-Azan-Panel") != "1":
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
            if p == "/api/tv/star":   # kids mode: a TV screen, without the password
                if not self._tv_allowed(p):
                    return self._send(403, b"Forbidden", "text/plain")
                req = json.loads(self._body(2_000) or b"{}")
                chart = svc.add_star(str(req.get("id") or ""), str(req.get("prayer") or ""))
                return self._json({"ok": True, **chart})
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
                    raise ValueError("Passwords are only used on AthanBox")
                if not svc.auth.setup_code and not svc.auth.verify(str(req.get("current") or "")):
                    raise ValueError("The current password is wrong")
                svc.auth.set_password(str(req.get("new") or ""))
                svc.auth.logout_others(self._token())
                log.info("Control panel password changed")
                return self._json({"ok": True})
            if p == "/api/factory-reset":
                if not svc.box:
                    raise ValueError("Factory reset is only available on AthanBox")
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
            if p == "/api/kids/screen":   # the TV app page: kids mode on one screen
                svc.set_kids_screen(str(req.get("id") or ""), bool(req.get("on")))
                return self._json({"ok": True, "screens": kids.settings(svc.cfg)["screens"]})
            if p == "/api/screens":
                svc.set_screens(bool(req.get("on")))
                return self._json({"ok": True, **svc.tv_links(self._page_host())})
            if p.startswith("/api/security/") and not svc.security:
                raise ValueError("Security settings are only on AthanBox")
            if p == "/api/security/fail2ban":
                svc.security.set_fail2ban(req)
                return self._json({"ok": True, **svc.security.state(self.client_address[0])})
            if p in ("/api/security/add", "/api/security/remove"):
                which = str(req.get("list") or "")
                if p.endswith("add"):
                    svc.security.add(which, str(req.get("ip") or ""), str(req.get("note") or ""),
                                     self.client_address[0], [box_ip(), *local_ips()])
                else:
                    svc.security.remove(which, str(req.get("ip") or ""))
                return self._json({"ok": True, **svc.security.state(self.client_address[0])})
            if p == "/api/security/unban":
                ips = req.get("ips") if isinstance(req.get("ips"), list) else [req.get("ip")]
                svc.security.request(unban=[str(x) for x in ips if x])
                return self._json({"ok": True})
            if p == "/api/security/refresh":
                svc.security.request()
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
                    raise ValueError("AthanBox can't be quit from the control panel")
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
    allow_reuse_address = True


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
            log.info("AthanBox is already running - opening its control panel")
            if open_browser:
                webbrowser.open(url)
            return 0
        message_box(f"AthanBox could not start: port {port} is used by another program.\n"
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
        log.info("AthanBox mode: control panel open to the network, password needed to change settings")
    log.info("AthanBox %s - control panel at %s", VERSION, url)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    threading.Thread(target=svc.auto_update_checker, name="update-check", daemon=True).start()
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
        log.info("AthanBox stopped")
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="AthanBox")
    ap.add_argument("-c", "--config", default=None)
    ap.add_argument("--background", action="store_true", help="don't open the browser")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    cfg = args.config or str(app_dir() / "config.toml")
    try:
        sys.exit(serve(cfg, open_browser=not args.background, verbose=args.verbose))
    except Exception as exc:
        logging.getLogger("azan-sip").exception("fatal error")
        message_box(f"AthanBox stopped because of an error:\n\n{exc}\n\nSee azan.log for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()
