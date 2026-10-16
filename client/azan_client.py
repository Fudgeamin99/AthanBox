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
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "0.5.2"

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


def watch_screens(every: float = 2, read=wayland_info, restart=restart_browser, rounds: int | None = None):
    """Run by the kiosk inside cage: keeps screens.json up to date as screens are plugged in and out
    (and a laptop's lid is opened and closed).

    When a screen is plugged in or unplugged, the browser is restarted: the kiosk then picks the
    sound output again, so the azan follows the TV, and the new window fills the screens there are
    now (cage before 0.2, in Debian 12, leaves the window the old size when a screen is unplugged:
    the prayer times would be cut off)."""
    last, names = None, None
    while rounds is None or rounds > 0:
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
        if outputs:   # (nothing read: wayland-info failed, so nothing is known to have changed)
            seen = sorted(o["name"] for o in outputs)
            if names is not None and seen != names:
                log("A screen was plugged in or out: restarting the browser, so it fits the screens and the sound follows the TV")
                restart()
            names = seen
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


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ HTTP
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
              "live_sound": c.get("live_sound") if isinstance(c.get("live_sound"), bool) else None}
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
        return self._send(404, b"Not found")

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
            save_config(c)
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
            if wall_needed() and self.headers.get("Sec-Fetch-Dest") != "iframe":
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
        # The screen only shows AthanBox: it never changes its settings
        return self._json({"ok": False, "error": "This screen can't change AthanBox's settings. Use AthanBox's control panel."}, 403)

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
            if (page and r.status == 200 and ctype.startswith("text/html")) or (url.path == "/api/tv" and r.status == 200):
                body = r.read()
                if url.path == "/api/tv":
                    body = self._own_volume(body, c, self._mirror())
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
    def _own_volume(body: bytes, c: dict, mirror: bool = False) -> bytes:
        """The azan plays at this screen's own volume, not AthanBox's (the TV app applies "volume").
        A second screen's copy stays silent, so the azan isn't heard twice."""
        try:
            j = json.loads(body)
            if isinstance(j, dict):
                j["volume"] = 0 if mirror else volume_of(c) / 100
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
        if p in ("/client/api/status", "/client/api/search", "/client/api/update"):
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
        if p in ("/client/api/connect", "/client/api/settings", "/client/api/update", "/client/api/chime"):
            return self._client_post(p, req) if self._signed_in() else self._json(self.SIGN_IN, 401)
        return self._json({"ok": False, "error": "Not found"}, 404)


def serve(port: int = PORT):
    watch = Watch()
    Handler.watch = watch
    Handler.updates = Updates()
    Handler.pairing = Pairing()
    threading.Thread(target=watch.run, daemon=True).start()
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
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        return serve(a.port)
    if a.cmd == "watch-screens":
        return watch_screens()
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
