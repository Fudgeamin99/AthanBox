"""Minimal SIP user agent (UDP) that places a call, plays audio over RTP and hangs up.

Two uses:
  * Direct-to-phone: INVITE sip:192.168.1.50 with auto-answer headers, so the
    phone answers on speaker by itself (phone must allow direct IP calls).
  * Via PBX: INVITE a paging/intercom group extension (e.g. sip:*80@pbx) using
    an extension's username/password (digest auth), and the PBX pages the phones.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import random
import re
import socket
import string
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import rtp
from .audio import CODECS

log = logging.getLogger(__name__)

COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length",
           "c": "content-type"}

AUTO_ANSWER_HEADERS = [
    "Call-Info: <sip:{host}>;answer-after=0",   # Yealink, Grandstream, Fanvil, Snom, Cisco MPP
    "Alert-Info: info=alert-autoanswer;delay=0",  # Grandstream / Fanvil
    "Alert-Info: <http://www.notused.com>;info=alert-autoanswer",
    "Answer-Mode: Auto",                         # RFC 5373
]


def _rand(n=10):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


@dataclass
class SipMessage:
    first: str
    headers: list[tuple[str, str]]
    body: str

    @property
    def status(self) -> int:
        m = re.match(r"SIP/2\.0\s+(\d{3})", self.first)
        return int(m.group(1)) if m else 0

    @property
    def method(self) -> str:
        return "" if self.first.startswith("SIP/2.0") else self.first.split(" ", 1)[0]

    def get(self, name: str, default: str = "") -> str:
        name = name.lower()
        for k, v in self.headers:
            if k == name:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name.lower()]

    @classmethod
    def parse(cls, data: bytes) -> "SipMessage":
        text = data.decode("utf-8", errors="replace")
        head, _, body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        headers = []
        for line in lines[1:]:
            if line[:1] in (" ", "\t") and headers:  # folded header
                k, v = headers[-1]
                headers[-1] = (k, v + " " + line.strip())
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip().lower()
                k = COMPACT.get(k, k)
                headers.append((k, v.strip()))
        return cls(lines[0], headers, body)


def parse_uri(uri: str) -> tuple[str, str, int]:
    """sip:user@host:port;params -> (user, host, port)"""
    u = uri.strip()
    if u.startswith("<"):
        u = u[1:u.index(">")]
    u = re.sub(r"^sips?:", "", u)
    u = u.split(";", 1)[0].split("?", 1)[0]
    user, _, hostport = u.rpartition("@")
    host, _, port = hostport.partition(":")
    return user, host, int(port) if port else 5060


def _digest(challenge: str, method: str, uri: str, username: str, password: str) -> str:
    params = {k.lower(): (a or b) for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', challenge)}
    realm, nonce = params.get("realm", ""), params.get("nonce", "")
    algo = params.get("algorithm", "MD5")
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
    ha1 = md5(f"{username}:{realm}:{password}")
    cnonce, nc = _rand(12), "00000001"
    if algo.upper() == "MD5-SESS":
        ha1 = md5(f"{ha1}:{nonce}:{cnonce}")
    ha2 = md5(f"{method}:{uri}")
    out = [f'Digest username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    qop = params.get("qop")
    if qop and "auth" in [q.strip() for q in qop.split(",")]:
        resp = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        out += [f'response="{resp}"', "qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    else:
        out.append(f'response="{md5(f"{ha1}:{nonce}:{ha2}")}"')
        if algo.upper() == "MD5-SESS":
            out.append(f'cnonce="{cnonce}"')
    out.append(f"algorithm={algo}")
    if "opaque" in params:
        out.append(f'opaque="{params["opaque"]}"')
    return ", ".join(out)


@dataclass
class SipTarget:
    uri: str
    username: str = ""
    password: str = ""
    auth_username: str = ""        # if different from username
    from_user: str = "azan"
    display_name: str = "Azan"
    auto_answer: bool = True
    codecs: list[str] = field(default_factory=lambda: ["PCMU", "PCMA"])
    ring_timeout: float = 20.0
    local_ip: str | None = None
    sip_port: int = 0              # 0 = any free port
    extra_headers: list[str] = field(default_factory=list)


class SipCall:
    def __init__(self, target: SipTarget, get_audio: Callable[[str], bytes], stop: threading.Event | None = None,
                 transport=None, settled: threading.Event | None = None):
        """transport: send and receive through AthanBox's own SIP server socket (phones that signed in to it)
        instead of a socket of this call's own. settled: set once the phone has answered, turned the call
        down or stopped ringing (and on any failure), so the next call to the same device can start."""
        self.t = target
        self.settled = settled or threading.Event()
        self.answered = threading.Event()   # the phone said 200 OK
        self.heard = 0                      # RTP packets that came back from the phone
        self.get_audio = get_audio
        self.stop = stop or threading.Event()
        self.transport = transport
        self.user, self.host, self.port = parse_uri(target.uri)
        self.remote = transport.remote if transport else (socket.gethostbyname(self.host), self.port)
        self.lip = target.local_ip or rtp.local_ip_for(self.remote[0])
        if transport:
            self.sock = None
            self.lport = transport.port
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("0.0.0.0", target.sip_port))
            self.lport = self.sock.getsockname()[1]
        self.rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_sock.bind(("0.0.0.0", 0))
        self.rtp_port = self.rtp_sock.getsockname()[1]
        self.call_id = f"{_rand(16)}@{self.lip}"
        if transport:
            transport.attach(self.call_id)
        self.from_domain = self.host if target.username else self.lip
        self.from_tag = _rand(8)
        self.to_tag = ""
        self.cseq = 0
        self.ruri = target.uri if target.uri.startswith("sip") else f"sip:{target.uri}"
        self.ruri = self.ruri.split(">")[0].lstrip("<")
        self.remote_target = self.ruri
        self.route_set: list[str] = []
        self.ended_by_remote = threading.Event()
        self.stop_stream = threading.Event()
        self._watching = False

    # ------------------------------------------------------------- messages
    def _sdp(self) -> str:
        pts = [CODECS[c] for c in self.t.codecs if c in CODECS]
        lines = ["v=0", f"o=azan {random.randint(1, 10**9)} 1 IN IP4 {self.lip}", "s=Azan",
                 f"c=IN IP4 {self.lip}", "t=0 0",
                 f"m=audio {self.rtp_port} RTP/AVP {' '.join(map(str, pts))}"]
        for c in self.t.codecs:
            if c in CODECS:
                lines.append(f"a=rtpmap:{CODECS[c]} {c}/8000")
        # sendrecv, not sendonly: phones show sendonly as "Held", and their audio lets us find them behind NAT
        lines += ["a=ptime:20", "a=sendrecv"]
        return "\r\n".join(lines) + "\r\n"

    def _request(self, method: str, cseq: int, branch: str, body: str = "", extra: list[str] | None = None,
                 in_dialog: bool = False) -> bytes:
        uri = self.remote_target if in_dialog else self.ruri
        to = f"<{self.ruri}>" + (f";tag={self.to_tag}" if in_dialog and self.to_tag else "")
        h = [f"{method} {uri} SIP/2.0",
             f"Via: SIP/2.0/UDP {self.lip}:{self.lport};branch={branch};rport",
             "Max-Forwards: 70",
             f'From: "{self.t.display_name}" <sip:{self.t.from_user}@{self.from_domain}>;tag={self.from_tag}',
             f"To: {to}",
             f"Call-ID: {self.call_id}",
             f"CSeq: {cseq} {method}",
             f"Contact: <sip:{self.t.from_user}@{self.lip}:{self.lport}>",
             "User-Agent: azan-sip/1.0"]
        for r in self.route_set if in_dialog else []:
            h.append(f"Route: {r}")
        h += extra or []
        if body:
            h.append("Content-Type: application/sdp")
        h.append(f"Content-Length: {len(body.encode())}")
        return ("\r\n".join(h) + "\r\n\r\n" + body).encode()

    def _send(self, data: bytes, addr=None):
        if self.transport:
            self.transport.send(data, addr or self.remote)
        else:
            self.sock.sendto(data, addr or self.remote)

    def _recv(self, timeout: float) -> SipMessage | None:
        if self.transport:
            got = self.transport.recv(timeout)
            if got is None:
                return None
            msg, addr = got
        else:
            self.sock.settimeout(max(timeout, 0.01))
            try:
                data, addr = self.sock.recvfrom(65535)
            except (socket.timeout, TimeoutError):
                return None
            except ConnectionResetError:  # Windows: ICMP port unreachable
                return None
            if not data.strip():
                return None  # keep-alive
            msg = SipMessage.parse(data)
        if msg.method:  # request from the far end
            self._handle_request(msg, addr)
            return None
        if msg.get("call-id") != self.call_id:
            return None
        return msg

    def _handle_request(self, msg: SipMessage, addr):
        resp_status = "200 OK"
        if msg.method == "ACK":
            return
        if msg.method == "BYE" and msg.get("call-id") == self.call_id:
            self.ended_by_remote.set()
            self.stop_stream.set()
        elif msg.method == "INVITE":
            resp_status = "488 Not Acceptable Here"
        resp = [f"SIP/2.0 {resp_status}"]
        for v in msg.get_all("via"):
            resp.append(f"Via: {v}")
        resp += [f"From: {msg.get('from')}", f"To: {msg.get('to')}", f"Call-ID: {msg.get('call-id')}",
                 f"CSeq: {msg.get('cseq')}", "Content-Length: 0"]
        self._send(("\r\n".join(resp) + "\r\n\r\n").encode(), addr)

    def _transaction(self, method: str, body: str = "", extra: list[str] | None = None,
                     in_dialog: bool = False, final_timeout: float = 32.0) -> tuple[SipMessage | None, str, int]:
        """Send a request with retransmissions until a final response. Returns (response, branch, cseq)."""
        self.cseq += 1
        cseq, branch = self.cseq, "z9hG4bK" + _rand(12)
        data = self._request(method, cseq, branch, body, extra, in_dialog)
        self._send(data)
        t1, interval = 0.5, 0.5
        next_tx = time.monotonic() + interval
        provisional = False
        deadline = time.monotonic() + final_timeout
        while time.monotonic() < deadline:
            if method == "INVITE" and self.stop.is_set():
                self._cancel(branch, cseq)
                return self._after_cancel(branch, cseq), branch, cseq
            msg = self._recv(min(next_tx - time.monotonic(), 0.25) if not provisional else 0.25)
            if msg is None:
                if not provisional and time.monotonic() >= next_tx:
                    self._send(data)
                    interval = min(interval * 2, 4.0)
                    next_tx = time.monotonic() + interval
                continue
            if not msg.get("cseq", "").endswith(method) or int(msg.get("cseq").split()[0]) != cseq:
                if msg.status >= 200 and msg.get("cseq", "").endswith("INVITE"):
                    self._ack(msg)  # retransmitted 200 for earlier INVITE
                continue
            if msg.status < 200:
                provisional = True
                if msg.status in (180, 183):
                    log.debug("%s: ringing", self.t.uri)
                continue
            return msg, branch, cseq
        if method == "INVITE":   # stop it ringing, rather than leave the phone (or a DECT base) busy with it
            self._cancel(branch, cseq)
            return self._after_cancel(branch, cseq), branch, cseq
        return None, branch, cseq

    def _after_cancel(self, branch: str, cseq: int, wait: float = 2.0) -> SipMessage | None:
        """After a CANCEL: wait briefly for the INVITE's final answer and ACK it (normally 487). Returns a
        200 OK that crossed the CANCEL (the phone answered just then), else None."""
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            msg = self._recv(0.25)
            if msg is None or not msg.get("cseq", "").endswith("INVITE") or msg.status < 200:
                continue
            if int(msg.get("cseq").split()[0]) != cseq:
                continue
            if msg.status < 300:
                return msg
            self._ack(msg, branch)
            return None
        return None

    def _cancel(self, branch: str, cseq: int):
        h = [f"CANCEL {self.ruri} SIP/2.0",
             f"Via: SIP/2.0/UDP {self.lip}:{self.lport};branch={branch};rport",
             "Max-Forwards: 70",
             f'From: "{self.t.display_name}" <sip:{self.t.from_user}@{self.from_domain}>;tag={self.from_tag}',
             f"To: <{self.ruri}>", f"Call-ID: {self.call_id}", f"CSeq: {cseq} CANCEL", "Content-Length: 0"]
        self._send(("\r\n".join(h) + "\r\n\r\n").encode())

    def _ack(self, resp: SipMessage, branch: str | None = None):
        cseq_num = int(resp.get("cseq").split()[0])
        if resp.status >= 300:  # ACK for non-2xx is hop-by-hop, same branch, To from response
            h = [f"ACK {self.ruri} SIP/2.0",
                 f"Via: SIP/2.0/UDP {self.lip}:{self.lport};branch={branch};rport",
                 "Max-Forwards: 70",
                 f'From: "{self.t.display_name}" <sip:{self.t.from_user}@{self.from_domain}>;tag={self.from_tag}',
                 f"To: {resp.get('to')}", f"Call-ID: {self.call_id}", f"CSeq: {cseq_num} ACK",
                 "Content-Length: 0"]
            self._send(("\r\n".join(h) + "\r\n\r\n").encode())
        else:
            self._send(self._request("ACK", cseq_num, "z9hG4bK" + _rand(12), in_dialog=True))

    # ----------------------------------------------------------------- call
    def run(self) -> bool:
        """Place the call, play audio, hang up. Returns True if audio was played."""
        try:
            return self._run()
        except Exception:
            log.exception("%s: call failed", self.t.uri)
            return False
        finally:
            self.settled.set()
            if self.sock:
                self.sock.close()
            if self.transport:
                self.transport.close()
            self.rtp_sock.close()

    def _run(self) -> bool:
        extra = list(self.t.extra_headers)
        if self.t.auto_answer:
            extra += [h.format(host=self.host) for h in AUTO_ANSWER_HEADERS]
        body = self._sdp()
        resp, branch, _ = self._transaction("INVITE", body, extra, final_timeout=self.t.ring_timeout)
        tried_auth = False
        while resp is not None and resp.status in (401, 407) and not tried_auth:
            self._ack(resp, branch)
            if not self.t.username:
                log.error("%s: PBX requires authentication; set username/password", self.t.uri)
                return False
            hdr = "WWW-Authenticate" if resp.status == 401 else "Proxy-Authenticate"
            ah = "Authorization" if resp.status == 401 else "Proxy-Authorization"
            cred = _digest(resp.get(hdr), "INVITE", self.ruri, self.t.auth_username or self.t.username,
                           self.t.password)
            tried_auth = True
            resp, branch, _ = self._transaction("INVITE", body, extra + [f"{ah}: {cred}"],
                                                final_timeout=self.t.ring_timeout)
        if resp is not None and 200 <= resp.status < 300:
            self.answered.set()
        self.settled.set()
        if resp is None:
            log.warning("%s: no answer", self.t.uri)
            return False
        if resp.status >= 300:
            self._ack(resp, branch)
            log.warning("%s: call rejected: %s", self.t.uri, resp.first)
            return False

        # 2xx: establish dialog
        m = re.search(r";\s*tag=([^;>\s]+)", resp.get("to"))
        self.to_tag = m.group(1) if m else ""
        contact = resp.get("contact")
        if contact:
            mm = re.search(r"<([^>]+)>", contact)
            self.remote_target = mm.group(1) if mm else contact.split(";")[0].strip()
        self.route_set = list(reversed([r.strip() for rr in resp.get_all("record-route") for r in rr.split(",")]))
        self._ack(resp)
        self.answered.set()

        dest, codec = self._parse_answer(resp.body)
        if not dest:
            log.error("%s: answer had no usable audio stream", self.t.uri)
            self._bye()
            return False
        dest = self._nat_dest(dest)
        log.info("%s: answered, streaming %s to %s:%s", self.t.uri, codec, *dest)
        payload = self.get_audio(codec)

        self._watching = True
        watcher = threading.Thread(target=self._watch, daemon=True)
        watcher.start()
        def _relay_stop():
            while not self.stop_stream.is_set():
                if self.stop.wait(0.5):
                    self.stop_stream.set()
        threading.Thread(target=_relay_stop, daemon=True).start()
        dests = [dest]
        threading.Thread(target=self._latch, args=(dests,), daemon=True).start()
        rtp.stream(payload, dests, CODECS[codec], self.stop_stream, sock=self.rtp_sock)
        done = self.ended_by_remote.is_set()
        self.stop_stream.set()
        self._watching = False
        watcher.join(timeout=1)
        if done:
            log.info("%s: far end hung up", self.t.uri)
        else:
            self._bye()
        log.debug("%s: %d audio packets came back from the phone", self.t.uri, self.heard)
        return True

    def hang_up(self):
        """End this call early (BYE), while the others carry on."""
        self.stop_stream.set()

    def _watch(self):
        while self._watching and not self.stop_stream.is_set():
            msg = self._recv(0.3)
            if msg is not None and msg.status == 200 and msg.get("cseq", "").endswith("INVITE"):
                self._ack(msg)

    def _nat_dest(self, dest: tuple[str, int]) -> tuple[str, int]:
        """A phone behind NAT (say one signed in to a box in the cloud) gives its private address in the
        SDP, which can't be reached: send to the public address its SIP messages came from instead."""
        try:
            private = ipaddress.ip_address(dest[0]).is_private
            public_remote = not ipaddress.ip_address(self.remote[0]).is_private
        except ValueError:
            return dest
        if private and public_remote:
            log.info("%s: the phone is behind NAT (%s), sending the audio to %s:%s", self.t.uri, dest[0],
                     self.remote[0], dest[1])
            return (self.remote[0], dest[1])
        return dest

    def _latch(self, dests: list[tuple[str, int]]):
        """Symmetric RTP: once the phone's own audio arrives, send ours back to where it came from,
        which is the NAT's real port for this call."""
        allowed = {dests[0][0], self.remote[0]}
        self.rtp_sock.settimeout(0.3)
        while not self.stop_stream.is_set():
            try:
                _, addr = self.rtp_sock.recvfrom(2048)
            except OSError:
                continue
            if addr[0] in allowed:
                self.heard += 1
            if addr[0] in allowed and addr != dests[0]:
                log.info("%s: the phone's audio comes from %s:%s, sending there", self.t.uri, *addr)
                dests[0] = addr

    def _parse_answer(self, sdp: str) -> tuple[tuple[str, int] | None, str]:
        conn, port, pts = None, None, []
        for line in sdp.splitlines():
            line = line.strip()
            if line.startswith("c=IN IP4 "):
                conn = line.split()[2].split("/")[0]
            elif line.startswith("m=audio "):
                parts = line.split()
                port = int(parts[1])
                pts = [int(p) for p in parts[3:] if p.isdigit()]
        if not conn or not port:
            return None, ""
        for pt in pts:
            for name, num in CODECS.items():
                if pt == num and name in self.t.codecs:
                    return (conn, port), name
        return None, ""

    def _bye(self):
        resp, branch, _ = self._transaction("BYE", in_dialog=True, final_timeout=8)
        if resp is not None and resp.status in (401, 407) and self.t.username:
            hdr = "WWW-Authenticate" if resp.status == 401 else "Proxy-Authenticate"
            ah = "Authorization" if resp.status == 401 else "Proxy-Authorization"
            cred = _digest(resp.get(hdr), "BYE", self.remote_target, self.t.auth_username or self.t.username,
                           self.t.password)
            self._transaction("BYE", extra=[f"{ah}: {cred}"], in_dialog=True, final_timeout=8)
