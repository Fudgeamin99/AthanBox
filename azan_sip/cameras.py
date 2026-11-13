"""Reolink cameras on the prayer-times screen, and a Reolink doorbell that rings the phones.

- Video: AthanBox reads a camera's RTSP stream with ffmpeg and repackages it as HLS, without re-encoding,
  only while a screen is watching it (/camera/<id>/stream.m3u8). Screens play it with Makkah live's player
  and never see the camera's address or password. ffmpeg stops a minute after the last screen stops asking.
- Doorbell: for a camera marked as a doorbell, AthanBox signs in to the camera's own web API and asks it
  every second whether its button was pressed (and, if chosen, whether a person was seen).
The cameras are set on the Display page (config [[cameras]]).
"""
from __future__ import annotations

import base64
import collections
import json
import logging
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import quote

log = logging.getLogger("azan-sip")

MAX_CAMERAS = 8
ID_RE = re.compile(r"^[a-z0-9]{1,12}$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$|^\[?[0-9A-Fa-f:]+\]?$")
SEGMENT_RE = re.compile(r"^seg\d{1,8}\.ts$")
IDLE = 60.0          # seconds without a screen asking before ffmpeg stops
START_WAIT = 15.0    # how long the first screen waits for the first piece of video
POLL = 1.0           # doorbell: seconds between questions to the camera
COOLDOWN = 20.0      # doorbell: one ring per press, however long the camera reports it
API_TIMEOUT = 5


class CameraError(Exception):
    """A problem with a camera, worded for the person using the app."""


def clean(cams) -> list[dict]:
    """The [[cameras]] settings, checked. Each camera keeps a short id that screens use in its address."""
    if not cams:
        return []
    if not isinstance(cams, list):
        raise ValueError("The cameras setting must be a list")
    if len(cams) > MAX_CAMERAS:
        raise ValueError(f"Up to {MAX_CAMERAS} cameras can be added")
    out, ids = [], set()
    for i, c in enumerate(cams, 1):
        if not isinstance(c, dict):
            raise ValueError("Each camera must be a table of settings")
        name = str(c.get("name") or "").strip()[:40] or f"Camera {i}"
        host = re.sub(r"^[a-z]+://", "", str(c.get("host") or "").strip(), flags=re.I).rstrip("/")
        if not host:
            raise ValueError(f"{name}: type the camera's address (its IP address on the network, such as 192.168.1.50)")
        if not HOST_RE.match(host):
            raise ValueError(f"{name}: “{host}” isn't a camera address. Type just its IP address or name, such as 192.168.1.50")
        cid = str(c.get("id") or "").strip().lower()
        if not ID_RE.match(cid) or cid in ids:
            cid = secrets.token_hex(3)
        ids.add(cid)

        def num(key, lo, hi, default):
            try:
                v = int(c.get(key) if c.get(key) not in (None, "") else default)
            except (TypeError, ValueError):
                raise ValueError(f"{name}: {key.replace('_', ' ')} must be a number") from None
            if not lo <= v <= hi:
                raise ValueError(f"{name}: {key.replace('_', ' ')} must be from {lo} to {hi}")
            return v

        ring = c.get("ring") if isinstance(c.get("ring"), list) else []
        out.append({
            "id": cid, "name": name, "host": host,
            "rtsp_port": num("rtsp_port", 1, 65535, 554),
            "channel": num("channel", 1, 64, 1),          # 1 for a camera; the camera's number on a Home Hub or NVR
            "username": str(c.get("username") or "admin").strip(),
            "password": str(c.get("password") or ""),
            "stream": "main" if c.get("stream") == "main" else "sub",
            "screens": c.get("screens", True) is not False,   # a Camera button for it on the screens
            "doorbell": c.get("doorbell") is True,
            "phones": c.get("phones", True) is not False,     # doorbell: ring the phones
            "ring": [str(x) for x in ring if str(x).strip()][:50],   # phone entries to ring (empty: all of them)
            "sound": str(c.get("sound") or ""),                # "" = the built-in chime, else "audio/<recording>"
            "popup": c.get("popup", True) is not False,       # doorbell: show the camera on every screen
            "motion": c.get("motion") is True,                # doorbell: also when a person is seen
        })
    return out


def for_screens(cams: list[dict] | None) -> list[dict]:
    """What screens may know about the cameras: names, and a key that changes with the stream's settings
    (so a screen showing a camera reconnects after it's changed). Never the address or password."""
    out = []
    for c in cams or []:
        if c.get("screens", True):
            key = json.dumps([c["host"], c["rtsp_port"], c["channel"], c["username"], c["password"], c["stream"]])
            out.append({"id": c["id"], "name": c["name"], "doorbell": bool(c.get("doorbell")),
                        "v": base64.urlsafe_b64encode(_digest(key)).decode()[:10]})
    return out


def _digest(text: str) -> bytes:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).digest()


def _rtsp_urls(cam: dict, stream: str | None = None) -> list[str]:
    """Reolink's RTSP addresses for the camera, the likeliest first (h264Preview on most cameras, Preview on newer
    firmware)."""
    auth = f"{quote(cam['username'], safe='')}:{quote(cam['password'], safe='')}@"
    host = f"[{cam['host'].strip('[]')}]" if ":" in cam["host"].strip("[]") else cam["host"]
    ch, st = f"{cam['channel']:02d}", stream or cam["stream"]
    base = f"rtsp://{auth}{host}:{cam['rtsp_port']}"
    return [f"{base}/h264Preview_{ch}_{st}", f"{base}/Preview_{ch}_{st}"]


def _hide(text: str, cam: dict) -> str:
    """ffmpeg's messages without the camera's password."""
    for secret in {cam.get("password") or "", quote(cam.get("password") or "", safe="")} - {""}:
        text = text.replace(secret, "***")
    return text


def friendly(err: str, cam: dict) -> str:
    """ffmpeg's or ffprobe's complaint, in words."""
    e = err.lower()
    if "401" in e or "unauthorized" in e:
        return "The camera refused the username or password"
    if "404" in e or "not found" in e:
        return "The camera has no such stream. If it's on a Home Hub or NVR, check the channel number"
    if "connection refused" in e:
        return (f"The camera at {cam['host']} didn't accept the connection on port {cam['rtsp_port']}. In the Reolink app, "
                "turn on RTSP (Settings, Network, Advanced, Server settings)")
    if "timed out" in e or "timeout" in e or "no route" in e or "unreachable" in e:
        return (f"The camera at {cam['host']} didn't answer. Check its address, and that AthanBox can reach it "
                "(for a camera at another place: port forwarding or Tailscale)")
    if "name or service not known" in e or "failed to resolve" in e:
        return f"The camera address {cam['host']} couldn't be found on the network"
    last = [x for x in _hide(err, cam).strip().splitlines() if x.strip()]
    return last[-1][:200] if last else "The camera's video couldn't be read"


def _shm_dir() -> str | None:
    """Keep the video pieces in memory when there's room (no wear on an SD card)."""
    try:
        st = os.statvfs("/dev/shm")
        if os.access("/dev/shm", os.W_OK) and st.f_bavail * st.f_frsize > 256_000_000:
            return "/dev/shm"
    except OSError:
        pass
    return None


# ------------------------------------------------------------------ video
class _Stream:
    def __init__(self, cam: dict, folder: Path):
        self.cam = cam
        self.folder = folder
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.seen = time.monotonic()
        self.errors: collections.deque[str] = collections.deque(maxlen=20)
        self.url_index = 0
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if not shutil.which("ffmpeg"):
            raise CameraError("ffmpeg isn't installed on this computer, so cameras can't be shown")
        self.stop()
        shutil.rmtree(self.folder, ignore_errors=True)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.errors.clear()
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", *self.source(),
               "-map", "0:v:0", "-an", "-c:v", "copy",   # the camera's own video as it is: light on AthanBox
               "-f", "hls", "-hls_time", "2", "-hls_list_size", "5",
               "-hls_flags", "delete_segments+omit_endlist+independent_segments",
               "-hls_segment_filename", str(self.folder / "seg%05d.ts"), str(self.folder / "stream.m3u8")]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True, errors="replace")
        self.started = time.monotonic()
        threading.Thread(target=self._read_errors, args=(self.proc,), name="camera-ffmpeg", daemon=True).start()
        log.info("Camera %s: started", self.cam["name"])

    def source(self) -> list[str]:
        """ffmpeg's input: the camera's RTSP stream, over TCP (steadier than UDP on Wi-Fi)."""
        return ["-rtsp_transport", "tcp", "-timeout", "10000000", "-i", _rtsp_urls(self.cam)[self.url_index % 2]]

    def _read_errors(self, proc: subprocess.Popen):
        for line in proc.stderr:
            if line.strip():
                self.errors.append(line.strip())

    def stop(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def error(self) -> str:
        return friendly("\n".join(self.errors), self.cam)


class Relay:
    """Each camera's video, while screens are watching it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.streams: dict[str, _Stream] = {}
        self.cams: dict[str, dict] = {}
        self.root: Path | None = None
        self.reaper: threading.Thread | None = None

    def sync(self, cams: list[dict] | None):
        """New settings: a camera that was removed or changed stops (screens watching it start it again)."""
        with self.lock:
            self.cams = {c["id"]: c for c in cams or []}
            for cid, s in list(self.streams.items()):
                if self.cams.get(cid) != s.cam:
                    s.stop()
                    shutil.rmtree(s.folder, ignore_errors=True)
                    del self.streams[cid]

    def _folder(self, cid: str) -> Path:
        if self.root is None:
            self.root = Path(tempfile.mkdtemp(prefix="athanbox-cameras-", dir=_shm_dir()))
        return self.root / cid

    def _stream(self, cid: str) -> _Stream:
        with self.lock:
            cam = self.cams.get(cid)
            if not cam or not cam.get("screens", True):
                raise KeyError(cid)
            s = self.streams.get(cid)
            if s is None:
                s = self.streams[cid] = _Stream(cam, self._folder(cid))
            if self.reaper is None or not self.reaper.is_alive():
                self.reaper = threading.Thread(target=self._reap, name="camera-reaper", daemon=True)
                self.reaper.start()
        s.seen = time.monotonic()
        return s

    def playlist(self, cid: str) -> bytes:
        """The camera's live playlist, starting ffmpeg if no screen was watching. Raises KeyError for an
        unknown camera and CameraError when the camera can't be read."""
        s = self._stream(cid)
        with s.lock:
            if not s.running():
                if s.proc is not None and time.monotonic() - s.started < 5:
                    raise CameraError(s.error())   # it stopped straight away: say why, and let the screen retry
                if s.proc is not None and time.monotonic() - s.started < 20:
                    s.url_index += 1               # the other kind of Reolink address
                s.start()
            deadline = s.started + START_WAIT
            f = s.folder / "stream.m3u8"
            while True:
                try:
                    body = f.read_bytes()
                    if b"#EXTINF" in body:
                        return body
                except OSError:
                    pass
                if not s.running():
                    time.sleep(0.2)   # let its last words arrive
                    raise CameraError(s.error())
                if time.monotonic() > deadline:
                    raise CameraError(f"{s.cam['name']} didn't send any video in {START_WAIT:.0f} seconds")
                time.sleep(0.25)

    def segment(self, cid: str, name: str) -> bytes | None:
        if not SEGMENT_RE.match(name):
            return None
        with self.lock:
            s = self.streams.get(cid)
        if s is None:
            return None
        s.seen = time.monotonic()
        try:
            return (s.folder / name).read_bytes()
        except OSError:
            return None

    def _reap(self):
        while True:
            time.sleep(10)
            with self.lock:
                for cid, s in list(self.streams.items()):
                    if time.monotonic() - s.seen > IDLE:
                        if s.running():
                            log.info("Camera %s: stopped (no screen is showing it)", s.cam["name"])
                        s.stop()
                        shutil.rmtree(s.folder, ignore_errors=True)
                        del self.streams[cid]
                if not self.streams:
                    self.reaper = None
                    return

    def stop_all(self):
        with self.lock:
            for s in self.streams.values():
                s.stop()
            self.streams.clear()
            if self.root:
                shutil.rmtree(self.root, ignore_errors=True)
                self.root = None


# ------------------------------------------------------------------ Reolink's web API (the doorbell)
def _ssl_context() -> ssl.SSLContext:
    # A Reolink camera's own certificate is self-signed, and the camera is on this network (or a VPN):
    # its address is what the owner typed, so the certificate isn't checked.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class ReolinkAPI:
    """Just enough of Reolink's HTTP API to sign in and read the doorbell's button and person detection."""

    def __init__(self, cam: dict):
        self.cam = cam
        self.base: str | None = None
        self.token: str | None = None
        self.lease = 0.0

    def _post(self, base: str, cmd: str, body: list, token: str | None = None) -> list:
        q = f"cmd={cmd}" + (f"&token={token}" if token else "")
        req = urllib.request.Request(f"{base}/api.cgi?{q}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=API_TIMEOUT, context=_ssl_context()) as r:
            data = json.loads(r.read(200_000) or b"[]")
        if not isinstance(data, list) or not data:
            raise CameraError("The camera's web API answered with something unexpected")
        return data

    def login(self):
        host = f"[{self.cam['host'].strip('[]')}]" if ":" in self.cam["host"].strip("[]") else self.cam["host"]
        body = [{"cmd": "Login", "action": 0, "param": {"User": {"Version": "0", "userName": self.cam["username"],
                                                               "password": self.cam["password"]}}}]
        last: Exception | None = None
        # newer cameras answer on https only, older ones on http
        for base in ([self.base] if self.base else [f"https://{host}", f"http://{host}"]):
            try:
                data = self._post(base, "Login", body)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                last = exc
                continue
            d = data[0]
            if d.get("code") != 0:
                detail = (d.get("error") or {}).get("detail") or "refused"
                raise CameraError(f"The camera refused to sign in ({detail}). Check the username and password")
            tok = d["value"]["Token"]
            self.base, self.token = base, str(tok["name"])
            self.lease = time.monotonic() + max(60.0, float(tok.get("leaseTime", 3600)) - 60)
            return
        raise CameraError(friendly(str(getattr(last, "reason", last) or "timed out"), self.cam))

    def events(self) -> dict:
        """{"visitor": bool or None (not reported), "person": bool or None} right now."""
        if not self.token or time.monotonic() > self.lease:
            self.login()
        ch = self.cam["channel"] - 1
        data = self._post(self.base, "GetEvents", [{"cmd": "GetEvents", "action": 0, "param": {"channel": ch}}], self.token)
        d = data[0]
        if d.get("code") != 0:
            code = (d.get("error") or {}).get("rspCode")
            if code in (-6, -1):   # signed out (the camera restarted, or the token ran out)
                self.token = None
                raise CameraError("The camera signed AthanBox out")
            return self._ai_state(ch)   # older firmware: no GetEvents, no doorbell button
        v = d.get("value") or {}
        visitor = v.get("visitor") or {}
        people = (v.get("ai") or {}).get("people") or {}
        return {"visitor": visitor.get("alarm_state") == 1 if visitor.get("support") == 1 else None,
                "person": people.get("alarm_state") == 1 if people.get("support") == 1 else None}

    def _ai_state(self, ch: int) -> dict:
        data = self._post(self.base, "GetAiState", [{"cmd": "GetAiState", "action": 0, "param": {"channel": ch}}], self.token)
        people = ((data[0].get("value") or {}).get("people") or {}) if data[0].get("code") == 0 else {}
        return {"visitor": None, "person": people.get("alarm_state") == 1 if people.get("support") == 1 else None}

    def logout(self):
        if self.token and self.base:
            try:
                self._post(self.base, "Logout", [{"cmd": "Logout", "action": 0, "param": {}}], self.token)
            except Exception:
                pass
        self.token = None


class _Watcher(threading.Thread):
    def __init__(self, cam: dict, pressed: Callable[[dict, str], None]):
        super().__init__(name=f"doorbell-{cam['id']}", daemon=True)
        self.cam, self.pressed = cam, pressed
        self.halt = threading.Event()
        self.api = ReolinkAPI(cam)

    def run(self):
        was = {"visitor": False, "person": False}
        last = {"visitor": 0.0, "person": 0.0}
        wait, warned, told_no_button = POLL, 0.0, False
        while not self.halt.is_set():
            try:
                now = self.api.events()
                if wait > POLL:
                    log.info("Doorbell %s: connected again", self.cam["name"])
                wait = POLL
                if now["visitor"] is None and not told_no_button:
                    told_no_button = True
                    log.warning("Doorbell %s: the camera doesn't report its button to AthanBox. Update its firmware in "
                                "the Reolink app", self.cam["name"])
                for kind in ("visitor", "person"):
                    on = bool(now[kind])
                    if on and not was[kind] and time.monotonic() - last[kind] > COOLDOWN:
                        if kind == "visitor" or self.cam.get("motion"):
                            last[kind] = time.monotonic()
                            try:
                                self.pressed(self.cam, "doorbell" if kind == "visitor" else "person")
                            except Exception:
                                log.exception("Doorbell %s", self.cam["name"])
                    was[kind] = on
            except Exception as exc:
                msg = str(exc) if isinstance(exc, CameraError) else friendly(str(getattr(exc, "reason", exc)), self.cam)
                if time.monotonic() - warned > 600:
                    warned = time.monotonic()
                    log.warning("Doorbell %s: %s. Trying again.", self.cam["name"], msg)
                wait = min(max(wait * 2, 5.0), 60.0)
            self.halt.wait(wait)
        self.api.logout()


class Doorbells:
    """A watcher for each camera marked as a doorbell."""

    def __init__(self, pressed: Callable[[dict, str], None]):
        self.pressed = pressed
        self.watchers: dict[str, _Watcher] = {}
        self.lock = threading.Lock()

    def sync(self, cams: list[dict] | None):
        want = {c["id"]: c for c in cams or [] if c.get("doorbell")}
        with self.lock:
            for cid, w in list(self.watchers.items()):
                if want.get(cid) != w.cam:
                    w.halt.set()
                    del self.watchers[cid]
            for cid, cam in want.items():
                if cid not in self.watchers:
                    w = self.watchers[cid] = _Watcher(cam, self.pressed)
                    w.start()
                    log.info("Doorbell %s: watching its button%s", cam["name"],
                             " and people it sees" if cam.get("motion") else "")

    def stop(self):
        self.sync([])


# ------------------------------------------------------------------ the Test button
def _probe(url: str, cam: dict) -> tuple[dict | None, str]:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "8000000",
                            "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
                            "-of", "json", url], capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    if r.returncode == 0:
        streams = (json.loads(r.stdout or "{}").get("streams") or [])
        if streams:
            return streams[0], ""
        return None, "The camera sent no video"
    return None, r.stderr


def _snapshot(url: str) -> str:
    """One picture from the stream, as a data: link for the Display page."""
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-timeout", "8000000",
                            "-i", url, "-frames:v", "1", "-vf", "scale=480:-2", "-f", "image2", "-c:v", "mjpeg", "-"],
                           capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(r.stdout).decode() if r.returncode == 0 and r.stdout else ""


def test(raw: dict) -> dict:
    """The Test button on the Display page: can AthanBox read this camera (saved or not)?"""
    try:
        cam = clean([raw])[0]
    except ValueError as exc:
        return {"working": False, "summary": str(exc)}
    if not shutil.which("ffprobe"):
        return {"working": False, "summary": "ffmpeg isn't installed on this computer, so cameras can't be shown"}
    host = cam["host"].strip("[]")
    try:
        socket.create_connection((host, cam["rtsp_port"]), timeout=4).close()
    except OSError as exc:
        return {"working": False, "summary": friendly(str(exc) if not isinstance(exc, TimeoutError) else "timed out", cam)}
    info, err = None, ""
    for url in _rtsp_urls(cam):
        info, err = _probe(url, cam)
        if info:
            break
    if not info:
        return {"working": False, "summary": friendly(err, cam)}
    parts, working = [], True
    codec = (info.get("codec_name") or "?").lower()
    size = f"{info.get('width')}×{info.get('height')}" if info.get("width") else ""
    if codec != "h264":
        working = False
        parts.append(f"The {'Clear' if cam['stream'] == 'main' else 'Fluent'} picture is {codec.upper()}, which TV screens "
                     "can't play. Choose Fluent, or set the camera's Clear stream to H.264 in the Reolink app")
    else:
        parts.append("Working" + (f" · {size} video" if size else ""))
    out = {"working": working, "summary": "", "picture": _snapshot(url) if working else ""}
    if cam["doorbell"]:
        api = ReolinkAPI(cam)
        try:
            ev = api.events()
            parts.append("doorbell button: reported to AthanBox" if ev["visitor"] is not None else
                         "this camera doesn't report a doorbell button (update its firmware in the Reolink app)")
            if cam["motion"]:
                parts.append("person detection: " + ("reported" if ev["person"] is not None else "not reported by this camera"))
        except Exception as exc:
            parts.append("doorbell: " + (str(exc) if isinstance(exc, CameraError) else friendly(str(exc), cam)))
        finally:
            api.logout()
    out["summary"] = " · ".join(parts)
    return out
