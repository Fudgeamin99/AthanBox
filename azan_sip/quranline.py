"""Quran by phone: dial an extension on AthanBox to listen to the live recitation from Masjid al-Haram,
or to a Quran playlist.

- Desk phones signed in to AthanBox dial the extension on their AthanBox line; a phone system registered to
  AthanBox sends the number over that trunk. Only devices signed in to AthanBox can call (their own SIP
  password, or the address they are signed in from), like every other part of AthanBox's SIP server.
- The live extension plays the sound of the Makkah live stream (the same link as the prayer-times screen).
- The playlist extension plays the recordings chosen on the Phones page, in order or shuffled, or the whole
  Quran recited by Mishary Alafasy from the internet when none are chosen. Either keeps going until the
  caller hangs up (at most MAX_MINUTES).
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

    def configure(self, cfg: dict | None, audio_dir: Path, live_url: Callable[[], str]):
        self.cfg, self.audio_dir, self.live_url = clean(cfg), Path(audio_dir), live_url

    def which(self, number: str) -> str | None:
        if not self.cfg["enabled"]:
            return None
        return {self.cfg["live_ext"]: "live", self.cfg["playlist_ext"]: "playlist"}.get(number)

    def frames(self, kind: str, codec: str, stop: threading.Event) -> Iterator[bytes]:
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
            for item in order:
                if stop.is_set() or failed >= 3:
                    return
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
        """Feed the sound into a short buffer and send it every 20 ms, with silence while the stream catches up."""
        buf: queue.Queue = queue.Queue(maxsize=150)   # 3 seconds
        done = threading.Event()

        def feed():
            rest = b""
            try:
                for piece in LINE.frames(self.kind, self.codec, self.stop):
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
        silence = audio.silence(rtp.PTIME_MS, self.codec)
        pt, seq, ts, ssrc = CODECS[self.codec], random.randint(0, 0xFFFF), random.randint(0, 0xFFFFFFFF), random.getrandbits(32)
        nxt, first = time.monotonic(), True
        while not self.stop.is_set():
            try:
                frame = buf.get_nowait()
            except queue.Empty:
                if done.is_set():
                    break              # the playlist or the stream has ended
                frame = silence
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
