"""Keeps AthanBox registered as an extension on the phone system (so it shows as Idle/online).

Registers, refreshes before the registration expires, answers the PBX's keep-alive
OPTIONS requests, declines incoming calls, and unregisters cleanly when stopped.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from datetime import datetime

from . import __version__, rtp
from .sip import SipMessage, _digest, _rand

log = logging.getLogger("azan-sip")

ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS, NOTIFY, INFO, MESSAGE"


class Registration(threading.Thread):
    def __init__(self, name: str, host: str, port: int, username: str, password: str,
                 auth_username: str = "", local_ip: str | None = None, expires: int = 300,
                 display_name: str = "AthanBox"):
        super().__init__(name=f"register-{username}@{host}", daemon=True)
        self.label = name
        self.host, self.port = host, int(port or 5060)
        self.username, self.password = username, password
        self.auth_username = auth_username or username
        self.display_name = display_name
        self.want_expires = max(60, int(expires or 300))
        self.local_ip_cfg = local_ip
        self.stop_event = threading.Event()
        self.status = "starting"        # starting | registered | failed | stopped
        self.detail = ""
        self.since: str | None = None
        self.call_id = f"{_rand(16)}@azan-sip"
        self.tag = _rand(8)
        self.cseq = 0
        self.sock: socket.socket | None = None
        self.remote = None
        self.lip = ""
        self.lport = 0

    # ------------------------------------------------------------------ info
    def key(self) -> tuple:
        return (self.host, self.port, self.username, self.password, self.auth_username,
                self.local_ip_cfg, self.want_expires)

    def info(self) -> dict:
        return {"name": self.label, "extension": self.username, "host": f"{self.host}:{self.port}",
                "status": self.status, "detail": self.detail, "since": self.since,
                "contact": f"{self.lip}:{self.lport}" if self.lport else ""}

    def _set(self, status: str, detail: str = ""):
        if status != self.status or detail != self.detail:
            if status == "registered" and self.status != "registered":
                log.info("Registered extension %s on %s:%s - it now shows as online", self.username, self.host, self.port)
            elif status == "failed" and (self.status != "failed" or detail != self.detail):
                log.warning("Extension %s could not register on %s: %s", self.username, self.host, detail)
            self.status, self.detail = status, detail
            self.since = datetime.now().isoformat(timespec="seconds")

    # -------------------------------------------------------------- messages
    def _register_msg(self, expires: int, auth: str | None = None) -> tuple[bytes, str, int]:
        self.cseq += 1
        branch = "z9hG4bK" + _rand(12)
        ruri = f"sip:{self.host}" + (f":{self.port}" if self.port != 5060 else "")
        aor = f"sip:{self.username}@{self.host}"
        h = [f"REGISTER {ruri} SIP/2.0",
             f"Via: SIP/2.0/UDP {self.lip}:{self.lport};branch={branch};rport",
             "Max-Forwards: 70",
             f'From: "{self.display_name}" <{aor}>;tag={self.tag}',
             f'To: "{self.display_name}" <{aor}>',
             f"Call-ID: {self.call_id}",
             f"CSeq: {self.cseq} REGISTER",
             f"Contact: <sip:{self.username}@{self.lip}:{self.lport}>",
             f"Expires: {expires}",
             f"Allow: {ALLOW}",
             f"User-Agent: azan-sip/{__version__}"]
        if auth:
            h.append(auth)
        h.append("Content-Length: 0")
        return ("\r\n".join(h) + "\r\n\r\n").encode(), ruri, self.cseq

    def _reply(self, req: SipMessage, addr, status: str, extra: list[str] | None = None):
        to = req.get("to")
        if req.method != "OPTIONS" and "tag=" not in to:
            to += f";tag={_rand(8)}"
        lines = [f"SIP/2.0 {status}"] + [f"Via: {v}" for v in req.get_all("via")] + [
            f"From: {req.get('from')}", f"To: {to}", f"Call-ID: {req.get('call-id')}", f"CSeq: {req.get('cseq')}",
            f"Contact: <sip:{self.username}@{self.lip}:{self.lport}>", f"Allow: {ALLOW}",
            f"User-Agent: azan-sip/{__version__}"] + (extra or []) + ["Content-Length: 0"]
        try:
            self.sock.sendto(("\r\n".join(lines) + "\r\n\r\n").encode(), addr)
        except OSError:
            pass

    def _handle_request(self, req: SipMessage, addr):
        m = req.method
        if m == "ACK":
            return
        if m == "OPTIONS":            # the PBX checking we are alive ("qualify" / keep-alive)
            self._reply(req, addr, "200 OK", ["Accept: application/sdp"])
        elif m == "INVITE":           # someone dialled AthanBox extension
            self._reply(req, addr, "486 Busy Here")
        elif m in ("NOTIFY", "MESSAGE", "INFO"):
            self._reply(req, addr, "200 OK")
        elif m in ("BYE", "CANCEL"):
            self._reply(req, addr, "481 Call/Transaction Does Not Exist")
        else:
            self._reply(req, addr, "405 Method Not Allowed")

    def _recv(self, timeout: float):
        """Receive one message; requests are answered here. Returns responses only."""
        self.sock.settimeout(max(0.05, timeout))
        try:
            data, addr = self.sock.recvfrom(65535)
        except (socket.timeout, TimeoutError, ConnectionResetError):
            return None
        except OSError:
            return None
        if not data.strip():
            return None  # keep-alive CRLF
        try:
            msg = SipMessage.parse(data)
        except Exception:
            return None
        if msg.method:
            self._handle_request(msg, addr)
            return None
        return msg

    def _transaction(self, expires: int, auth: str | None = None) -> SipMessage | None:
        data, ruri, cseq = self._register_msg(expires, auth)
        self.sock.sendto(data, self.remote)
        interval, deadline = 0.5, time.monotonic() + 16
        next_tx = time.monotonic() + interval
        provisional = False
        while time.monotonic() < deadline and not (self.stop_event.is_set() and expires):
            msg = self._recv(min(0.25, max(0.05, next_tx - time.monotonic())))
            if msg is None:
                if not provisional and time.monotonic() >= next_tx:
                    self.sock.sendto(data, self.remote)
                    interval = min(interval * 2, 4.0)
                    next_tx = time.monotonic() + interval
                continue
            cs = msg.get("cseq").split()
            if msg.get("call-id") != self.call_id or not cs or cs[0] != str(cseq):
                continue
            if msg.status < 200:
                provisional = True
                continue
            return msg
        return None

    def _register_once(self, expires: int) -> int | None:
        """One REGISTER (with authentication). Returns granted expiry, or None on failure."""
        resp = self._transaction(expires)
        for _ in range(3):
            if resp is None:
                self._set("failed", f"no reply from {self.host}:{self.port} - check the address and that UDP SIP is enabled")
                return None
            if resp.status in (401, 407):
                hdr = "WWW-Authenticate" if resp.status == 401 else "Proxy-Authenticate"
                ah = "Authorization" if resp.status == 401 else "Proxy-Authorization"
                ruri = f"sip:{self.host}" + (f":{self.port}" if self.port != 5060 else "")
                cred = _digest(resp.get(hdr), "REGISTER", ruri, self.auth_username, self.password)
                resp = self._transaction(expires, f"{ah}: {cred}")
                if resp is not None and resp.status in (401, 407):
                    self._set("failed", "wrong extension number or password (the phone system rejected the login)")
                    return None
                continue
            if resp.status == 423:  # Interval Too Brief
                m = re.search(r"\d+", resp.get("min-expires"))
                expires = int(m.group()) if m else expires * 2
                self.want_expires = expires
                resp = self._transaction(expires)
                continue
            break
        if resp is None:
            self._set("failed", f"no reply from {self.host}:{self.port}")
            return None
        if resp.status == 403:
            self._set("failed", "403 Forbidden - check the password, and that this PC's IP is allowed for the extension")
            return None
        if resp.status == 404:
            self._set("failed", f"404 - extension {self.username} doesn't exist on the phone system")
            return None
        if resp.status >= 300:
            self._set("failed", resp.first.replace("SIP/2.0 ", ""))
            return None
        granted = expires
        my_contact = f"{self.lip}:{self.lport}"
        for c in resp.get_all("contact"):
            for part in c.split(","):
                if my_contact in part:
                    m = re.search(r"expires=(\d+)", part)
                    if m:
                        granted = int(m.group(1))
        if granted == expires:
            m = re.search(r"\d+", resp.get("expires"))
            if m:
                granted = int(m.group())
        return granted

    # -------------------------------------------------------------- the loop
    def run(self):
        try:
            self.remote = (socket.gethostbyname(self.host), self.port)
        except OSError:
            self._set("failed", f"can't find '{self.host}' - check the phone system address")
            self.status = "stopped"
            return
        self.lip = self.local_ip_cfg or rtp.local_ip_for(self.remote[0])
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.lport = self.sock.getsockname()[1]
        backoff = 15
        try:
            while not self.stop_event.is_set():
                granted = self._register_once(self.want_expires)
                if granted:
                    self._set("registered", f"as {self.lip}:{self.lport}, renews every {max(30, int(granted * 0.8))} s")
                    wait, backoff = max(30, granted * 0.8), 15
                else:
                    wait, backoff = backoff, min(backoff * 2, 300)
                end = time.monotonic() + wait
                while not self.stop_event.is_set() and time.monotonic() < end:
                    self._recv(min(1.0, end - time.monotonic()))  # answer keep-alives while we wait
            if self.status == "registered":
                self._register_once(0)  # unregister
                log.info("Extension %s unregistered from %s", self.username, self.host)
        except Exception as exc:  # never take the app down
            log.exception("Registration for %s stopped: %s", self.username, exc)
            self._set("failed", str(exc))
        finally:
            self.status = "stopped" if self.stop_event.is_set() else self.status
            try:
                self.sock.close()
            except OSError:
                pass

    def stop(self, wait: bool = False):
        self.stop_event.set()
        if wait:
            self.join(timeout=5)


class RegistrationManager:
    """Starts/stops registrations to match the settings; unchanged ones keep running."""

    def __init__(self):
        self.regs: dict[tuple, Registration] = {}
        self.lock = threading.Lock()

    def sync(self, cfg: dict):
        from .sip import parse_uri
        want: dict[tuple, Registration] = {}
        net = cfg.get("network", {})
        for t in cfg.get("targets", []):
            if not t.get("enabled", True) or t.get("type") != "sip" or not t.get("username"):
                continue
            if not t.get("register", True):
                continue
            _, host, port = parse_uri(t.get("uri", ""))
            if not host:
                continue
            r = Registration(t.get("name") or "Phone system", host, port, t["username"], t.get("password", ""),
                             t.get("auth_username", ""), net.get("local_ip") or None,
                             int(t.get("register_expires", 300) or 300), t.get("display_name", "AthanBox"))
            want.setdefault(r.key(), r)
        with self.lock:
            for k, r in list(self.regs.items()):
                if k not in want:
                    r.stop()
                    del self.regs[k]
            for k, r in want.items():
                if k in self.regs and self.regs[k].is_alive():
                    self.regs[k].label = r.label
                else:
                    self.regs[k] = r
                    r.start()

    def info(self) -> list[dict]:
        with self.lock:
            return [r.info() for r in self.regs.values()]

    def stop_all(self):
        with self.lock:
            regs = list(self.regs.values())
            self.regs.clear()
        for r in regs:
            r.stop()
        for r in regs:
            r.join(timeout=5)
