"""Quran by phone: dial an extension on AthanBox to listen to the live recitation from Masjid al-Haram,
or to a Quran playlist.

- Desk phones signed in to AthanBox dial the extension on their AthanBox line; a phone system registered to
  AthanBox sends the number over that trunk. Only devices signed in to AthanBox can call (their own SIP
  password, or the address they are signed in from), like every other part of AthanBox's SIP server.
- The live extension plays the sound of the Makkah live stream (the same link as the prayer-times screen).
- The playlist extension is like a radio station: it plays all the time, from when AthanBox starts, and a
  caller joins wherever it has got to (hanging up and calling back carries on where it is, not from the
  start). It plays the recordings chosen on the Phones page, in order or shuffled, or the whole Quran
  recited by Mishary Alafasy from the internet when none are chosen.
- Both are shared stations (one download of the live stream however many people listen); the live one runs
  only while someone is listening. Calls end when the caller hangs up (at most MAX_MINUTES).
"""
from __future__ import annotations

import ipaddress
import logging
import queue
import random
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

from . import __version__, audio, rtp
from .audio import CODECS
from .sip import SipMessage, _rand

log = logging.getLogger("azan-sip")

DEFAULT = {"enabled": True, "live_ext": "700", "playlist_ext": "701", "playlist": [], "shuffle": False}
MAX_MINUTES = 180
EXT_RE = re.compile(r"^[0-9*#]{1,12}$")
# When no recordings are chosen: the whole Quran, surah by surah, recited by Mishary Rashid Alafasy
ONLINE = "https://download.quranicaudio.com/quran/mishaari_raashid_al_3afaasee/{:03d}.mp3"
ONLINE_NAME = "The whole Quran, Mishary Alafasy (from the internet)"
UA = "Mozilla/5.0 (compatible; AthanBox; +https://github.com/Fudgeamin99/AthanBox)"
FILTER = "highpass=f=200,lowpass=f=3400,alimiter=limit=0.95"


def clean(cfg: dict | None) -> dict:
    """The [quran_line] settings, checked, with a message for the person saving them."""
    c = dict(DEFAULT, **(cfg if isinstance(cfg, dict) else {}))
    live, pl = str(c.get("live_ext", "")).strip(), str(c.get("playlist_ext", "")).strip()
    for ext, what in ((live, "live recitation"), (pl, "Quran playlist")):
        if not EXT_RE.match(ext):
            raise ValueError(f"The extension for the {what} can use up to 12 digits, * and #")
    if live == pl:
        raise ValueError("Give the live recitation and the Quran playlist different extensions")
    items = c.get("playlist") if isinstance(c.get("playlist"), list) else []
    return {"enabled": c.get("enabled") is not False, "live_ext": live, "playlist_ext": pl,
            "playlist": [Path(str(x)).name for x in items if str(x).strip()][:200],
            "shuffle": bool(c.get("shuffle"))}


class QuranLine:
    """What the extensions play; AthanBox's web service keeps it up to date with the settings."""

    def __init__(self):
        self.cfg = dict(DEFAULT)
        self.audio_dir: Path | None = None
        self.live_url: Callable[[], str] = lambda: ""
        self.active = 0
        self.lock = threading.Lock()
        self.stations: dict[str, Station] = {}

    def configure(self, cfg: dict | None, audio_dir: Path, live_url: Callable[[], str]):
        new = clean(cfg)
        restart = (new["playlist"], new["shuffle"], new["enabled"]) != (self.cfg["playlist"], self.cfg["shuffle"],
                                                                        self.cfg["enabled"]) or self.audio_dir != Path(audio_dir)
        self.cfg, self.audio_dir, self.live_url = new, Path(audio_dir), live_url
        with self.lock:
            if restart and "playlist" in self.stations:   # the playlist changed: the station starts it again
                self.stations.pop("playlist").stop.set()
        if new["enabled"]:
            self.station("playlist")                     # the radio plays even while nobody is listening
        else:
            with self.lock:
                for s in self.stations.values():
                    s.stop.set()
                self.stations.clear()

    def station(self, kind: str) -> "Station":
        """The running station for an extension, started if it isn't (or if the live link has changed)."""
        with self.lock:
            s = self.stations.get(kind)
            if s and (not s.is_alive() or s.stop.is_set() or (kind == "live" and s.url != self.live_url())):
                s.stop.set()
                s = None
            if s is None:
                s = self.stations[kind] = Station(kind, self.live_url() if kind == "live" else "")
                s.start()
            return s

    def now_playing(self) -> str:
        s = self.stations.get("playlist")
        return s.now if s and s.is_alive() else ""

    def which(self, number: str) -> str | None:
        if not self.cfg["enabled"]:
            return None
        return {self.cfg["live_ext"]: "live", self.cfg["playlist_ext"]: "playlist"}.get(number)

    def frames(self, kind: str, codec: str, stop: threading.Event,
               on_item: Callable[[str], None] = lambda name: None) -> Iterator[bytes]:
        """The sound to play, as G.711 bytes in pieces of any size, until stop is set."""
        if kind == "live":
            yield from _ffmpeg(self.live_url(), codec, stop, live=True)
            return
        items = []
        for name in self.cfg["playlist"]:
            p = (self.audio_dir or Path(".")) / name
            if p.is_file():
                items.append(p)
            else:
                log.warning("Quran playlist: %s isn't in the audio folder any more, skipping it", name)
        online = not items
        if online:
            items = [ONLINE.format(n) for n in range(1, 115)]
        while not stop.is_set():
            order = items[:]
            if self.cfg["shuffle"] and not online:
                random.shuffle(order)
            failed, any_played = 0, False   # failed in a row: give up (and hang up) rather than try every surah of an unreachable site
            for i, item in enumerate(order):
                if stop.is_set() or failed >= 3:
                    return
                on_item(item.name if isinstance(item, Path) else f"Surah {items.index(item) + 1}")
                if isinstance(item, Path) and i + 1 < len(order) and isinstance(order[i + 1], Path):
                    nxt = order[i + 1]   # convert the next recording now, so there's no gap before it
                    threading.Thread(target=lambda: _quiet(audio.load, nxt, codec), daemon=True).start()
                played = False
                if isinstance(item, Path):
                    try:
                        data = audio.load(item, codec)
                    except Exception as exc:
                        log.warning("Quran playlist: couldn't play %s: %s", item.name, exc)
                        data = b""
                    for off in range(0, len(data), 8000):
                        if stop.is_set():
                            return
                        played = True
                        yield data[off:off + 8000]
                else:
                    for piece in _ffmpeg(item, codec, stop):
                        played = True
                        yield piece
                failed = 0 if played else failed + 1
                any_played = any_played or played
            if not any_played:
                return


def _ffmpeg(url: str, codec: str, stop: threading.Event, live: bool = False) -> Iterator[bytes]:
    """Stream a link (a live .m3u8, or an mp3) through ffmpeg as 8 kHz G.711."""
    if not shutil.which("ffmpeg"):
        log.warning("Quran by phone needs ffmpeg to play %s", "the live recitation" if live else "from the internet")
        return
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-user_agent", UA]
    if url.startswith("http"):
        cmd += ["-reconnect", "1", "-reconnect_delay_max", "5"]
    cmd += ["-i", url, "-vn", "-ac", "1", "-ar", "8000", "-af", FILTER,
            "-f", "mulaw" if codec == "PCMU" else "alaw", "pipe:1"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        log.warning("Quran by phone: couldn't start ffmpeg: %s", exc)
        return
    got = 0
    try:
        while not stop.is_set():
            chunk = proc.stdout.read(1600)
            if not chunk:
                break
            got += len(chunk)
            yield chunk
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            err = proc.stderr.read(2000).decode(errors="replace").strip()
            proc.wait(timeout=3)
        except Exception:
            err = ""
        if not got and not stop.is_set():
            log.warning("Quran by phone: couldn't play %s%s", "the live recitation" if live else url,
                        f": {err.splitlines()[-1]}" if err else "")


def _quiet(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass


# mu-law (how stations make their sound) to A-law, for phones that only take PCMA
def _ulaw_to_linear(u: int) -> int:
    u = ~u & 0xFF
    t = (((u & 0x0F) << 3) + 0x84) << ((u & 0x70) >> 4)
    return (0x84 - t) if u & 0x80 else (t - 0x84)


TO_ALAW = audio.encode_pcm16([_ulaw_to_linear(u) for u in range(256)], "PCMA")


class Station(threading.Thread):
    """One extension's sound, played in real time for everyone listening, like a radio station: each call
    takes the 20 ms pieces as they go out, joining wherever the station has got to."""
    IDLE_STOP = 30.0   # the live station stops this long after its last listener leaves

    def __init__(self, kind: str, url: str = ""):
        super().__init__(name=f"quran-station-{kind}", daemon=True)
        self.kind, self.url = kind, url
        self.stop = threading.Event()
        self.listeners: set[queue.Queue] = set()
        self.lock = threading.Lock()
        self.now = ""
        self.empty_since = time.monotonic()

    def join_listener(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=50)   # a second
        with self.lock:
            self.listeners.add(q)
        return q

    def leave(self, q: queue.Queue):
        with self.lock:
            self.listeners.discard(q)
            if not self.listeners:
                self.empty_since = time.monotonic()

    def run(self):
        if self.kind == "playlist":
            log.info("Quran by phone: the playlist station is playing")
        fails = 0
        while not self.stop.is_set():
            fails = 0 if self._play() else fails + 1
            if self.stop.is_set() or (self.kind == "live" and (not self.listeners or fails >= 3)):
                break                  # nobody listening, or the live stream can't be reached: callers hang up
            self.stop.wait(5 if self.kind == "live" else 60)   # the stream dropped, or nothing could be played: try again
        self.stop.set()
        with LINE.lock:
            if LINE.stations.get(self.kind) is self:
                del LINE.stations[self.kind]

    def _play(self) -> bool:
        """Play until the source ends. Returns whether any sound came."""
        buf: queue.Queue = queue.Queue(maxsize=150)   # 3 seconds
        done = threading.Event()
        played = False

        def feed():
            rest = b""
            try:
                for piece in LINE.frames(self.kind, "PCMU", self.stop, lambda name: setattr(self, "now", name)):
                    rest += piece
                    while len(rest) >= rtp.FRAME and not self.stop.is_set():
                        frame, rest = rest[:rtp.FRAME], rest[rtp.FRAME:]
                        while not self.stop.is_set():
                            try:
                                buf.put(frame, timeout=0.5)
                                break
                            except queue.Full:
                                continue
            except Exception:
                log.exception("Quran by phone: playing failed")
            finally:
                done.set()

        threading.Thread(target=feed, daemon=True).start()
        nxt = time.monotonic()
        while not self.stop.is_set():
            try:
                frame = buf.get_nowait()
            except queue.Empty:
                if done.is_set():
                    return played      # the stream or the playlist has ended
                frame = None           # still loading: listeners hear silence, and the station waits
            if frame is not None:
                played = True
                with self.lock:
                    targets = list(self.listeners)
                for q in targets:
                    try:
                        q.put_nowait(frame)
                    except queue.Full:   # a listener fell behind: drop its oldest piece
                        _quiet(q.get_nowait)
                        _quiet(q.put_nowait, frame)
            if self.kind == "live" and not self.listeners and time.monotonic() - self.empty_since > self.IDLE_STOP:
                self.stop.set()
                return played
            nxt += rtp.PTIME_MS / 1000
            delay = nxt - time.monotonic()
            if delay > 0:
                self.stop.wait(delay)
            elif delay < -0.5:
                nxt = time.monotonic()
        return played


LINE = QuranLine()


def _sdp_offer(sdp: str) -> tuple[tuple[str, int] | None, list[int]]:
    conn, port, pts = None, None, []
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            conn = line.split()[2].split("/")[0]
        elif line.startswith("m=audio "):
            parts = line.split()
            port = int(parts[1]) if parts[1].isdigit() else None
            pts = [int(p) for p in parts[3:] if p.isdigit()]
    return ((conn, port) if conn and port else None), pts


class InboundCall(threading.Thread):
    """One call to a Quran extension, through AthanBox's SIP server socket: answer, play, hang up."""

    def __init__(self, server, invite: SipMessage, addr: tuple[str, int], kind: str, who: str):
        super().__init__(name=f"quran-{kind}", daemon=True)
        self.server, self.invite, self.addr, self.kind, self.who = server, invite, addr, kind, who
        self.call_id = invite.get("call-id")
        self.q: queue.Queue = queue.Queue()
        server.calls[self.call_id] = self.q
        self.to_tag = _rand(8)
        self.lip = server.local_ip_cfg or rtp.local_ip_for(addr[0])
        self.rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_sock.bind(("0.0.0.0", 0))
        self.rtp_port = self.rtp_sock.getsockname()[1]
        self.stop = threading.Event()
        self.cseq = random.randint(1, 1000)
        m = re.search(r"<([^>]+)>", invite.get("contact"))
        self.remote_target = m.group(1) if m else invite.get("contact").split(";")[0].strip() or None
        self.route_set = [r.strip() for rr in invite.get_all("record-route") for r in rr.split(",")]
        self.dest: tuple[str, int] | None = None
        self.codec = ""

    # ------------------------------------------------------------ SIP
    def _respond(self, req: SipMessage, status: str, sdp: str = ""):
        to = req.get("to")
        if "tag=" not in to:
            to += f";tag={self.to_tag}"
        via = req.get_all("via")
        if via:
            v0 = via[0]
            if "rport" in v0 and not re.search(r"rport=\d", v0):
                v0 = re.sub(r";rport(?=;|$)", f";rport={self.addr[1]}", v0)
            if "received=" not in v0:
                v0 += f";received={self.addr[0]}"
            via = [v0] + via[1:]
        lines = ([f"SIP/2.0 {status}"] + [f"Via: {v}" for v in via]
                 + [f"Record-Route: {r}" for r in (self.route_set if req.method == "INVITE" else [])]
                 + [f"From: {req.get('from')}", f"To: {to}", f"Call-ID: {self.call_id}", f"CSeq: {req.get('cseq')}",
                    f"Contact: <sip:quran@{self.lip}:{self.server.port}>", "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS",
                    f"Server: azan-sip/{__version__}"])
        if sdp:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(sdp.encode())}")
        self.server.send(("\r\n".join(lines) + "\r\n\r\n" + sdp).encode(), self.addr)

    def _sdp(self) -> str:
        return "\r\n".join(["v=0", f"o=azan {random.randint(1, 10**9)} 1 IN IP4 {self.lip}", "s=Quran",
                            f"c=IN IP4 {self.lip}", "t=0 0", f"m=audio {self.rtp_port} RTP/AVP {CODECS[self.codec]}",
                            f"a=rtpmap:{CODECS[self.codec]} {self.codec}/8000", "a=ptime:20", "a=sendrecv"]) + "\r\n"

    def _bye(self):
        if not self.remote_target:
            return
        self.cseq += 1
        h = [f"BYE {self.remote_target} SIP/2.0",
             f"Via: SIP/2.0/UDP {self.lip}:{self.server.port};branch=z9hG4bK{_rand(12)};rport", "Max-Forwards: 70",
             f"From: {self.invite.get('to')}" + ("" if "tag=" in self.invite.get("to") else f";tag={self.to_tag}"),
             f"To: {self.invite.get('from')}", f"Call-ID: {self.call_id}", f"CSeq: {self.cseq} BYE"]
        h += [f"Route: {r}" for r in self.route_set] + [f"User-Agent: azan-sip/{__version__}", "Content-Length: 0"]
        data = ("\r\n".join(h) + "\r\n\r\n").encode()
        for wait in (0.5, 1, 2):   # resend until the phone answers
            self.server.send(data, self.addr)
            end = time.monotonic() + wait
            while time.monotonic() < end:
                try:
                    msg, _ = self.q.get(timeout=max(0.05, end - time.monotonic()))
                except queue.Empty:
                    break
                if not msg.method and msg.get("cseq", "").endswith("BYE"):
                    return

    def _on_message(self, msg: SipMessage) -> bool:
        """A message during the call. Returns False once the call is over."""
        m = msg.method
        if m == "BYE":
            self._respond(msg, "200 OK")
            return False
        if m == "INVITE":                       # a retransmission, or a re-INVITE (hold, new address)
            dest, _ = _sdp_offer(msg.body)
            if dest and msg.get("cseq") != self.invite.get("cseq"):
                self.dest = self._nat(dest)
            self._respond(msg, "200 OK", self._sdp())
        elif m in ("OPTIONS", "INFO", "UPDATE", "NOTIFY"):
            self._respond(msg, "200 OK")
        elif m == "CANCEL":
            self._respond(msg, "200 OK")
        elif m and m != "ACK":
            self._respond(msg, "405 Method Not Allowed")
        return True

    def _nat(self, dest: tuple[str, int]) -> tuple[str, int]:
        try:
            if ipaddress.ip_address(dest[0]).is_private and not ipaddress.ip_address(self.addr[0]).is_private:
                return (self.addr[0], dest[1])
        except ValueError:
            pass
        return dest

    # ------------------------------------------------------------ call
    def run(self):
        LINE.active += 1
        try:
            self._run()
        except Exception:
            log.exception("Quran by phone: the call from %s failed", self.who)
        finally:
            LINE.active -= 1
            self.stop.set()
            self.server.calls.pop(self.call_id, None)
            self.rtp_sock.close()

    def _run(self):
        dest, pts = _sdp_offer(self.invite.body)
        self.codec = next((c for pt in pts for c, n in CODECS.items() if n == pt), "")
        if not dest or not self.codec:
            self._respond(self.invite, "488 Not Acceptable Here")
            log.warning("Quran by phone: %s offered no G.711 audio (PCMU or PCMA), so the call can't be answered", self.who)
            return
        self.dest = self._nat(dest)
        self._respond(self.invite, "100 Trying")
        # answer, resending the 200 OK until the phone's ACK arrives
        sdp, wait, deadline, acked = self._sdp(), 0.5, time.monotonic() + 32, False
        self._respond(self.invite, "200 OK", sdp)
        nxt = time.monotonic() + wait
        while time.monotonic() < deadline and not acked:
            try:
                msg, _ = self.q.get(timeout=max(0.05, nxt - time.monotonic()))
            except queue.Empty:
                self._respond(self.invite, "200 OK", sdp)
                wait = min(wait * 2, 4)
                nxt = time.monotonic() + wait
                continue
            if msg.method == "ACK":
                acked = True
            elif msg.method == "CANCEL":
                self._respond(msg, "200 OK")
                return
            elif msg.method and not self._on_message(msg):
                return
        if not acked:
            log.warning("Quran by phone: %s didn't confirm the call, hanging up", self.who)
            self._bye()
            return
        what = "the live recitation from Makkah" if self.kind == "live" else "the Quran playlist"
        log.info("Quran by phone: %s is listening to %s", self.who, what)
        started = time.monotonic()
        threading.Thread(target=self._play, daemon=True).start()
        threading.Thread(target=self._latch, daemon=True).start()
        ended_by_phone = False
        while not self.stop.is_set():
            if time.monotonic() - started > MAX_MINUTES * 60:
                log.info("Quran by phone: hanging up on %s after %d minutes", self.who, MAX_MINUTES)
                break
            try:
                msg, _ = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self._on_message(msg):
                ended_by_phone = True
                break
        stopped_by_us = self.stop.is_set()
        self.stop.set()
        if not ended_by_phone:
            self._bye()
        log.info("Quran by phone: %s's call ended after %d min%s", self.who, round((time.monotonic() - started) / 60),
                 " (nothing more to play)" if stopped_by_us and not ended_by_phone else "")

    def _play(self):
        """Send the station's sound to the phone every 20 ms, with silence while it waits for more."""
        station = LINE.station(self.kind)
        q = station.join_listener()
        silence, alaw = audio.silence(rtp.PTIME_MS, self.codec), self.codec == "PCMA"
        pt, seq, ts, ssrc = CODECS[self.codec], random.randint(0, 0xFFFF), random.randint(0, 0xFFFFFFFF), random.getrandbits(32)
        nxt, first, primed = time.monotonic(), True, False
        try:
            while not self.stop.is_set():
                primed = primed or q.qsize() >= 3   # a few pieces in hand first, so small timing differences don't click
                frame = silence
                if primed:
                    try:
                        frame = q.get_nowait()
                        if alaw:
                            frame = frame.translate(TO_ALAW)
                    except queue.Empty:
                        primed = False
                pkt = struct.pack("!BBHII", 0x80, (0x80 if first else 0) | pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc) + frame
                first = False
                try:
                    self.rtp_sock.sendto(pkt, self.dest)
                except OSError:
                    pass
                seq, ts = seq + 1, ts + rtp.FRAME
                nxt += rtp.PTIME_MS / 1000
                delay = nxt - time.monotonic()
                if delay > 0:
                    self.stop.wait(delay)
                elif delay < -0.5:
                    nxt = time.monotonic()
                if station.stop.is_set() and q.empty():
                    if self.kind == "live":
                        break              # the live stream couldn't be played: hang up
                    station = LINE.station(self.kind)   # the playlist was changed: listen to the new one
                    q = station.join_listener()
        finally:
            station.leave(q)
        self.stop.set()

    def _latch(self):
        """Symmetric RTP: send to wherever the phone's own audio comes from (behind NAT)."""
        allowed = {self.dest[0], self.addr[0]}
        self.rtp_sock.settimeout(0.3)
        while not self.stop.is_set():
            try:
                _, a = self.rtp_sock.recvfrom(2048)
            except OSError:
                continue
            if a[0] in allowed and a != self.dest:
                self.dest = a
