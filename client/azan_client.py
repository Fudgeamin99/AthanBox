"""AthanBox Client: a TV screen for AthanBox, on a Dell Wyse (or any small PC) running Debian.

The Wyse shows AthanBox's TV app (the prayer-times screen that also plays the azan) full screen on the
TV plugged into it. This program sits between the Wyse's kiosk browser and AthanBox, on
http://127.0.0.1:8771 only:

* Everything the screen asks for is passed on to AthanBox. Only reading is passed on (GET and HEAD),
  so the screen can never change AthanBox's settings.
* The azan's volume on the TV app is this screen's own: it replaces AthanBox's TV volume.
* When AthanBox can't be reached (a restart, an update, the network), the screen shows "Connecting to
  AthanBox" and goes back to the prayer times by itself as soon as AthanBox answers again.
* /client/ is this screen's own settings: which AthanBox, its name on AthanBox's TV app page, the volume
  and updates. The volume also has buttons on the prayer-times screen itself (web/screen.js).
* The same settings open from a phone or computer on the network, on port 8772 (and nothing else
  there: not AthanBox's pages). A browser gets in once by typing a code the TV shows, and stays
  signed in for 90 days. It can be switched off in Screen settings on the TV.
* With more than one screen plugged in (two TVs, or a laptop and a TV), every screen shows the prayer
  times: /client/wall puts one copy of the TV app on each, and only the first one makes a sound.
  "watch-screens" (run by the kiosk inside cage) keeps the list of screens in screens.json.
  A laptop's own screen is left out while its lid is closed, and the TV gets the prayer times alone.
* "update" run as root (azan-sip-client-update.service does, when Screen settings asks for it)
  installs the newest azan-sip-client.deb from the client's own GitHub releases (client-vX.Y.Z).
* "power" run as root (azan-sip-client-shutdown.service does, when Screen settings asks for it)
  shuts the computer down, restarts it, or switches it off for the night with its clock set to
  switch it back on before Fajr (Night sleep). "wifi" run as root (azan-sip-client-wifi.service)
  looks for Wi-Fi networks and joins one. Both take only a request file from the client's folder.
* Screen settings also chooses this screen's sound for each prayer (the azan, a chime or quiet),
  Night sleep, the picture (turned, kept off the TV's edges, its text size) and shows how the
  computer is doing.

Run "azan-sip-client serve" (the azan-sip-client service does). The other commands talk to it:
    azan-sip-client status | search | connect <address> [--name NAME] | volume [0-100] | update
Only Python's standard library is used.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "0.6.0"

PORT = 8771                     # 8770 is the Docker box's updater
REMOTE_PORT = int(os.environ.get("AZAN_CLIENT_REMOTE_PORT", "8772"))   # the settings, from a phone or computer
SESSION_DAYS = 90               # how long a phone or computer stays signed in
CODE_MINUTES = 5                # how long the code on the TV lasts
BOX_PORTS = (80, 8765)          # a box on Debian answers on both; Docker and the desktop app on 8765
CONFIG = Path(os.environ.get("AZAN_CLIENT_CONFIG", "/var/lib/azan-sip-client/client.json"))
WEB = Path(os.environ.get("AZAN_CLIENT_WEB", Path(__file__).resolve().parent / "web"))
CHECK_EVERY = 5                 # seconds between checks on AthanBox
# Response headers passed back to the screen (cookies and AthanBox's frame rules are left out)
PASS_HEADERS = ("content-type", "content-length", "cache-control", "content-disposition", "content-range",
                "accept-ranges", "etag", "last-modified", "location", "x-content-type-options")
INJECT = b'<script src="/client/screen.js"></script>'
VOLUME_STEP = 10                # the volume buttons on the screen
MAX_SCREENS = 8
# Updates: the client has its own releases, tagged client-vX.Y.Z (AthanBox's are box-vX.Y.Z), so a
# client-only change never makes a new Box or Docker version
RELEASES = os.environ.get("AZAN_CLIENT_RELEASES", "https://api.github.com/repos/Fudgeamin99/AthanBox/releases?per_page=100")
TAG_PREFIX = "client-v"
# Where GitHub serves the packages from: under the repository's name, AthanBox, or the name it had
# before (azan-sip-box), which GitHub used until the rename
OFFICIAL = ("https://github.com/Fudgeamin99/AthanBox/releases/download/",
            "https://github.com/Fudgeamin99/azan-sip-box/releases/download/")
DEB_ASSET = re.compile(r"^azan-sip-client_(\d{1,4}(?:\.\d{1,4}){1,3})_all\.deb$")
# Written by azan-sip-client-update.service (root), read by the client and Screen settings
UPDATE_STATE = Path(os.environ.get("AZAN_CLIENT_UPDATE_STATE", "/var/lib/azan-sip-client-update/state.json"))
UPDATE_CACHE = Path("/var/cache/azan-sip-client-update")
# Written by the root services (Shut down, Restart, Night sleep and Wi-Fi), read by the client
POWER_STATE = Path(os.environ.get("AZAN_CLIENT_POWER_STATE", "/var/lib/azan-sip-client-power"))
RTC_ALARM = Path(os.environ.get("AZAN_CLIENT_RTC_ALARM", "/sys/class/rtc/rtc0/wakealarm"))   # switches the computer on
ETC = Path(os.environ.get("AZAN_CLIENT_ETC", "/etc"))
PRAYER_NAMES = ("fajr", "dhuhr", "asr", "maghrib", "isha")
PRAYER_SOUNDS = ("azan", "chime", "quiet")
POWER_FRESH = 120               # seconds: an older request from Screen settings (or one from the future) is ignored


# ------------------------------------------------------------------ settings
def load_config() -> dict:
    try:
        c = json.loads(CONFIG.read_text("utf-8"))
        if isinstance(c, dict):
            return c
    except (OSError, ValueError):
        pass
    return {}


def save_config(c: dict):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, indent=2) + "\n", "utf-8")
    os.replace(tmp, CONFIG)


def write_json(path: Path, obj, mode: int = 0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1) + "\n", "utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    try:
        j = json.loads(path.read_text("utf-8"))
        return j if isinstance(j, dict) else {}
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ settings from a phone or computer
def sessions_file() -> Path:
    return CONFIG.parent / "sessions.json"


def new_session(device: str) -> str:
    """Signs a browser in (after it typed the code on the TV): the token for its cookie."""
    token, now = secrets.token_urlsafe(32), time.time()
    keep = [x for x in read_json(sessions_file()).get("sessions", []) if isinstance(x, dict) and x.get("until", 0) > now]
    keep = keep[-19:] + [{"hash": hashlib.sha256(token.encode()).hexdigest(), "made": now,
                          "until": now + SESSION_DAYS * 86400, "device": str(device)[:80]}]
    write_json(sessions_file(), {"sessions": keep}, 0o600)
    return token


def session_ok(token: str) -> bool:
    if not token:
        return False
    h, now = hashlib.sha256(token.encode()).hexdigest(), time.time()
    return any(isinstance(x, dict) and hmac.compare_digest(str(x.get("hash", "")), h) and x.get("until", 0) > now
               for x in read_json(sessions_file()).get("sessions", []))


def signed_in() -> int:
    now = time.time()
    return sum(1 for x in read_json(sessions_file()).get("sessions", []) if isinstance(x, dict) and x.get("until", 0) > now)


class Pairing:
    """The code a phone or computer types to open this screen's settings: shown on the TV for a few
    minutes. Five wrong tries end a code, and ten in an hour stop new ones for the rest of the hour."""

    def __init__(self):
        self.lock = threading.Lock()
        self.code, self.until, self.tries, self.wrong = None, 0.0, 0, []

    def _recent(self, now: float) -> list:
        self.wrong = [t for t in self.wrong if now - t < 3600]
        return self.wrong

    def start(self) -> bool:
        with self.lock:
            now = time.time()
            if self.code and now < self.until:
                return True
            if len(self._recent(now)) >= 10:
                return False
            self.code, self.until, self.tries = f"{secrets.randbelow(10 ** 6):06d}", now + CODE_MINUTES * 60, 0
            log("A phone or computer asked for this screen's settings: the code is on the TV")
            return True

    def shown(self) -> str | None:
        with self.lock:
            return self.code if self.code and time.time() < self.until else None

    def check(self, code: str) -> bool | None:
        """True: right. False: wrong. None: no code showing (expired, or too many tries)."""
        with self.lock:
            now = time.time()
            if not self.code or now >= self.until:
                return None
            if hmac.compare_digest(re.sub(r"\D", "", str(code))[:6], self.code):
                self.code = None
                return True
            self._recent(now).append(now)
            self.tries += 1
            if self.tries >= 5 or len(self.wrong) >= 10:
                self.code = None
            return False


_remote_url = (0.0, "")


def remote_url() -> str:
    """Where a phone or computer opens this screen's settings, such as http://192.168.1.31:8772/."""
    global _remote_url
    if time.time() - _remote_url[0] > 60:
        nets = local_networks()
        _remote_url = (time.time(), f"http://{nets[0].ip}:{REMOTE_PORT}/" if nets else "")
    return _remote_url[1]


def volume_of(c: dict) -> int:
    try:
        return max(0, min(100, int(c.get("volume", 100))))
    except (TypeError, ValueError):
        return 100


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def prayer_sounds(c: dict) -> dict:
    """What this screen plays for each prayer: "azan", a "chime" instead, or "quiet"."""
    s = c.get("prayer_sounds") if isinstance(c.get("prayer_sounds"), dict) else {}
    return {p: s[p] if s.get(p) in PRAYER_SOUNDS else "azan" for p in PRAYER_NAMES}


def night_settings(c: dict) -> dict:
    """Night sleep: off a while after Isha, on a while before Fajr. "what" sleeps: the TV "screen"
    (the computer stays on), or the whole "computer" (its clock switches it back on)."""
    n = c.get("night") if isinstance(c.get("night"), dict) else {}
    return {"on": n.get("on") is True, "what": "computer" if n.get("what") == "computer" else "screen",
            "after_isha": _int(n.get("after_isha"), 60, 0, 300), "before_fajr": _int(n.get("before_fajr"), 20, 0, 120)}


def picture(c: dict) -> dict:
    """The picture on the TV: turned (0, 90, 180 or 270 degrees), a margin kept off the TV's edges
    (0 to 10 % on each side) and the text size (percent of AthanBox's own)."""
    p = c.get("picture") if isinstance(c.get("picture"), dict) else {}
    r = _int(p.get("rotate"), 0, 0, 270)
    return {"rotate": r if r in (0, 90, 180, 270) else 0, "margin": _int(p.get("margin"), 0, 0, 10),
            "text": _int(p.get("text"), 100, 70, 160)}


def default_name() -> str:
    return (socket.gethostname().split(".")[0] or "Wyse")[:40]


# ------------------------------------------------------------------ addresses
def parse_address(text: str) -> tuple[str, int | None] | None:
    """"192.168.1.20", "azan.local:8765" or "http://192.168.1.20/tv" -> (host, port or None)."""
    s = (text or "").strip()
    s = re.sub(r"(?i)^https?://", "", s).split("/")[0].strip()
    if not s:
        return None
    host, port = s, None
    if s.count(":") == 1:
        host, p = s.split(":")
        if not p.isdigit() or not 0 < int(p) < 65536:
            return None
        port = int(p)
    host = host.lower()
    if not re.fullmatch(r"[a-z0-9.\-]+", host) or host in ("localhost",) or host.startswith("127."):
        return None
    return host, port


def base_url(host: str, port: int) -> str:
    return f"http://{host}" + ("" if port == 80 else f":{port}")


def ping(base: str, timeout: float = 3) -> dict:
    """Asks a server whether it is AthanBox. state: ok, not_allowed, not_azan or unreachable."""
    try:
        with urllib.request.urlopen(urllib.request.Request(base + "/api/ping"), timeout=timeout) as r:
            j = json.loads(r.read(4096).decode("utf-8", "replace"))
            if isinstance(j, dict) and j.get("app") == "azan-sip":
                return {"state": "ok", "version": str(j.get("version", ""))}
            return {"state": "not_azan"}
    except urllib.error.HTTPError as e:
        return {"state": "not_allowed" if e.code == 403 else "not_azan"}
    except (OSError, ValueError, http.client.HTTPException):
        return {"state": "unreachable"}


def find_box(text: str) -> dict:
    """Checks an address someone typed, on the port given or on AthanBox's usual ports."""
    a = parse_address(text)
    if a is None:
        return {"state": "bad_address"}
    host, port = a
    first = None
    for p in ([port] if port else BOX_PORTS):
        r = ping(base_url(host, p))
        r["base"] = base_url(host, p)
        if r["state"] in ("ok", "not_allowed"):
            return r
        first = first or r
    return first


def local_networks() -> list[ipaddress.IPv4Interface]:
    """This computer's own addresses on the local network (with their /24)."""
    out = []
    try:
        text = subprocess.run(["ip", "-o", "-4", "addr", "show", "scope", "global"], capture_output=True,
                              text=True, timeout=5).stdout
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", text):
            out.append(ipaddress.IPv4Interface(f"{m.group(1)}/{max(24, int(m.group(2)))}"))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    if not out:   # no "ip" command: the address the default route goes out on
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))
                out.append(ipaddress.IPv4Interface(s.getsockname()[0] + "/24"))
        except OSError:
            pass
    return [i for i in out if i.ip.is_private]


def search(timeout: float = 0.5) -> list[dict]:
    """Looks for AthanBoxes: azan.local, then every address next to this computer's."""
    mine = {str(i.ip) for i in local_networks()}
    hosts = ["azan.local"]
    for i in local_networks():
        hosts += [str(h) for h in i.network.hosts() if str(h) not in mine]
    hosts = list(dict.fromkeys(hosts))

    def knock(hp):
        host, port = hp
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
        except OSError:
            return None
        r = ping(base_url(host, port))
        return {"base": base_url(host, port), "host": host, "port": port, **r} if r["state"] in ("ok", "not_allowed") else None

    found = {}
    with concurrent.futures.ThreadPoolExecutor(64) as pool:
        for r in pool.map(knock, [(h, p) for h in hosts for p in BOX_PORTS]):
            if r and r["host"] not in found:   # a box answers on 80 and 8765: list it once
                found[r["host"]] = r
    boxes = list(found.values())
    if "azan.local" in found:   # azan.local is one of the addresses found too: keep the name only
        ip = _resolve("azan.local")
        boxes = [b for b in boxes if b["host"] != ip]
    return boxes


def _resolve(host: str) -> str | None:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


# ------------------------------------------------------------------ this computer
def addresses() -> list[dict]:
    """This computer's network addresses: [{"iface", "ip", "kind": "wifi" or "cable"}], cable first."""
    out = []
    try:
        text = subprocess.run(["ip", "-o", "-4", "addr", "show", "scope", "global"], capture_output=True,
                              text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    for m in re.finditer(r"^\d+:\s+([^\s:@]+)\S*\s+inet (\d+\.\d+\.\d+\.\d+)/", text, re.M):
        kind = "wifi" if Path("/sys/class/net", m.group(1), "wireless").exists() else "cable"
        out.append({"iface": m.group(1), "ip": m.group(2), "kind": kind})
    if not out:
        out = [{"iface": "", "ip": str(n.ip), "kind": "cable"} for n in local_networks()]
    return sorted(out, key=lambda a: a["kind"] != "cable")


def wifi_signal(iface: str) -> int | None:
    """The Wi-Fi's strength, 0 to 100, from /proc/net/wireless."""
    try:
        for line in Path("/proc/net/wireless").read_text().splitlines():
            if line.strip().startswith(iface + ":"):
                level = float(line.split()[3].rstrip("."))
                return max(0, min(100, int(2 * (level + 100)))) if level < 0 else max(0, min(100, int(level)))
    except (OSError, IndexError, ValueError):
        pass
    return None


def uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, IndexError, ValueError):
        return 0.0


def health() -> dict:
    """How this computer is doing, for Screen settings: addresses, temperature, storage, memory and
    how long it has been on."""
    temps = []
    for f in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            t = int(f.read_text().strip()) / 1000
            if 0 < t < 150:
                temps.append(t)
        except (OSError, ValueError):
            pass
    try:
        du = shutil.disk_usage("/")
        storage = {"used": du.used, "total": du.total}
    except OSError:
        storage = None
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            if k in ("MemTotal", "MemAvailable"):
                mem[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    nets = addresses()
    for a in nets:
        if a["kind"] == "wifi":
            a["signal"] = wifi_signal(a["iface"])
    return {"addresses": nets, "temperature": round(max(temps)) if temps else None, "storage": storage,
            "memory": {"used": mem["MemTotal"] - mem["MemAvailable"], "total": mem["MemTotal"]}
            if len(mem) == 2 else None, "up": int(uptime()), "last": SEEN.last_sound()}


# ------------------------------------------------------------------ the screens plugged in
INTERNAL = re.compile(r"^(eDP|LVDS|DSI)", re.I)   # a laptop's own screen


def screens_file() -> Path:
    return CONFIG.parent / "screens.json"


def parse_wayland_info(text: str) -> list[dict]:
    """The screens cage shows and where each sits on its desktop, from what wayland-info prints:
    each xdg_output's name, logical_x/_y and logical_width/_height (or, without xdg-output, each
    wl_output's x, y, scale and current mode)."""
    xdg, wl, cur = [], [], None
    for line in text.splitlines():
        t = line.strip()
        if t.startswith("interface:"):
            cur = None
            if "'wl_output'" in t:
                cur = {"kind": "wl", "scale": 1, "w": 0, "h": 0, "name": ""}
                wl.append(cur)
            continue
        if t == "xdg_output_v1":
            cur = {"kind": "xdg", "name": ""}
            xdg.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r"name: '?([^',]*)'?$", t)
        if m and not cur["name"]:
            cur["name"] = m.group(1)
        m = re.match(r"logical_x: (-?\d+), logical_y: (-?\d+)", t)
        if m:
            cur["x"], cur["y"] = int(m.group(1)), int(m.group(2))
        m = re.match(r"logical_width: (\d+), logical_height: (\d+)", t)
        if m:
            cur["w"], cur["h"] = int(m.group(1)), int(m.group(2))
        if cur["kind"] == "wl":
            m = re.match(r"x: (-?\d+), y: (-?\d+), scale: (\d+)", t)
            if m:
                cur["x"], cur["y"], cur["scale"] = int(m.group(1)), int(m.group(2)), max(1, int(m.group(3)))
            m = re.match(r"width: (\d+) px, height: (\d+) px", t)
            if m:
                cur["mode"] = (int(m.group(1)), int(m.group(2)))
            if t.startswith("flags:") and "current" in t and cur.get("mode"):
                cur["w"], cur["h"] = cur["mode"][0] // cur["scale"], cur["mode"][1] // cur["scale"]
    found = [o for o in xdg if "x" in o and o.get("w")] or [o for o in wl if "x" in o and o.get("w")]
    found.sort(key=lambda o: (o["x"], o["y"]))   # left to right: the first plays the azan and shows the settings
    return [{"name": o["name"][:40], "x": o["x"], "y": o["y"], "w": o["w"], "h": o["h"]} for o in found][:MAX_SCREENS]


def lid_closed() -> bool:
    for f in Path("/proc/acpi/button/lid").glob("*/state"):
        try:
            if "closed" in f.read_text():
                return True
        except OSError:
            pass
    return False


def shown_screens(screens: list[dict], closed: bool) -> list[dict]:
    """The screens to show the prayer times on: a laptop's own screen is left out while its lid is
    closed (with a TV plugged in), since nobody sees it."""
    if closed:
        outside = [s for s in screens if not INTERNAL.match(s["name"])]
        if outside:
            return outside
    return screens


def read_screens() -> tuple[list[dict], list[dict]]:
    """(the screens to show the prayer times on, every screen cage spreads the browser over)."""
    j = read_json(screens_file())

    def clean(items):
        out = []
        for s in items if isinstance(items, list) else []:
            try:
                out.append({"name": str(s.get("name", ""))[:40], "x": int(s["x"]), "y": int(s["y"]),
                            "w": max(1, int(s["w"])), "h": max(1, int(s["h"]))})
            except (AttributeError, KeyError, TypeError, ValueError):
                pass
        return out[:MAX_SCREENS]
    shown = clean(j.get("screens"))
    return shown, clean(j.get("outputs")) or shown


def wall_needed() -> bool:
    """The browser is spread over more than one screen (even if one is a closed laptop's), so the
    prayer times go where each screen to show them on is, instead of across them all."""
    shown, outputs = read_screens()
    return len(outputs) > 1 and bool(shown)


def restart_browser():
    """Stops the screen's browser; the kiosk starts it again straight away (kiosk.sh)."""
    me = str(os.getuid())
    for d in Path("/proc").iterdir():
        try:
            if not d.name.isdigit() or (d / "comm").read_text().strip() != "chromium":
                continue
            # The process's own user ("Uid:" real, effective...): /proc shows sandboxed ones as root's
            uid = next(line.split()[1] for line in (d / "status").read_text().splitlines() if line.startswith("Uid:"))
            if uid == me:
                os.kill(int(d.name), signal.SIGTERM)
        except (OSError, ValueError, StopIteration, IndexError):
            pass


def wayland_info() -> str:
    return subprocess.run(["wayland-info"], capture_output=True, text=True, timeout=10).stdout


def display_wanted() -> dict:
    """What the client wants of the screens (asleep for the night, turned): {} when it doesn't answer."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/client/api/display", timeout=3) as r:
            j = json.loads(r.read(10000))
            return j if isinstance(j, dict) else {}
    except (OSError, ValueError, http.client.HTTPException):
        return {}


def screen_power(on: bool) -> str | None:
    """Switches the screens on or off (the TV sees no picture and goes to standby): None when done,
    or why not. Needs wlopm and a cage that lets it (cage 0.2, in Debian 13)."""
    if not shutil.which("wlopm"):
        return "wlopm isn't installed"
    try:
        r = subprocess.run(["wlopm", "--on" if on else "--off", "*"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return None if r.returncode == 0 else ((r.stderr or r.stdout).strip()[:200] or "wlopm didn't work")


def rotate_screens(outputs: list[dict], degrees: int) -> str | None:
    """Turns the picture on every screen but a laptop's own: None when done, or why not. Needs
    wlr-randr and a cage that lets it (cage 0.2, in Debian 13)."""
    if not shutil.which("wlr-randr"):
        return "wlr-randr isn't installed"
    for o in outputs:
        if INTERNAL.match(o["name"]) or not o["name"]:
            continue
        try:
            r = subprocess.run(["wlr-randr", "--output", o["name"], "--transform", str(degrees) if degrees else "normal"],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            return str(e)
        if r.returncode:
            return (r.stderr or r.stdout).strip()[:200] or "wlr-randr didn't work"
    return None


def display_file() -> Path:
    return CONFIG.parent / "display.json"


def watch_screens(every: float = 2, read=wayland_info, restart=restart_browser, rounds: int | None = None,
                  wanted=display_wanted, power=screen_power, rotate=rotate_screens):
    """Run by the kiosk inside cage: keeps screens.json up to date as screens are plugged in and out
    (and a laptop's lid is opened and closed).

    When a screen is plugged in or unplugged, the browser is restarted: the kiosk then picks the
    sound output again, so the azan follows the TV, and the new window fills the screens there are
    now (cage before 0.2, in Debian 12, leaves the window the old size when a screen is unplugged:
    the prayer times would be cut off).

    It also switches the screens off and on for Night sleep, and turns the picture, as the client
    asks (display.json says how that went, for Screen settings). While the screens are off, and for
    a little while after, screens coming and going aren't taken as plugged in or out."""
    last, names, off, woke, turned, done = None, None, False, 0.0, None, {}
    while rounds is None or rounds > 0:
        want = wanted()
        if want and bool(want.get("screen_off")) != off:
            err = power(off)   # (off is still what it was: switch to the other)
            log(("Night sleep: the screen is " + ("on again" if off else "off")) + (f" (only dark: {err})" if err else ""))
            off, woke = not off, time.time()
            done = {**done, "power": err}
            write_json(display_file(), done)
        try:
            outputs = parse_wayland_info(read())
        except (OSError, subprocess.SubprocessError):
            outputs = []
        now = {"screens": shown_screens(outputs, lid_closed()), "outputs": outputs}
        if now != last and (outputs or last is None):   # a failed reading keeps what was known
            write_json(screens_file(), now)
            log("Screens: " + (", ".join(f"{s['name'] or '?'} {s['w']}x{s['h']} at {s['x']},{s['y']}"
                                         + ("" if s in now["screens"] else " (lid closed)") for s in outputs) or "not known"))
            last = now
        settled = not off and time.time() - woke > 15
        if outputs and settled:   # (nothing read: wayland-info failed, so nothing is known to have changed)
            seen = sorted(o["name"] for o in outputs)
            if names is not None and seen != names:
                log("A screen was plugged in or out: restarting the browser, so it fits the screens and the sound follows the TV")
                restart()
            names = seen
            degrees = want.get("rotate") if want else None
            if degrees in (0, 90, 180, 270) and (degrees, seen) != turned and (degrees or turned is not None):
                err = rotate(outputs, degrees)
                log(f"Picture turned {degrees} degrees" if err is None else f"The picture can't be turned here: {err}")
                turned = (degrees, seen)
                done = {**done, "rotate": degrees, "rotate_error": err}
                write_json(display_file(), done)
        if rounds is not None:
            rounds -= 1
        time.sleep(every)


# ------------------------------------------------------------------ updates
def vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4])


def latest_release(timeout: float = 20) -> dict:
    """The newest client on GitHub, from its own releases (not drafts or pre-releases): version, url,
    digest, size and tag."""
    req = urllib.request.Request(RELEASES, headers={"Accept": "application/vnd.github+json",
                                                    "User-Agent": f"azan-sip-client/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read(20_000_000))
    best = None
    for rel in j if isinstance(j, list) else [j]:
        tag = str(rel.get("tag_name", "")) if isinstance(rel, dict) else ""
        if not tag.startswith(TAG_PREFIX) or rel.get("draft") or rel.get("prerelease"):
            continue
        for a in rel.get("assets") or []:
            m = DEB_ASSET.match(str(a.get("name", "")))
            if m and (best is None or vtuple(m.group(1)) > vtuple(best["version"])):
                best = {"version": m.group(1), "url": str(a.get("browser_download_url") or ""),
                        "digest": str(a.get("digest") or ""), "size": int(a.get("size") or 0), "tag": tag}
    if best is None:
        raise ValueError("GitHub has no AthanBox Client release")
    return best


class Updates:
    """What GitHub says is the newest client, asked at most every half hour (or when asked to)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last, self.at = None, 0.0

    def check(self, force: bool = False) -> dict:
        with self.lock:
            if force or self.last is None or time.time() - self.at > 1800:
                try:
                    self.last = {"latest": latest_release()["version"], "error": None}
                except (OSError, ValueError, http.client.HTTPException) as e:
                    log(f"Checking for updates failed: {e}")
                    self.last = {"latest": None, "error": "This screen couldn't reach GitHub to check for updates. Is it connected to the internet?"}
                self.at = time.time()
            return dict(self.last)


def installed_version() -> str:
    try:
        v = subprocess.run(["dpkg-query", "-W", "-f=${Version}", "azan-sip-client"], capture_output=True,
                           text=True, timeout=30).stdout.strip()
        return v or __version__
    except (OSError, subprocess.SubprocessError):
        return __version__


def fetch_deb(rel: dict, folder: Path) -> Path:
    """Downloads the release's package into folder and checks it: the checksum GitHub gives for it,
    and that it is AthanBox Client at that version. Raises ValueError (or OSError) if not."""
    folder.mkdir(mode=0o755, parents=True, exist_ok=True)
    deb = folder / f"azan-sip-client_{rel['version']}_all.deb"
    try:
        sha = hashlib.sha256()
        req = urllib.request.Request(rel["url"], headers={"User-Agent": f"azan-sip-client/{__version__}"})
        with urllib.request.urlopen(req, timeout=120) as r, open(deb, "wb") as f:
            got = 0
            while chunk := r.read(65536):
                got += len(chunk)
                if got > 50_000_000:
                    raise ValueError("the download is far bigger than the client")
                sha.update(chunk)
                f.write(chunk)
        os.chmod(deb, 0o644)   # apt reads it as its own user
        if rel.get("digest", "").startswith("sha256:") and rel["digest"][7:].lower() != sha.hexdigest():
            raise ValueError("the download doesn't match the release's checksum")
        out = subprocess.run(["dpkg-deb", "-f", str(deb), "Package", "Version"], capture_output=True, text=True, timeout=60).stdout
        info = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
        if (info.get("Package"), info.get("Version")) != ("azan-sip-client", rel["version"]):
            raise ValueError("the download isn't AthanBox Client package")
    except BaseException:
        deb.unlink(missing_ok=True)
        raise
    return deb


def self_update(say=print) -> int:
    """As root: downloads the newest azan-sip-client.deb from GitHub and installs it with apt (which
    restarts the client and the screen). Progress goes to UPDATE_STATE for Screen settings."""
    st = {"started": time.time()}

    def step(name: str, **kw):
        st.update(step=name, at=time.time(), **kw)
        write_json(UPDATE_STATE, st)
        if kw.get("message") or kw.get("error"):
            say(kw.get("message") or kw.get("error"))

    if os.geteuid() != 0:
        say("Updating needs root: run it as root (su -), or press Update now in Screen settings.")
        return 1
    step("checking")
    try:
        rel = latest_release(timeout=60)
    except (OSError, ValueError, http.client.HTTPException) as e:
        step("failed", error=f"Couldn't get the update from GitHub ({e}). Check this computer's internet connection.")
        return 1
    have = installed_version()
    if vtuple(rel["version"]) <= vtuple(have):
        step("done", version=have, message=f"Already up to date: AthanBox Client {have}.")
        return 0
    if RELEASES.startswith("https://api.github.com/") and not rel["url"].startswith(OFFICIAL):
        step("failed", error="The update isn't from AthanBox's own releases, so it wasn't installed.")
        return 1
    step("downloading", version=rel["version"], message=f"Downloading AthanBox Client {rel['version']}...")
    try:
        deb = fetch_deb(rel, UPDATE_CACHE)
    except (OSError, ValueError, http.client.HTTPException) as e:
        step("failed", error=f"Downloading the update failed: {e}.")
        return 1
    step("installing", message=f"Installing AthanBox Client {rel['version']}. The screen restarts by itself.")
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    try:
        # New versions may need new Debian packages: refresh the package lists first (a failure
        # here only matters if one is missing)
        subprocess.run(["apt-get", "-o", "DPkg::Lock::Timeout=600", "update"], capture_output=True, timeout=1800, env=env)
        r = subprocess.run(["apt-get", "-y", "-o", "DPkg::Lock::Timeout=600", "-o", "Dpkg::Options::=--force-confdef",
                            "-o", "Dpkg::Options::=--force-confold", "install", str(deb)],
                           capture_output=True, text=True, timeout=3600, env=env)
        why = "" if r.returncode == 0 else " ".join((r.stderr or r.stdout).strip().splitlines()[-3:])[:400]
    except (OSError, subprocess.SubprocessError) as e:
        why = str(e)
    deb.unlink(missing_ok=True)
    if why:
        step("failed", error=f"Installing the update failed: {why}")
        return 1
    step("done", version=rel["version"], message=f"Updated to AthanBox Client {rel['version']}.")
    return 0


TRIGGERS = ("shutdown-now", "restart-now", "sleep-now")   # written by the client, in its folder


def trigger_at(path: Path) -> float | None:
    """When the client wrote a request file, if it's a plain file written in the last two minutes."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st.st_mtime if stat.S_ISREG(st.st_mode) and abs(time.time() - st.st_mtime) <= POWER_FRESH else None


def read_small(path: Path, limit: int = 4096) -> bytes:
    """A request file the client wrote, read as root: never through a link, and only so much of it."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a plain file")
        return os.read(fd, limit)
    finally:
        os.close(fd)


def set_wake_alarm(at: float, rtc: Path = RTC_ALARM):
    """Sets the computer's clock to switch it on at that time (raises OSError if it can't)."""
    if not rtc.exists():
        raise OSError("this computer's clock can't switch it on")
    t = int(at)
    try:   # a clock kept in local time, not UTC (/etc/adjtime says LOCAL)
        if (ETC / "adjtime").read_text().splitlines()[2].strip() == "LOCAL":
            t += time.localtime(at).tm_gmtoff
    except (OSError, IndexError):
        pass
    rtc.write_text("0\n")
    rtc.write_text(f"{t}\n")
    if rtc.read_text().strip() != str(t):
        raise OSError("the clock didn't keep the time to switch on")


def power(say=print, run=lambda cmd: subprocess.run(cmd).returncode, rtc: Path = RTC_ALARM) -> int:
    """As root: does what Screen settings asked for in the last two minutes (the newest request):
    shut down, restart, or switch off for Night sleep until the time sleep-now holds, with the
    computer's clock set to switch it back on then. It reads nothing else from the client."""
    if os.geteuid() != 0:
        say("This needs root: use poweroff or reboot as root, or Screen settings.")
        return 1
    asked = {n: t for n in TRIGGERS if (t := trigger_at(CONFIG.parent / n)) is not None}
    if not asked:
        say("Nothing was asked for in Screen settings in the last two minutes, so the computer stays on.")
        return 0
    what = max(asked, key=asked.get)
    trigger, wake = CONFIG.parent / what, 0.0
    if what == "sleep-now":
        try:
            wake = float(read_small(trigger, 64).decode().strip())
        except (OSError, ValueError, UnicodeDecodeError):
            pass
    trigger.unlink(missing_ok=True)   # done with: each request is acted on once
    if what == "restart-now":
        say("Restart was pressed in Screen settings: restarting the computer.")
        return run(["systemctl", "reboot"])
    if what == "shutdown-now":
        say("Shut down was pressed in Screen settings: turning the computer off.")
        return run(["systemctl", "poweroff"])
    now = time.time()
    try:
        if not now + 100 <= wake <= now + 18 * 3600:
            raise ValueError("the time to switch back on isn't right")
        set_wake_alarm(wake, rtc)
    except (OSError, ValueError) as e:
        write_json(POWER_STATE / "state.json", {"failed": now, "error": str(e)})
        say(f"Night sleep: the computer stays on, since it can't be sure to switch itself back on ({e}).")
        return 1
    write_json(POWER_STATE / "state.json", {"slept": now, "wake": wake})
    say(f"Night sleep: switching off until {time.strftime('%H:%M', time.localtime(wake))}.")
    return run(["systemctl", "poweroff"])


# ------------------------------------------------------------------ Wi-Fi
# Screen settings looks for networks and joins one through azan-sip-client-wifi.service (root), with
# NetworkManager when it runs here (laptops), or else ifupdown and wpa_supplicant (Debian's own,
# without a desktop). Only the Wi-Fi is touched: never the network cable's settings.
WIFI_NAME = "AthanBox Wi-Fi"   # NetworkManager's name for the network joined here


def wireless_ifaces() -> list[str]:
    return sorted(p.parent.name for p in Path("/sys/class/net").glob("*/wireless"))


def wifi_files() -> dict:
    return {"nm": ETC / "NetworkManager/system-connections/athanbox-wifi.nmconnection",
            "iface": ETC / "network/interfaces.d/azan-sip-client-wifi",
            "wpa": ETC / "wpa_supplicant/azan-sip-client-wifi.conf"}


def wifi_psk(ssid: bytes, password: str) -> str:
    """WPA's key for a network and its password, as wpa_passphrase works it out (a password of 64
    hex digits is the key itself)."""
    if re.fullmatch(r"[0-9a-fA-F]{64}", password):
        return password.lower()
    return hashlib.pbkdf2_hmac("sha1", password.encode(), ssid, 4096, 32).hex()


def wifi_password_problem(password: str) -> str | None:
    if re.fullmatch(r"[0-9a-fA-F]{64}", password) or (8 <= len(password) <= 63 and all(32 <= ord(ch) < 127 for ch in password)):
        return None
    return "A Wi-Fi password has 8 to 63 letters, numbers and signs. Check it and try again."


def _iw_bytes(text: str) -> bytes:
    """An SSID as iw prints it, with unusual bytes written \\xNN."""
    return re.sub(rb"\\x([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]), text.encode())


def _best(nets: list[dict]) -> list[dict]:
    """One line per network name (the strongest), the one joined first, then the strongest."""
    best = {}
    for n in nets:
        if not n["ssid"]:   # (hidden networks)
            continue
        b = best.get(n["ssid"])
        if b is None or n["signal"] > b["signal"]:
            best[n["ssid"]] = {**n, "current": n["current"] or bool(b and b["current"])}
        elif n["current"]:
            b["current"] = True
    return sorted(best.values(), key=lambda n: (not n["current"], -n["signal"], n["ssid"].lower()))[:30]


def parse_iw_scan(text: str) -> list[dict]:
    """The networks in "iw dev wlan0 scan": [{"ssid", "signal" (0-100), "secure", "current"}]."""
    nets, cur = [], None
    for line in text.splitlines():
        if line.startswith("BSS "):
            cur = {"ssid": "", "signal": 0, "secure": False, "current": "associated" in line}
            nets.append(cur)
            continue
        t = line.lstrip()
        if cur is None:
            continue
        if t.startswith("SSID: "):
            cur["ssid"] = _iw_bytes(t[6:]).decode("utf-8", "replace")
        elif t.startswith("signal:"):
            m = re.search(r"(-?\d+(?:\.\d+)?) dBm", t)
            if m:
                cur["signal"] = max(0, min(100, int(2 * (float(m.group(1)) + 100))))
        elif t.startswith(("RSN:", "WPA:")) or (t.startswith("capability:") and "Privacy" in t):
            cur["secure"] = True
    return _best(nets)


def _nm_fields(line: str) -> list[str]:
    """One line of nmcli -t: fields split at ":", with "\\:" and "\\\\" inside them."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur, esc = cur + ch, False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out, cur = out + [cur], ""
        else:
            cur += ch
    return out + [cur]


def parse_nmcli_wifi(text: str) -> list[dict]:
    """The networks in "nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY dev wifi list"."""
    nets = []
    for line in text.splitlines():
        f = _nm_fields(line)
        if len(f) >= 4:
            nets.append({"ssid": f[1], "signal": _int(f[2], 0, 0, 100), "secure": f[3].strip() not in ("", "--"),
                         "current": f[0].strip() == "*"})
    return _best(nets)


def nm_keyfile(ssid: bytes, psk: str | None) -> str:
    """NetworkManager's file for the network (the SSID as its bytes, so no name needs escaping)."""
    lines = ["# Written by AthanBox Client (Wi-Fi in Screen settings)", "[connection]", f"id={WIFI_NAME}",
             f"uuid={uuid.uuid5(uuid.NAMESPACE_URL, 'https://github.com/Fudgeamin99/AthanBox/wifi')}", "type=wifi",
             "autoconnect=true", "", "[wifi]", "mode=infrastructure", "ssid=" + "".join(f"{b};" for b in ssid), ""]
    if psk:
        lines += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={psk}", ""]
    return "\n".join(lines + ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""])


def ifupdown_files(iface: str, ssid: bytes, psk: str | None) -> tuple[str, str]:
    """(the interfaces.d file, the wpa_supplicant file) that join the network with ifupdown."""
    head = "# Written by AthanBox Client (Wi-Fi in Screen settings)\n"
    wpa = head + "network={\n" + f"\tssid={ssid.hex()}\n\tscan_ssid=1\n" + (f"\tpsk={psk}\n" if psk else "\tkey_mgmt=NONE\n") + "}\n"
    net = head + f"allow-hotplug {iface}\niface {iface} inet dhcp\n\twpa-conf {wifi_files()['wpa']}\n"
    return net, wpa


def ifupdown_problem(iface: str) -> str | None:
    """Why Wi-Fi can't be set here with ifupdown, if it can't."""
    try:
        text = (ETC / "network/interfaces").read_text()
    except OSError:
        return "This computer has no /etc/network/interfaces, so Wi-Fi can't be set here."
    if re.search(rf"^\s*iface\s+{re.escape(iface)}\s", text, re.M):
        return f"The Wi-Fi ({iface}) is set up in /etc/network/interfaces on this computer: change it there, or take it out there to set it here."
    if not re.search(r"^\s*source(-directory)?\s+/etc/network/interfaces\.d", text, re.M):
        return "/etc/network/interfaces doesn't read /etc/network/interfaces.d, so Wi-Fi can't be set here."
    return None


def wifi_how() -> str | None:
    """"nm" (NetworkManager runs here), "ifupdown" (with wpa_supplicant), or None."""
    if shutil.which("nmcli"):
        try:
            if subprocess.run(["nmcli", "-t", "-f", "RUNNING", "general"], capture_output=True, text=True,
                              timeout=15).stdout.strip() == "running":
                return "nm"
        except (OSError, subprocess.SubprocessError):
            pass
    if (ETC / "network/interfaces").exists() and shutil.which("ifup") and (ETC / "network/if-pre-up.d/wpasupplicant").exists():
        return "ifupdown"
    return None


def _quiet(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def has_address(iface: str, wait: float = 0) -> bool:
    until = time.time() + wait
    while True:
        if " inet " in _quiet(["ip", "-o", "-4", "addr", "show", "dev", iface, "scope", "global"], 10).stdout:
            return True
        if time.time() >= until:
            return False
        time.sleep(1)


def wifi_scan(iface: str, how: str) -> list[dict]:
    if how == "nm":
        r = _quiet(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "ifname", iface, "--rescan", "yes"], 90)
        if r.returncode:
            raise OSError((r.stderr or r.stdout).strip()[:200] or "nmcli didn't work")
        return parse_nmcli_wifi(r.stdout)
    if not shutil.which("iw"):
        raise OSError("the iw program isn't installed (apt install iw)")
    _quiet(["ip", "link", "set", iface, "up"], 15)
    for _ in range(4):
        r = _quiet(["iw", "dev", iface, "scan"], 60)
        if r.returncode == 0:
            return parse_iw_scan(r.stdout)
        time.sleep(3)   # (busy: the Wi-Fi was already looking)
    raise OSError((r.stderr or r.stdout).strip()[:200] or "iw didn't work")


def _put(path: Path, data: bytes | None, mode: int = 0o600):
    """Writes a file (None: removes it)."""
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def wifi_connect(iface: str, how: str, ssid: bytes, psk: str | None) -> str | None:
    """Joins the network: None, or why not. When it can't be joined, things are put back as they were."""
    name, files = ssid.decode("utf-8", "replace"), wifi_files()
    if how == "nm":
        old = files["nm"].read_bytes() if files["nm"].exists() else None
        _put(files["nm"], nm_keyfile(ssid, psk).encode())
        _quiet(["nmcli", "connection", "reload"])
        if _quiet(["nmcli", "--wait", "60", "connection", "up", "id", WIFI_NAME, "ifname", iface], 120).returncode == 0:
            return None
        _put(files["nm"], old)
        _quiet(["nmcli", "connection", "reload"])
        if old is not None:
            _quiet(["nmcli", "--wait", "60", "connection", "up", "id", WIFI_NAME], 120)
        return f"Couldn't join {name}. Check the password, and that this computer is in reach of the Wi-Fi."
    problem = ifupdown_problem(iface)
    if problem:
        return problem
    net, wpa = ifupdown_files(iface, ssid, psk)
    old = {k: files[k].read_bytes() if files[k].exists() else None for k in ("iface", "wpa")}
    _put(files["wpa"], wpa.encode())
    _put(files["iface"], net.encode(), 0o644)
    _quiet(["ifdown", "--force", iface], 60)
    _quiet(["ifup", iface], 120)
    if has_address(iface, 30):
        return None
    _quiet(["ifdown", "--force", iface], 60)
    for k in ("iface", "wpa"):
        _put(files[k], old[k], 0o644 if k == "iface" else 0o600)
    if old["iface"] is not None:
        _quiet(["ifup", iface], 120)
    return f"Couldn't join {name}. Check the password, and that this computer is in reach of the Wi-Fi."


def wifi_forget(iface: str, how: str):
    """Leaves the network joined here, and forgets it."""
    files = wifi_files()
    if how == "nm":
        _quiet(["nmcli", "connection", "delete", "id", WIFI_NAME])
        files["nm"].unlink(missing_ok=True)
        _quiet(["nmcli", "connection", "reload"])
        return
    if files["iface"].exists():
        _quiet(["ifdown", "--force", iface], 60)
    files["iface"].unlink(missing_ok=True)
    files["wpa"].unlink(missing_ok=True)


def wifi_root(say=print) -> int:
    """As root (azan-sip-client-wifi.service): does what Wi-Fi in Screen settings asked for in the
    last two minutes (wifi-now: look for networks, join one, or forget it). How it went goes to
    wifi.json for Screen settings."""
    if os.geteuid() != 0:
        say("This needs root: use Wi-Fi in Screen settings.")
        return 1
    path = CONFIG.parent / "wifi-now"
    asked = trigger_at(path)
    if asked is None:
        return 0
    try:
        req = json.loads(read_small(path))
    except (OSError, ValueError):
        req = None
    path.unlink(missing_ok=True)   # (it may hold a password)
    req = req if isinstance(req, dict) else {}
    out = {"at": time.time(), "asked": asked, "do": str(req.get("do", ""))[:10], "ok": False, "error": None,
           "ours": read_json(POWER_STATE / "wifi.json").get("ours")}   # the network joined here, if any
    ifaces = wireless_ifaces()
    how = wifi_how() if ifaces else None
    out.update(iface=ifaces[0] if ifaces else None, how=how)
    try:
        if not ifaces:
            raise ValueError("This computer has no Wi-Fi.")
        if how is None:
            raise ValueError("Wi-Fi can't be set here: this computer has neither NetworkManager nor ifupdown with wpasupplicant.")
        if out["do"] == "scan":
            out["networks"] = wifi_scan(ifaces[0], how)
        elif out["do"] == "forget":
            wifi_forget(ifaces[0], how)
            out["ours"] = None
        elif out["do"] == "connect":
            name, password, secure = str(req.get("ssid") or ""), str(req.get("password") or ""), req.get("secure") is not False
            ssid = name.encode()
            if not 1 <= len(ssid) <= 32:
                raise ValueError("Choose a Wi-Fi network first.")
            if secure and wifi_password_problem(password):
                raise ValueError(wifi_password_problem(password))
            out["ssid"] = name
            err = wifi_connect(ifaces[0], how, ssid, wifi_psk(ssid, password) if secure else None)
            if err:
                raise ValueError(err)
            out["ours"] = name
        else:
            raise ValueError("Screen settings asked for something unknown.")
        out["ok"] = True
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        out["error"] = str(e)
        say(f"Wi-Fi: {e}")
    write_json(POWER_STATE / "wifi.json", out)
    return 0 if out["ok"] else 1


def wifi_now(iface: str) -> dict:
    """The Wi-Fi network this computer is on now (as the client's own user): name and strength."""
    name = None
    if shutil.which("iw"):
        m = re.search(r"^\s*SSID: (.*)$", _quiet(["iw", "dev", iface, "link"], 10).stdout, re.M)
        name = _iw_bytes(m.group(1)).decode("utf-8", "replace") if m else None
    if name is None and shutil.which("nmcli"):
        for line in _quiet(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"], 10).stdout.splitlines():
            f = _nm_fields(line)
            if len(f) >= 2 and f[0] == "yes":
                name = f[1]
    return {"ssid": name, "signal": wifi_signal(iface) if name else None}


# ------------------------------------------------------------------ AthanBox, checked in the background
class Watch:
    """Checks AthanBox every few seconds, so the screen knows whether it's there."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"state": "none"}
        self.wake = threading.Event()

    def get(self) -> dict:
        with self.lock:
            return dict(self.state)

    def check_now(self):
        base = load_config().get("box")
        r = ping(base) if base else {"state": "none"}
        r["at"] = time.time()
        with self.lock:
            changed = r["state"] != self.state.get("state")
            self.state = r
        if changed:
            log(f"AthanBox {base or '(none set)'} is {r['state']}")
        return r

    def run(self):
        while True:
            try:
                self.check_now()
            except Exception as e:   # never let the checks stop
                log(f"Checking AthanBox failed: {e}")
            self.wake.wait(CHECK_EVERY)
            self.wake.clear()


class Seen:
    """What AthanBox's TV app on this screen asks about and is told twice a second (/api/tv): what plays
    now and next, and the last sound and whether it played here."""

    def __init__(self):
        self.lock = threading.Lock()
        self.at, self.playing, self.next_at, self.last = 0.0, None, None, None

    def update(self, j: dict, query: str, rule: str | None):
        q = urllib.parse.parse_qs(query)
        p = j.get("playing") if isinstance(j.get("playing"), dict) else None
        n = j.get("next") if isinstance(j.get("next"), dict) else None
        with self.lock:
            self.at, self.playing = time.time(), str(p.get("label") or "") if p else None
            self.next_at = n["at"] / 1000 if n and isinstance(n.get("at"), (int, float)) else None
            if p:
                if not self.last or self.last["seq"] != p.get("seq"):
                    self.last = {"seq": p.get("seq"), "label": str(p.get("label") or "")[:40],
                                 "at": (p.get("started") or 0) / 1000 or time.time(), "played": False, "rule": rule}
                if q.get("playing") == [str(p.get("seq"))]:   # the TV app says it's playing it
                    self.last["played"] = True

    def busy(self, now: float) -> bool:
        """Something plays now or within a minute and a half."""
        with self.lock:
            return bool(self.playing and now - self.at < 10) or (self.next_at is not None and -60 < self.next_at - now < 90)

    def last_sound(self) -> dict | None:
        with self.lock:
            return dict(self.last) if self.last else None


def night_window(st: dict, now: float, after_isha: int, before_fajr: int) -> tuple[float, float] | None:
    """The night under way or the next one: (off, on) as times, from AthanBox's prayer times (its
    /api/state): off a while after Isha, on a while before the next Fajr. None without the times."""
    try:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(str(st.get("timezone") or ""))
        except Exception:   # (no zoneinfo, or an unknown name: AthanBox's offset from UTC now)
            tz = datetime.fromisoformat(st["now"]).tzinfo
        day = datetime.fromisoformat(st["now"]).astimezone(tz).date()
        today = {r["name"]: r["time"] for r in st["today"]}
        tomorrow = {r["name"]: r["time"] for r in (st.get("tomorrow") or {}).get("rows") or []}

        def at(d, hm: str) -> float:
            h, m = (int(x) for x in hm.split(":"))
            return datetime(d.year, d.month, d.day, h, m, tzinfo=tz).timestamp()
        one = timedelta(days=1)
        nights = ((at(day - one, today["isha"]) + after_isha * 60, at(day, today["fajr"]) - before_fajr * 60),
                  (at(day, today["isha"]) + after_isha * 60, at(day + one, tomorrow.get("fajr") or today["fajr"]) - before_fajr * 60))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return next((w for w in nights if now < w[1] and w[0] < w[1]), None)


def night_file() -> Path:
    return CONFIG.parent / "night.json"


class Night:
    """Night sleep. With "screen", the TV goes dark for the night (web/screen.js, and watch-screens
    switches the screen off). With "computer", the client asks for the computer to be switched off
    as the night begins (azan-sip-client-shutdown.service sets its clock to switch it back on). The
    screen wakes for anything AthanBox plays at night, and when someone presses a key."""

    def __init__(self, seen: Seen):
        self.seen, self.lock = seen, threading.Lock()
        self.times, self.fetched, self.woken = None, 0.0, 0.0

    def refresh(self, base: str | None):
        """AthanBox's prayer times, asked again every ten minutes (by run, never while answering)."""
        if base and (time.time() - self.fetched > 600 or base != (self.times or {}).get("box")):
            self.fetched = time.time()
            try:
                with urllib.request.urlopen(base + "/api/state", timeout=10) as r:
                    j = json.loads(r.read(5_000_000))
                self.times = {"box": base, **{k: j.get(k) for k in ("now", "timezone", "today", "tomorrow")}}
            except (OSError, ValueError, http.client.HTTPException, AttributeError):
                self.fetched -= 540   # try again in a minute

    def window(self, c: dict, now: float | None = None) -> tuple[float, float] | None:
        n, t = night_settings(c), self.times
        return night_window(t, now or time.time(), n["after_isha"], n["before_fajr"]) if n["on"] and t else None

    def failed(self, w: tuple[float, float]) -> bool:
        """The computer couldn't be switched off for this night (it can't switch itself back on)."""
        return not RTC_ALARM.exists() or read_json(POWER_STATE / "state.json").get("failed", 0) >= w[0] - 60

    def screen_off(self, c: dict, now: float | None = None) -> bool:
        now = now or time.time()
        n = night_settings(c)
        if not n["on"] or now < self.woken:
            return False
        w = self.window(c, now)
        if not w or not w[0] <= now < w[1] or self.seen.busy(now):
            return False
        # A whole computer that sleeps: switched on by hand in the night, it stays on
        return n["what"] == "screen" or self.failed(w)

    def wake(self, minutes: int = 10):
        self.woken = time.time() + minutes * 60

    def tick(self, now: float | None = None):
        """A whole computer that sleeps is switched off as its night begins (once a night)."""
        now = now or time.time()
        c = load_config()
        n = night_settings(c)
        if not n["on"] or n["what"] != "computer":
            return
        w = self.window(c, now)
        if not w or not w[0] <= now < w[0] + 600 or w[1] - now < 300 or self.seen.busy(now) or now < self.woken:
            return
        if read_json(night_file()).get("slept_for") == w[0]:
            return
        write_json(night_file(), {"slept_for": w[0]})
        if not RTC_ALARM.exists():
            log("Night sleep: this computer can't switch itself back on, so only the screen goes dark")
            return
        log(f"Night sleep: switching the computer off until {time.strftime('%H:%M', time.localtime(w[1]))}")
        (CONFIG.parent / "sleep-now").write_text(f"{w[1]:.0f}\n")

    def run(self):
        while True:
            try:
                self.refresh(load_config().get("box"))
                self.tick()
            except Exception as e:   # never let it stop
                log(f"Night sleep: {e}")
            time.sleep(30)

    def status(self, c: dict) -> dict:
        now = time.time()
        n, w = night_settings(c), self.window(c, now)
        st = read_json(POWER_STATE / "state.json")
        last = None
        if st.get("slept"):   # the last time the computer slept: when it switched back on
            booted = now - uptime()
            last = {"slept": st["slept"], "wake": st.get("wake"), "on": booted if booted > st["slept"] else None}
        elif st.get("failed"):
            last = {"failed": st["failed"], "error": st.get("error")}
        return {**n, "off_at": w[0] if w else None, "on_at": w[1] if w else None, "asleep": self.screen_off(c, now),
                "can_wake": RTC_ALARM.exists(), "times": self.times is not None, "last": last}


SEEN = Seen()
NIGHT = Night(SEEN)


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ HTTP
_CHIME: bytes | None = None
# A prayer set to Chime on this screen: the TV app plays this instead of the azan
CHIME_SOUND = {"id": "azc-chime", "url": "/client/chime.wav", "fallback": "/client/chime.wav", "raw": False}


def chime_wav() -> bytes:
    """AthanBox's chime (the four notes of the test chime), as a WAV file."""
    global _CHIME
    if _CHIME is None:
        rate, seg, notes = 22050, 0.6, (659.25, 523.25, 587.33, 392.0)
        step = round(seg * rate)
        data = [0.0] * (step * len(notes) + round(0.6 * rate))
        for k, f in enumerate(notes):
            for i in range(step):
                t = i / rate
                data[k * step + i] = 0.4 * math.exp(-3.5 * t) * min(1, t / 0.004) * math.sin(2 * math.pi * f * t)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(struct.pack(f"<{len(data)}h", *(int(x * 32767) for x in data)))
        _CHIME = buf.getvalue()
    return _CHIME


def sound_rule(label, rules: dict) -> str | None:
    """This screen's own rule for a sound AthanBox plays, by its label ("Fajr azan", "Isha reminder"):
    "quiet" (a prayer set to Quiet: its azan and reminder), "chime" (the azan of a prayer set to
    Chime), or None (as AthanBox plays it: Ramadan's sounds and test chimes too)."""
    words = str(label or "").lower().split()
    if len(words) != 2 or words[0] not in rules or words[1] not in ("azan", "reminder"):
        return None
    rule = rules[words[0]]
    return "quiet" if rule == "quiet" else "chime" if rule == "chime" and words[1] == "azan" else None


class Handler(BaseHTTPRequestHandler):
    watch: Watch = None
    updates: Updates = None
    pairing: Pairing = None
    REMOTE = False                  # True for a phone or computer (RemoteHandler)
    chime_at = 0.0                  # "Play a test chime on the TV", from a phone or computer
    server_version = "azan-sip-client"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype="text/plain; charset=utf-8", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _redirect(self, where: str):
        self._send(302, b"", extra={"Location": where})

    def _ok_request(self) -> bool:
        """Only this computer's own kiosk and commands (no web page elsewhere may use this)."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost"):
            self._send(403, b"Forbidden")
            return False
        return True

    def _status(self) -> dict:
        c, w = load_config(), self.watch.get()
        st = {"state": w["state"], "box_version": w.get("version"), "box": c.get("box"),
              "name": c.get("name") or default_name(), "volume": volume_of(c), "version": __version__,
              **dict(zip(("screens", "outputs"), read_screens())), "remote": self.REMOTE, "chime": Handler.chime_at,
              "live_sound": c.get("live_sound") if isinstance(c.get("live_sound"), bool) else None,
              "prayer_sounds": prayer_sounds(c), "night": NIGHT.status(c), "picture": picture(c),
              "turning": read_json(display_file()), "show_ip": c.get("show_ip", True) is not False,
              "ip": next((a["ip"] for a in addresses()), None)}
        if not self.REMOTE:   # the TV only: the code, and where phones and computers open these settings
            on = c.get("remote", True) is not False and bool(REMOTE_PORT)
            st.update(remote_on=on, remote_url=remote_url() if on else "", signed_in=signed_in(),
                      pair_code=self.pairing.shown() if on and self.pairing else None)
        return st

    def _update_status(self, force: bool) -> dict:
        u = (self.updates or Updates()).check(force)
        return {"current": __version__, **u, "available": bool(u["latest"]) and vtuple(u["latest"]) > vtuple(__version__),
                "update": read_json(UPDATE_STATE)}

    # -- the screen's own pages and settings
    def _client_get(self, p: str, query: str = ""):
        if p in ("/client", "/client/"):
            return self._file("settings.html")
        if p == "/client/connecting":
            return self._file("connecting.html")
        if p == "/client/wall":
            return self._file("wall.html")
        if p == "/client/screen.js":
            return self._file("screen.js")
        if p == "/client/code.js":
            return self._file("code.js")
        if p == "/client/api/status":
            return self._json(self._status())
        if p == "/client/api/search":
            return self._json({"boxes": search()})
        if p == "/client/api/update":
            return self._json(self._update_status("check=1" in query))
        if p == "/client/api/health":
            return self._json(health())
        if p == "/client/api/wifi":
            return self._json(self._wifi())
        if p == "/client/api/display" and not self.REMOTE:   # for watch-screens, in the kiosk
            c = load_config()
            return self._json({"screen_off": NIGHT.screen_off(c), "rotate": picture(c)["rotate"]})
        if p == "/client/chime.wav" and not self.REMOTE:
            return self._send(200, chime_wav(), "audio/wav")
        return self._send(404, b"Not found")

    def _wifi(self) -> dict:
        ifaces = wireless_ifaces()
        return {"iface": ifaces[0] if ifaces else None, **(wifi_now(ifaces[0]) if ifaces else {}),
                "cable": any(a["kind"] == "cable" for a in addresses()),
                "busy": trigger_at(CONFIG.parent / "wifi-now") is not None, "result": read_json(POWER_STATE / "wifi.json")}

    def _file(self, name: str):
        types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
        f = WEB / name
        return self._send(200, f.read_bytes(), types[f.suffix])

    def _body(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= 10000:
                self.close_connection = True   # (left unread)
                return None
            req = json.loads(self.rfile.read(n) or b"{}")
            return req if isinstance(req, dict) else None
        except ValueError:
            return None

    def _client_post(self, p: str, req: dict):
        c = load_config()
        if p == "/client/api/connect":
            r = find_box(str(req.get("address", "")))
            msg = {"bad_address": "That doesn't look like an address. Type AthanBox's IP address, such as 192.168.1.20, or azan.local.",
                   "unreachable": "Nothing answers at that address. Check the address and that AthanBox is switched on and on the same network.",
                   "not_azan": "Something answers at that address, but it isn't an AthanBox."}.get(r["state"])
            if msg:
                return self._json({"ok": False, "error": msg})
            c["box"] = r["base"]
            if str(req.get("name", "")).strip():
                c["name"] = str(req["name"]).strip()[:40]
            save_config(c)
            log(f"Connected to {r['base']}")
            self.watch.check_now()
            return self._json({"ok": True, **self._status(),
                               "warning": None if r["state"] == "ok" else
                               "AthanBox doesn't let TV screens connect yet. In its control panel, open TV app and switch on "
                               "“Let TV screens connect”. This screen then connects by itself."})
        if p == "/client/api/settings":
            if "volume" in req:
                c["volume"] = volume_of({"volume": req["volume"]})
            elif "volume_step" in req:   # the buttons on the screen: one step up or down
                try:
                    c["volume"] = volume_of({"volume": volume_of(c) + VOLUME_STEP * max(-10, min(10, int(req["volume_step"])))})
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "Bad request"}, 400)
            if "name" in req:
                c["name"] = str(req["name"]).strip()[:40]
            if "live_sound" in req:   # Makkah live's sound on this screen (web/screen.js applies it)
                if not isinstance(req["live_sound"], bool):
                    return self._json({"ok": False, "error": "Bad request"}, 400)
                c["live_sound"] = req["live_sound"]
            if "remote" in req and not self.REMOTE:   # switched on and off on the TV only
                c["remote"] = bool(req["remote"])
            for k, now in (("prayer_sounds", prayer_sounds), ("night", night_settings), ("picture", picture)):
                if k in req:   # one part or more of it; anything wrong is left as it was
                    if not isinstance(req[k], dict):
                        return self._json({"ok": False, "error": "Bad request"}, 400)
                    c[k] = now({k: {**now(c), **req[k]}})
            if "show_ip" in req:   # this screen's address on the prayer times
                c["show_ip"] = req["show_ip"] is not False
            save_config(c)
            if "night" in req:   # a new night: it may sleep again, and isn't woken
                night_file().unlink(missing_ok=True)
                NIGHT.woken = 0.0
            return self._json({"ok": True, **self._status()})
        if p == "/client/api/chime":   # the prayer times on the TV play it (web/screen.js)
            Handler.chime_at = time.time()
            return self._json({"ok": True})
        if p == "/client/api/forget" and not self.REMOTE:   # sign every phone and computer out
            write_json(sessions_file(), {"sessions": []}, 0o600)
            log("Every phone and computer was signed out of this screen's settings")
            return self._json({"ok": True, **self._status()})
        if p == "/client/api/update":
            # azan-sip-client-update.path sees this and runs the update as root
            # (azan-sip-client-update.service), which reads nothing from here
            at = time.time()
            (CONFIG.parent / "update-now").write_text(f"{at}\n")
            log("Update asked for in Screen settings")
            return self._json({"ok": True, "asked": at})
        if p in ("/client/api/shutdown", "/client/api/restart", "/client/api/sleep-test"):
            # azan-sip-client-shutdown.path sees these and does it as root (azan-sip-client-shutdown.service),
            # which only looks at when they were written, and at the time to switch back on
            what = p.rsplit("/", 1)[1]
            if what == "sleep-test" and not RTC_ALARM.exists():
                return self._json({"ok": False, "error": "This computer can't switch itself back on, so it can't sleep at night."})
            at = time.time()
            (CONFIG.parent / {"shutdown": "shutdown-now", "restart": "restart-now", "sleep-test": "sleep-now"}[what]).write_text(
                f"{at + 180:.0f}\n" if what == "sleep-test" else f"{at}\n")
            log({"shutdown": "Shut down", "restart": "Restart", "sleep-test": "Night sleep's try (off for 3 minutes)"}[what]
                + " asked for in Screen settings" + (f" from a phone or computer ({self.client_address[0]})" if self.REMOTE else ""))
            return self._json({"ok": True, "at": at})
        if p == "/client/api/wake":   # the screen asleep for the night: awake for a while
            NIGHT.wake()
            return self._json({"ok": True})
        if p == "/client/api/wifi":
            # azan-sip-client-wifi.path sees this and does it as root (azan-sip-client-wifi.service)
            do = req.get("do")
            if do not in ("scan", "connect", "forget"):
                return self._json({"ok": False, "error": "Bad request"}, 400)
            ask = {"do": do}
            if do == "connect":
                ask.update(ssid=str(req.get("ssid") or "")[:64], password=str(req.get("password") or "")[:64],
                           secure=req.get("secure") is not False)
                if ask["secure"] and wifi_password_problem(ask["password"]):
                    return self._json({"ok": False, "error": wifi_password_problem(ask["password"])})
            path = CONFIG.parent / "wifi-now"
            path.unlink(missing_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # (it may hold a password)
            with os.fdopen(fd, "w") as f:
                json.dump(ask, f)
            if do != "scan":
                log(f"Wi-Fi: {'joining ' + ask['ssid'] if do == 'connect' else 'forgetting the network'}, as asked in Screen settings")
            return self._json({"ok": True, "at": time.time()})
        return self._json({"ok": False, "error": "Not found"}, 404)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._ok_request():
            return
        url = urllib.parse.urlsplit(self.path)
        p = url.path
        if p == "/client" or p.startswith("/client/"):
            return self._client_get(p, url.query)
        c = load_config()
        if not c.get("box"):
            return self._redirect("/client/")
        if p == "/":   # where the kiosk starts: the TV app, with this screen's name on AthanBox's list
            # Two screens or more: the prayer times on each (never a wall inside a wall)
            # A picture kept off the TV's edges: the wall leaves the margin round it
            if (wall_needed() or picture(c)["margin"]) and self.headers.get("Sec-Fetch-Dest") != "iframe":
                return self._redirect("/client/wall")
            return self._redirect("/tv?" + urllib.parse.urlencode({"name": c.get("name") or default_name()}))
        self._proxy(c, url)

    def do_POST(self):
        req = self._body()   # (read before answering, even a refusal, or it's taken for the next request)
        if not self._ok_request():
            return
        p = urllib.parse.urlsplit(self.path).path
        if p.startswith("/client/api/"):
            return self._client_post(p, req) if req is not None else self._json({"ok": False, "error": "Bad request"}, 400)
        if p == "/api/tv/star":   # kids mode's "Did you pray?": a star on this screen's chart, nothing else
            c = load_config()
            if not c.get("box") or req is None:
                return self._json({"ok": False, "error": "Bad request"}, 400)
            return self._star(c, req)
        # The screen only shows AthanBox: it never changes its settings
        return self._json({"ok": False, "error": "This screen can't change AthanBox's settings. Use AthanBox's control panel."}, 403)

    def _star(self, c: dict, req: dict):
        base = urllib.parse.urlsplit(c["box"])
        body = json.dumps({"id": str(req.get("id") or "")[:40], "prayer": str(req.get("prayer") or "")[:10]}).encode()
        try:
            con = http.client.HTTPConnection(base.hostname, base.port or 80, timeout=15)
            con.request("POST", "/api/tv/star", body, {"Host": base.netloc, "User-Agent": f"azan-sip-client/{__version__}",
                                                       "Content-Type": "application/json", "X-Azan-Panel": "1", "Connection": "close"})
            r = con.getresponse()
            out = r.read(20_000)
            con.close()
        except (OSError, http.client.HTTPException):
            return self._send(502, b"AthanBox can't be reached")
        return self._send(r.status, out, r.getheader("Content-Type", "application/json"))

    # -- passing the screen's requests on to AthanBox
    def _proxy(self, c: dict, url):
        base = urllib.parse.urlsplit(c["box"])
        page = url.path in ("/tv", "/display")
        try:
            con = http.client.HTTPConnection(base.hostname, base.port or 80, timeout=15)
            headers = {"Host": base.netloc, "User-Agent": f"azan-sip-client/{__version__}", "Accept-Encoding": "identity",
                       "Connection": "close"}
            for h in ("Accept", "Range", "If-None-Match", "If-Modified-Since", "Accept-Language"):
                if self.headers.get(h):
                    headers[h] = self.headers[h]
            con.request(self.command, url.path + ("?" + url.query if url.query else ""), headers=headers)
            r = con.getresponse()
        except (OSError, http.client.HTTPException):
            self.watch.wake.set()   # check again straight away
            if page:   # the prayer times can't be shown: say so, and come back by itself
                return self._redirect("/client/connecting?to=" + urllib.parse.quote(url.path + ("?" + url.query if url.query else ""), safe=""))
            return self._send(502, b"AthanBox can't be reached")
        if page and r.status == 403:   # "Let TV screens connect" is off: say so, and connect when it's on
            r.read()
            con.close()
            self.watch.wake.set()
            return self._redirect("/client/connecting?to=" + urllib.parse.quote(url.path + ("?" + url.query if url.query else ""), safe=""))
        try:
            ctype = r.getheader("Content-Type", "")
            if (page and r.status == 200 and ctype.startswith("text/html")) or (url.path in ("/api/tv", "/api/state") and r.status == 200):
                body = r.read()
                if url.path == "/api/tv":
                    body = self._own_volume(body, c, self._mirror(), url.query)
                elif url.path == "/api/state":
                    body = self._own_text_size(body, c)
                else:
                    body = body.replace(b"</body>", INJECT + b"</body>", 1) if b"</body>" in body else body + INJECT
                return self._send(r.status, body, ctype, {k: v for k, v in r.getheaders()
                                                          if k.lower() in PASS_HEADERS and k.lower() not in ("content-type", "content-length", "cache-control")})
            self.send_response(r.status)
            length = r.getheader("Content-Length")
            for k, v in r.getheaders():
                if k.lower() in PASS_HEADERS:
                    self.send_header(k, v)
            if length is None:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (OSError, http.client.HTTPException):
            self.close_connection = True
        finally:
            con.close()

    def _mirror(self) -> bool:
        """Asked by a copy of the TV app on a second screen (/client/wall gives those mirror=1)."""
        ref = urllib.parse.urlsplit(self.headers.get("Referer") or "")
        return urllib.parse.parse_qs(ref.query).get("mirror") == ["1"]

    @staticmethod
    def _own_volume(body: bytes, c: dict, mirror: bool = False, query: str = "") -> bytes:
        """The azan plays at this screen's own volume, not AthanBox's (the TV app applies "volume"),
        and as this screen plays each prayer: quiet, or a chime instead of the azan. A second
        screen's copy stays silent, so the azan isn't heard twice."""
        try:
            j = json.loads(body)
        except ValueError:
            return body
        if not isinstance(j, dict):
            return body
        rules = prayer_sounds(c)
        p, n = (j.get(k) if isinstance(j.get(k), dict) else None for k in ("playing", "next"))
        rule = sound_rule(p.get("label"), rules) if p else None
        j["volume"] = 0 if mirror or rule == "quiet" else volume_of(c) / 100
        if rule == "chime":
            p["sound"] = CHIME_SOUND
        if n and sound_rule(n.get("label"), rules) == "chime":   # (the TV app downloads what plays next early)
            n["sound"] = CHIME_SOUND
        if not mirror:
            SEEN.update(j, query, rule)
        return json.dumps(j).encode()

    @staticmethod
    def _own_text_size(body: bytes, c: dict) -> bytes:
        """The prayer times' text at this screen's own size: a share of AthanBox's ("look"'s scale)."""
        pct = picture(c)["text"]
        try:
            j = json.loads(body) if pct != 100 else None
            look = j.get("look") if isinstance(j, dict) else None
            if isinstance(look, dict):
                look["scale"] = max(50, min(250, round(_int(look.get("scale"), 100, 10, 400) * pct / 100)))
                return json.dumps(j).encode()
        except ValueError:
            pass
        return body


class RemoteHandler(Handler):
    """This screen's settings from a phone or computer on the network (REMOTE_PORT): the settings page
    and its API only, never AthanBox's pages, for browsers signed in with the code shown on the TV."""
    REMOTE = True
    SIGN_IN = {"ok": False, "signin": True, "error": "Type the code shown on the TV first."}

    def _signed_in(self) -> bool:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "azc" and session_ok(v):
                return True
        return False

    def _switched_on(self) -> bool:
        if load_config().get("remote", True) is False:
            self._send(403, "Changing this screen's settings from a phone or computer is switched off. "
                            "Switch it on in Screen settings on the TV.".encode())
            return False
        return True

    def do_GET(self):
        if not self._switched_on():
            return
        url = urllib.parse.urlsplit(self.path)
        p = url.path
        if p in ("/", "/client", "/client/"):
            return self._file("settings.html" if self._signed_in() else "signin.html")
        if p == "/client/code.js":   # (Screen settings loads it; it does nothing here)
            return self._file("code.js")
        if p in ("/client/api/status", "/client/api/search", "/client/api/update", "/client/api/health", "/client/api/wifi"):
            return self._client_get(p, url.query) if self._signed_in() else self._json(self.SIGN_IN, 401)
        return self._send(404, b"Not found")

    def do_POST(self):
        req = self._body()   # (read before answering, even a refusal, or it's taken for the next request)
        if not self._switched_on():
            return
        p = urllib.parse.urlsplit(self.path).path
        # Only this screen's own pages may post here: JSON (which a page elsewhere can't send without
        # asking first), and from this address
        origin, host = self.headers.get("Origin"), self.headers.get("Host", "")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json" \
                or (origin and origin != f"http://{host}"):
            return self._json({"ok": False, "error": "Forbidden"}, 403)
        if req is None:
            return self._json({"ok": False, "error": "Bad request"}, 400)
        if p == "/client/api/signin/code":   # show a code on the TV
            if self.pairing.start():
                return self._json({"ok": True, "minutes": CODE_MINUTES})
            return self._json({"ok": False, "error": "Too many wrong codes. Try again in an hour."})
        if p == "/client/api/signin":
            ok = self.pairing.check(str(req.get("code", "")))
            if ok is None:
                return self._json({"ok": False, "expired": True, "error": "That code isn't showing any more. Press Show the code again for a new one."})
            if not ok:
                return self._json({"ok": False, "error": "That isn't the code on the TV. Check it and try again."})
            token = new_session(self.headers.get("User-Agent") or "")
            log(f"A phone or computer ({self.client_address[0]}) signed in to this screen's settings")
            return self._send(200, b'{"ok": true}', "application/json", {
                "Set-Cookie": f"azc={token}; Max-Age={SESSION_DAYS * 86400}; Path=/; HttpOnly; SameSite=Strict"})
        if p in ("/client/api/connect", "/client/api/settings", "/client/api/update", "/client/api/chime",
                 "/client/api/shutdown", "/client/api/restart", "/client/api/sleep-test", "/client/api/wake", "/client/api/wifi"):
            return self._client_post(p, req) if self._signed_in() else self._json(self.SIGN_IN, 401)
        return self._json({"ok": False, "error": "Not found"}, 404)


def serve(port: int = PORT):
    watch = Watch()
    Handler.watch = watch
    Handler.updates = Updates()
    Handler.pairing = Pairing()
    threading.Thread(target=watch.run, daemon=True).start()
    threading.Thread(target=NIGHT.run, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    c = load_config()
    log(f"AthanBox Client {__version__} on http://127.0.0.1:{port}, box: {c.get('box') or 'not chosen yet'}")
    if REMOTE_PORT:
        try:
            remote = ThreadingHTTPServer(("0.0.0.0", REMOTE_PORT), RemoteHandler)
            remote.daemon_threads = True
            threading.Thread(target=remote.serve_forever, daemon=True).start()
            log(f"Screen settings from a phone or computer: {remote_url() or f'port {REMOTE_PORT}'}"
                + ("" if c.get("remote", True) is not False else " (switched off in Screen settings)"))
        except OSError as e:
            log(f"Screen settings from a phone or computer aren't available: port {REMOTE_PORT}: {e}")
    server.serve_forever()


# ------------------------------------------------------------------ the command line
def call(path: str, body: dict | None = None, port: int = PORT) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")
    except OSError:
        sys.exit("AthanBox Client isn't running. Start it with: sudo systemctl start azan-sip-client")


STATES = {"ok": "connected", "not_allowed": "found, but “Let TV screens connect” is off on AthanBox",
          "unreachable": "can't be reached", "not_azan": "isn't an AthanBox", "none": "not chosen yet"}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="azan-sip-client", description="AthanBox's prayer times and azan on this computer's screen")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the client (the azan-sip-client service does this)")
    s.add_argument("--port", type=int, default=PORT)
    sub.add_parser("status", help="which AthanBox this screen shows, and whether it's connected")
    sub.add_parser("search", help="look for AthanBoxes on the network")
    c = sub.add_parser("connect", help="show this AthanBox's prayer times on this screen")
    c.add_argument("address", help="AthanBox's address, such as 192.168.1.20 or azan.local")
    c.add_argument("--name", help="this screen's name on AthanBox's TV app page")
    v = sub.add_parser("volume", help="show or set the azan's volume on this screen")
    v.add_argument("percent", nargs="?", type=int, help="0 to 100")
    u = sub.add_parser("update", help="install the newest AthanBox Client from GitHub (as root)")
    u.add_argument("--check", action="store_true", help="only say whether there is a newer version")
    sub.add_parser("watch-screens", help=argparse.SUPPRESS)
    sub.add_parser("power", help=argparse.SUPPRESS)      # (azan-sip-client-shutdown.service)
    sub.add_parser("shutdown", help=argparse.SUPPRESS)   # (its name in 0.5.3 to 0.5.4)
    sub.add_parser("wifi", help=argparse.SUPPRESS)       # (azan-sip-client-wifi.service)
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        return serve(a.port)
    if a.cmd == "watch-screens":
        return watch_screens()
    if a.cmd in ("power", "shutdown"):
        sys.exit(power())
    if a.cmd == "wifi":
        sys.exit(wifi_root())
    if a.cmd == "update":
        if a.check or os.geteuid() != 0:
            try:
                rel = latest_release()
            except (OSError, ValueError, http.client.HTTPException) as e:
                sys.exit(f"Couldn't check for updates: {e}")
            have = installed_version()
            if vtuple(rel["version"]) <= vtuple(have):
                print(f"AthanBox Client {have} is the newest version.")
                return
            print(f"AthanBox Client {rel['version']} is available (this computer has {have}).")
            if not a.check:
                print("To install it, run this as root (su -), or press Update now in Screen settings.")
            return
        sys.exit(self_update())
    if a.cmd == "search":
        print("Looking for AthanBoxes on the network...")
        boxes = call("/client/api/search")["boxes"]
        for b in boxes:
            print(f"  {b['host']:<18} {STATES.get(b['state'], b['state'])}  (version {b.get('version') or '?'})")
        print("Connect with: azan-sip-client connect <address>" if boxes else
              "None found. Is AthanBox switched on and on the same network? You can also type its address: azan-sip-client connect <address>")
        return
    if a.cmd == "connect":
        r = call("/client/api/connect", {"address": a.address, **({"name": a.name} if a.name else {})})
        if not r.get("ok"):
            sys.exit(r.get("error") or "Couldn't connect")
        print(f"This screen now shows {r['box']}.")
        if r.get("warning"):
            print(r["warning"])
        return
    if a.cmd == "volume":
        if a.percent is not None:
            if not 0 <= a.percent <= 100:
                sys.exit("The volume is 0 to 100")
            call("/client/api/settings", {"volume": a.percent})
        print(f"Azan volume on this screen: {call('/client/api/status')['volume']}%")
        return
    st = call("/client/api/status")
    print(f"AthanBox Client {st['version']}")
    print(f"Box:    {st.get('box') or '-'} ({STATES.get(st['state'], st['state'])})" + (f", AthanBox {st['box_version']}" if st.get("box_version") else ""))
    print(f"Name:   {st['name']}")
    print(f"Volume: {st['volume']}%")
    if isinstance(st.get("live_sound"), bool):
        print(f"Makkah live sound: {'on' if st['live_sound'] else 'off'}")
    if st.get("remote_on"):
        print(f"Phones and computers: {st.get('remote_url') or f'port {REMOTE_PORT}'} (with the code the TV shows)")
    else:
        print("Phones and computers: switched off in Screen settings")


if __name__ == "__main__":
    main()
