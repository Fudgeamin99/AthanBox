"""Azan SIP Client: a TV screen for the Azan SIP Box, on a Dell Wyse (or any small PC) running Debian.

The Wyse shows the box's TV app (the prayer-times screen that also plays the azan) full screen on the
TV plugged into it. This program sits between the Wyse's kiosk browser and the box, on
http://127.0.0.1:8771 only:

* Everything the screen asks for is passed on to the box. Only reading is passed on (GET and HEAD),
  so the screen can never change the box's settings.
* The azan's volume on the TV app is this screen's own: it replaces the box's TV volume.
* When the box can't be reached (a restart, an update, the network), the screen shows "Connecting to
  the Azan SIP Box" and goes back to the prayer times by itself as soon as the box answers again.
* /client/ is this screen's own settings: which box, its name on the box's TV app page, and the volume.

Run "azan-sip-client serve" (the azan-sip-client service does). The other commands talk to it:
    azan-sip-client status | search | connect <address> [--name NAME] | volume [0-100]
Only Python's standard library is used.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import ipaddress
import json
import os
import re
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

__version__ = "0.1.0"

PORT = 8771                     # 8770 is the Docker box's updater
BOX_PORTS = (80, 8765)          # a box on Debian answers on both; Docker and the desktop app on 8765
CONFIG = Path(os.environ.get("AZAN_CLIENT_CONFIG", "/var/lib/azan-sip-client/client.json"))
WEB = Path(os.environ.get("AZAN_CLIENT_WEB", Path(__file__).resolve().parent / "web"))
CHECK_EVERY = 5                 # seconds between checks on the box
# Response headers passed back to the screen (cookies and the box's frame rules are left out)
PASS_HEADERS = ("content-type", "content-length", "cache-control", "content-disposition", "content-range",
                "accept-ranges", "etag", "last-modified", "location", "x-content-type-options")
INJECT = b'<script src="/client/screen.js"></script>'


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
    """Asks a server whether it is Azan SIP. state: ok, not_allowed, not_azan or unreachable."""
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
    """Checks an address someone typed, on the port given or on the box's usual ports."""
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
    """Looks for Azan SIP boxes: azan.local, then every address next to this computer's."""
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


# ------------------------------------------------------------------ the box, checked in the background
class Watch:
    """Checks the box every few seconds, so the screen knows whether it's there."""

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
            log(f"The box {base or '(none set)'} is {r['state']}")
        return r

    def run(self):
        while True:
            try:
                self.check_now()
            except Exception as e:   # never let the checks stop
                log(f"Checking the box failed: {e}")
            self.wake.wait(CHECK_EVERY)
            self.wake.clear()


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    watch: Watch = None
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
        return {"state": w["state"], "box_version": w.get("version"), "box": c.get("box"),
                "name": c.get("name") or default_name(), "volume": volume_of(c), "version": __version__}

    # -- the screen's own pages and settings
    def _client_get(self, p: str):
        if p in ("/client", "/client/"):
            return self._file("settings.html")
        if p == "/client/connecting":
            return self._file("connecting.html")
        if p == "/client/screen.js":
            return self._file("screen.js")
        if p == "/client/api/status":
            return self._json(self._status())
        if p == "/client/api/search":
            return self._json({"boxes": search()})
        return self._send(404, b"Not found")

    def _file(self, name: str):
        types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
        f = WEB / name
        return self._send(200, f.read_bytes(), types[f.suffix])

    def _client_post(self, p: str):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}") if n <= 10000 else {}
            if not isinstance(req, dict):
                raise ValueError
        except ValueError:
            return self._json({"ok": False, "error": "Bad request"}, 400)
        c = load_config()
        if p == "/client/api/connect":
            r = find_box(str(req.get("address", "")))
            msg = {"bad_address": "That doesn't look like an address. Type the box's IP address, such as 192.168.1.20, or azan.local.",
                   "unreachable": "Nothing answers at that address. Check the address and that the box is switched on and on the same network.",
                   "not_azan": "Something answers at that address, but it isn't an Azan SIP Box."}.get(r["state"])
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
                               "The box doesn't let TV screens connect yet. In its control panel, open TV app and switch on "
                               "“Let TV screens connect”. This screen then connects by itself."})
        if p == "/client/api/settings":
            if "volume" in req:
                c["volume"] = volume_of({"volume": req["volume"]})
            if "name" in req:
                c["name"] = str(req["name"]).strip()[:40]
            save_config(c)
            return self._json({"ok": True, **self._status()})
        return self._json({"ok": False, "error": "Not found"}, 404)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._ok_request():
            return
        url = urllib.parse.urlsplit(self.path)
        p = url.path
        if p == "/client" or p.startswith("/client/"):
            return self._client_get(p)
        c = load_config()
        if not c.get("box"):
            return self._redirect("/client/")
        if p == "/":   # where the kiosk starts: the TV app, with this screen's name on the box's list
            return self._redirect("/tv?" + urllib.parse.urlencode({"name": c.get("name") or default_name()}))
        self._proxy(c, url)

    def do_POST(self):
        if not self._ok_request():
            return
        p = urllib.parse.urlsplit(self.path).path
        if p.startswith("/client/api/"):
            return self._client_post(p)
        # The screen only shows the box: it never changes its settings
        return self._json({"ok": False, "error": "This screen can't change the box's settings. Use the box's control panel."}, 403)

    # -- passing the screen's requests on to the box
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
            return self._send(502, b"The Azan SIP Box can't be reached")
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
                    body = self._own_volume(body, c)
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

    @staticmethod
    def _own_volume(body: bytes, c: dict) -> bytes:
        """The azan plays at this screen's own volume, not the box's (the TV app applies "volume")."""
        try:
            j = json.loads(body)
            if isinstance(j, dict):
                j["volume"] = volume_of(c) / 100
                return json.dumps(j).encode()
        except ValueError:
            pass
        return body


def serve(port: int = PORT):
    watch = Watch()
    Handler.watch = watch
    threading.Thread(target=watch.run, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    c = load_config()
    log(f"Azan SIP Client {__version__} on http://127.0.0.1:{port}, box: {c.get('box') or 'not chosen yet'}")
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
        sys.exit("The Azan SIP Client isn't running. Start it with: sudo systemctl start azan-sip-client")


STATES = {"ok": "connected", "not_allowed": "found, but “Let TV screens connect” is off on the box",
          "unreachable": "can't be reached", "not_azan": "isn't an Azan SIP Box", "none": "not chosen yet"}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="azan-sip-client", description="The Azan SIP Box's prayer times and azan on this computer's screen")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the client (the azan-sip-client service does this)")
    s.add_argument("--port", type=int, default=PORT)
    sub.add_parser("status", help="which box this screen shows, and whether it's connected")
    sub.add_parser("search", help="look for Azan SIP Boxes on the network")
    c = sub.add_parser("connect", help="show this box's prayer times on this screen")
    c.add_argument("address", help="the box's address, such as 192.168.1.20 or azan.local")
    c.add_argument("--name", help="this screen's name on the box's TV app page")
    v = sub.add_parser("volume", help="show or set the azan's volume on this screen")
    v.add_argument("percent", nargs="?", type=int, help="0 to 100")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        return serve(a.port)
    if a.cmd == "search":
        print("Looking for Azan SIP Boxes on the network...")
        boxes = call("/client/api/search")["boxes"]
        for b in boxes:
            print(f"  {b['host']:<18} {STATES.get(b['state'], b['state'])}  (version {b.get('version') or '?'})")
        print("Connect with: azan-sip-client connect <address>" if boxes else
              "None found. Is the box switched on and on the same network? You can also type its address: azan-sip-client connect <address>")
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
    print(f"Azan SIP Client {st['version']}")
    print(f"Box:    {st.get('box') or '-'} ({STATES.get(st['state'], st['state'])})" + (f", Azan SIP {st['box_version']}" if st.get("box_version") else ""))
    print(f"Name:   {st['name']}")
    print(f"Volume: {st['volume']}%")


if __name__ == "__main__":
    main()
