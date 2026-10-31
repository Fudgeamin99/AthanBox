"""AthanBox's own small SIP server: desk phones (each with its own extension on AthanBox) or a phone
system (as a register SIP trunk) sign in to AthanBox, and it calls them to play the azan.

- Two methods on the Phones page share the server: "Desk phones sign in to AthanBox" (accounts of kind
  "phone", each called on the device itself, always with auto-answer) and "Register to AthanBox"
  (accounts of kind "trunk": a phone system called on the number you choose, such as a paging group;
  with no number, the device itself, as before 0.6.2). Each account can allow several sign-ins at once.
- Sign-ins use SIP digest authentication; repeated wrong passwords from an address are blocked
  for a while. It never connects calls anywhere: the only calls it takes are to the Quran by phone
  extensions (quranline.py), from devices signed in to it.
- Calls go out from the same port the phones signed in to, as phones expect from their server.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, quranline, rtp, security, sites
from .sip import SipCall, SipMessage, SipTarget, _digest, _rand

log = logging.getLogger("azan-sip")

REALM = "azan-sip"
ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS"
MIN_EXPIRES, MAX_EXPIRES = 60, 900
MAX_SIGNINS = 50
NONCE_LIFE = 300
_USER = re.compile(r"^[A-Za-z0-9_.+-]{1,40}$")


@dataclass
class Account:
    username: str
    password: str
    name: str = ""
    kind: str = "trunk"          # trunk: a phone system (or older desk phone set-ups); phone: a desk phone
    dial: str = ""               # the number to call on the phone system (e.g. a paging group); empty: the device
    enabled: bool = True
    max_signins: int = 1         # how many devices / phone systems may be signed in with it at once
    azan: bool = True            # off: it stays signed in, but the azan doesn't call it (its Test button still does)
    site: str = sites.MAIN       # Group: the site it belongs to (other sites' phones sign in as code-username)


def login(site: str, username: str) -> str:
    """The username a phone signs in with: the first site's as it is, any other site's with its code in front
    (wcp-201), so one SIP server serves every site and each phone reaches its own."""
    u = str(username).strip()
    return u if site == sites.MAIN else f"{site}-{u}"


@dataclass
class Binding:
    username: str
    contact: str                 # the Contact URI the phone registered
    addr: tuple[str, int]        # where its REGISTER came from (works through NAT)
    expires_at: float
    user_agent: str = ""
    since: float = field(default_factory=time.time)


def check_accounts(accounts: list, port, empty: str = "Add at least one account for a phone or phone system to sign in with") -> None:
    """Refuse settings that can't work, with a message for the person saving them."""
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValueError("The SIP port must be a number, usually 5060") from None
    if not 1 <= p <= 65535:
        raise ValueError("The SIP port must be between 1 and 65535")
    seen = set()
    if not accounts:
        raise ValueError(empty)
    for a in accounts:
        u = str(a.get("username", "")).strip()
        if not _USER.match(u):
            raise ValueError("Account usernames can use letters, numbers and . _ - + (no spaces)")
        if u.lower() in seen:
            raise ValueError(f"The username {u} is used twice")
        seen.add(u.lower())
        if len(str(a.get("password", ""))) < 8:
            raise ValueError(f"Use a password of at least 8 characters for {u}")
        if not re.match(r"^[A-Za-z0-9*#+_.-]{0,40}$", str(a.get("dial", "")).strip()):
            raise ValueError(f"The number to call for {u} can use digits, letters and * # + _ . -")
        try:
            n = int(a.get("max_signins", 1) or 1)
        except (TypeError, ValueError):
            raise ValueError(f"Sign-ins allowed for {u} must be a number") from None
        if not 1 <= n <= MAX_SIGNINS:
            raise ValueError(f"Sign-ins allowed for {u} must be between 1 and {MAX_SIGNINS}")


def _contact_key(contact: str) -> str:
    """One sign-in per Contact address (user@host:port), whatever parameters a device adds."""
    return re.sub(r"^sips?:", "", contact.strip().strip("<>")).split(";", 1)[0].split("?", 1)[0].lower()


class SipServer(threading.Thread):
    def __init__(self, port: int, local_ip: str | None, accounts: list[Account], state_file: Path | None = None):
        super().__init__(name=f"sip-server-{port}", daemon=True)
        self.port = int(port)
        self.local_ip_cfg = local_ip
        self.accounts = {a.username.lower(): a for a in accounts}
        self.state_file = state_file
        self.lock = threading.Lock()
        self.bindings: dict[str, dict[str, Binding]] = {}   # username -> contact -> sign-in
        self.parked: dict[str, dict[str, Binding]] = {}     # saved sign-ins of accounts not loaded yet (other sites start later)
        self.calls: dict[str, queue.Queue] = {}
        self.secret = secrets.token_bytes(16)
        self.failures: dict[str, list[float]] = {}
        self.stop_event = threading.Event()
        self.status, self.detail = "starting", ""
        self.sock: socket.socket | None = None
        self.started = threading.Event()
        self.loaded = False   # only a server that started (and read the saved sign-ins) may overwrite them

    # ------------------------------------------------------------ lifecycle
    def run(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.sock.bind(("0.0.0.0", self.port))
        except OSError as exc:
            self.status = "failed"
            self.detail = (f"port {self.port} is already in use by another program (a softphone or phone system on "
                           f"this PC?) - choose another port" if getattr(exc, "errno", 0) in (48, 98, 10048, 10013)
                           else f"can't open UDP port {self.port}: {exc}")
            log.error("AthanBox phone server: %s", self.detail)
            self.started.set()
            return
        self._load()
        self.status, self.detail = "running", f"listening on UDP port {self.port}"
        log.info("AthanBox phone server listening on UDP port %s (%d account(s))", self.port, len(self.accounts))
        self.started.set()
        self.sock.settimeout(1.0)
        while not self.stop_event.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except (socket.timeout, TimeoutError):
                self._expire()
                continue
            except (ConnectionResetError, OSError):
                if self.stop_event.is_set():
                    break
                continue
            if not data.strip() or security.FILTER.blocked(addr[0]):
                continue                      # keep-alive, or on the blacklist (Security page)
            try:
                msg = SipMessage.parse(data)
            except Exception:
                continue
            try:
                self._dispatch(msg, addr)
            except Exception:
                log.exception("SIP server: couldn't handle a message from %s", addr[0])
        try:
            self.sock.close()
        except OSError:
            pass
        self.status = "stopped"

    def stop(self):
        self.stop_event.set()
        self._save()

    def update_accounts(self, accounts: list[Account]):
        with self.lock:
            self.accounts = {a.username.lower(): a for a in accounts}
            for u in [u for u in self.parked if u in self.accounts]:
                self.bindings.setdefault(u, {}).update(self.parked.pop(u))
            for u in list(self.bindings):
                a = self.accounts.get(u)
                if a is None or not a.enabled:
                    del self.bindings[u]
                    continue
                bs = self.bindings[u]   # fewer sign-ins allowed now: keep the most recently refreshed
                for k in sorted(bs, key=lambda k: bs[k].expires_at)[:max(0, len(bs) - max(1, a.max_signins))]:
                    del bs[k]
        self._save()

    # ------------------------------------------------------------ state file (bindings survive a restart)
    def _save(self):
        if not self.state_file or not self.loaded:
            return
        with self.lock:
            data = {u: [{"contact": b.contact, "addr": list(b.addr), "expires_at": b.expires_at,
                         "user_agent": b.user_agent, "since": b.since} for b in bs.values()]
                    for u, bs in list(self.bindings.items()) + list(self.parked.items()) if bs}
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError:
            pass

    def _load(self):
        self.loaded = True
        if not self.state_file:
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        now = time.time()
        with self.lock:
            for u, items in data.items():
                into = self.bindings if u in self.accounts else self.parked
                for b in (items if isinstance(items, list) else [items]):   # 1.9.0 saved one per account
                    if b.get("expires_at", 0) > now:
                        into.setdefault(u, {})[_contact_key(b["contact"])] = Binding(
                            u, b["contact"], tuple(b["addr"]), b["expires_at"], b.get("user_agent", ""), b.get("since", now))

    def _expire(self):
        now, gone = time.time(), []
        with self.lock:
            for u, bs in self.bindings.items():
                for k, b in list(bs.items()):
                    if b.expires_at < now:
                        gone.append((u, b.addr[0]))
                        del bs[k]
            for u in [u for u, bs in self.parked.items() if all(b.expires_at < now for b in bs.values())]:
                del self.parked[u]   # a site's sign-in that ran out before the site started (or the site has gone)
        for u, ip in gone:
            log.info("SIP account %s is no longer signed in from %s (its registration ran out)", u, ip)
        if gone:
            self._save()

    # ------------------------------------------------------------ messages
    def send(self, data: bytes, addr):
        try:
            self.sock.sendto(data, addr)
        except OSError:
            pass

    def _reply(self, req: SipMessage, addr, status: str, extra: list[str] | None = None, to_tag: bool = True):
        to = req.get("to")
        if to_tag and "tag=" not in to:
            to += f";tag={_rand(8)}"
        via = req.get_all("via")
        if via:   # rport / received, so replies reach phones behind NAT
            v0 = via[0]
            if "rport" in v0 and not re.search(r"rport=\d", v0):
                v0 = re.sub(r";rport(?=;|$)", f";rport={addr[1]}", v0)
            if "received=" not in v0:
                v0 += f";received={addr[0]}"
            via = [v0] + via[1:]
        lines = ([f"SIP/2.0 {status}"] + [f"Via: {v}" for v in via]
                 + [f"From: {req.get('from')}", f"To: {to}", f"Call-ID: {req.get('call-id')}",
                    f"CSeq: {req.get('cseq')}", f"Allow: {ALLOW}", f"Server: azan-sip/{__version__}"]
                 + (extra or []) + ["Content-Length: 0"])
        self.send(("\r\n".join(lines) + "\r\n\r\n").encode(), addr)

    def _dispatch(self, msg: SipMessage, addr):
        cid = msg.get("call-id")
        q = self.calls.get(cid)
        if q is not None:                      # part of a call we placed
            q.put((msg, addr))
            return
        if not msg.method:
            return                             # a stray response
        m = msg.method
        if m == "REGISTER":
            self._register(msg, addr)
        elif m == "OPTIONS":
            self._reply(msg, addr, "200 OK", ["Accept: application/sdp"], to_tag=False)
        elif m == "INVITE":
            self._invite(msg, addr)
        elif m == "ACK":
            return
        elif m in ("BYE", "CANCEL"):
            self._reply(msg, addr, "481 Call/Transaction Does Not Exist")
        elif m in ("SUBSCRIBE", "PUBLISH"):
            self._reply(msg, addr, "489 Bad Event")
        elif m in ("NOTIFY", "MESSAGE", "INFO"):
            self._reply(msg, addr, "200 OK")
        else:
            self._reply(msg, addr, "405 Method Not Allowed")

    # ------------------------------------------------------------ sign-in
    def _nonce(self) -> str:
        ts = str(int(time.time()))
        return ts + "." + hmac.new(self.secret, ts.encode(), hashlib.sha256).hexdigest()[:24]

    def _nonce_ok(self, nonce: str) -> bool:
        ts, _, sig = nonce.partition(".")
        if not ts.isdigit():
            return False
        good = hmac.new(self.secret, ts.encode(), hashlib.sha256).hexdigest()[:24]
        return hmac.compare_digest(good, sig) and time.time() - int(ts) < NONCE_LIFE

    def _blocked(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self.failures.get(ip, []) if now - t < 300]
        self.failures[ip] = recent
        return len(recent) >= 10

    def _challenge(self, req, addr, stale=False):
        self._reply(req, addr, "401 Unauthorized",
                    [f'WWW-Authenticate: Digest realm="{REALM}", nonce="{self._nonce()}", algorithm=MD5, qop="auth"'
                     + (", stale=true" if stale else "")])

    def _check_auth(self, req: SipMessage, account: Account) -> str:
        """'ok', 'none' (no credentials yet), 'stale' or 'bad'."""
        h = req.get("authorization")
        if not h.lower().startswith("digest"):
            return "none"
        p = {k.lower(): (a or b) for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', h)}
        if p.get("username", "").lower() != account.username.lower() or p.get("realm") != REALM:
            return "bad"
        if not self._nonce_ok(p.get("nonce", "")):
            return "stale"
        md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
        ha1 = md5(f"{p['username']}:{REALM}:{account.password}")
        ha2 = md5(f"{req.method}:{p.get('uri', '')}")
        if p.get("qop", "").lower() == "auth":
            want = md5(f"{ha1}:{p['nonce']}:{p.get('nc', '')}:{p.get('cnonce', '')}:auth:{ha2}")
        else:
            want = md5(f"{ha1}:{p['nonce']}:{ha2}")
        return "ok" if hmac.compare_digest(want, p.get("response", "")) else "bad"

    def _register(self, req: SipMessage, addr):
        ip = addr[0]
        if self._blocked(ip):
            self._reply(req, addr, "403 Forbidden")
            return
        m = re.search(r"sips?:([^@;>]+)@", req.get("to"))
        user = (m.group(1) if m else "").lower()
        account = self.accounts.get(user)
        if account is None or not account.enabled:
            result = "none" if not req.get("authorization") else "bad"
            if result == "none":
                self._challenge(req, addr)      # don't tell scanners which names exist
                return
        else:
            result = self._check_auth(req, account)
        if result in ("none", "stale"):
            self._challenge(req, addr, stale=result == "stale")
            return
        if result != "ok":
            self.failures.setdefault(ip, []).append(time.time())
            log.warning("SIP sign-in for %s from %s failed: wrong username or password", user or "?", ip)
            self._reply(req, addr, "403 Forbidden")
            return
        self.failures.pop(ip, None)
        contacts = req.get_all("contact")
        exp_hdr = re.search(r"\d+", req.get("expires"))
        if any(c.strip() == "*" for c in contacts):
            expires = 0
        else:
            cm = re.search(r";\s*expires=(\d+)", ",".join(contacts))
            expires = int(cm.group(1)) if cm else int(exp_hdr.group()) if exp_hdr else 3600
        cm = re.search(r"<([^>]+)>", contacts[0]) if contacts else None
        contact = (cm.group(1) if cm else contacts[0].split(";")[0].strip()) if contacts else ""
        key = _contact_key(contact) if contact else ""
        if expires == 0 or not contacts:
            with self.lock:
                bs = self.bindings.get(user, {})
                gone = bs.pop(key, None) if contact and contact != "*" else bs.clear() or True
            if gone:
                log.info("SIP account %s signed out (%s)", account.username, ip)
            self._save()
            self._reply(req, addr, "200 OK")
            return
        expires = min(max(expires, MIN_EXPIRES), MAX_EXPIRES)
        now = time.time()
        with self.lock:
            bs = self.bindings.setdefault(user, {})
            for k in [k for k, b in bs.items() if b.expires_at < now]:
                del bs[k]
            old = bs.get(key)
            if old is None and len(bs) >= max(1, account.max_signins):
                # full: a device signing in again from the same address (new port after a restart) takes
                # its old place; anything else has to wait until a sign-in ends or more are allowed
                same = [k for k, b in bs.items() if b.addr[0] == addr[0]]
                if same:
                    del bs[min(same, key=lambda k: bs[k].expires_at)]
                else:
                    full = True
            else:
                full = False
            if not locals().get("full"):
                bs[key] = Binding(user, contact, (addr[0], addr[1]), now + expires, req.get("user-agent"),
                                  old.since if old else now)
        if locals().get("full"):
            log.warning("SIP account %s: refused a sign-in from %s:%s - it already has %d of %d allowed",
                        account.username, addr[0], addr[1], len(bs), account.max_signins)
            self._reply(req, addr, "403 Forbidden", ["Warning: 399 azan-sip \"Too many sign-ins for this account\""])
            return
        if not old or old.addr != (addr[0], addr[1]):
            log.info("%s %s signed in from %s:%s%s", "Desk phone" if account.kind == "phone" else "SIP trunk",
                     account.name or account.username, addr[0], addr[1],
                     f" ({req.get('user-agent')})" if req.get("user-agent") else "")
            self._save()
        self._reply(req, addr, "200 OK", [f"Contact: <{contact}>;expires={expires}", f"Expires: {expires}",
                                          f"Date: {time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime())}"])

    # ------------------------------------------------------------ calls in: Quran by phone
    def _invite(self, req: SipMessage, addr):
        """Only the Quran by phone extensions take calls, and only from devices signed in to AthanBox: by the
        account's password, or (phone systems that don't send one) from the address it signed in from."""
        ip = addr[0]
        m = re.match(r"sips?:([^@;>]+)@", req.first.split(" ")[1] if " " in req.first else "")
        if self._blocked(ip):
            self._reply(req, addr, "403 Forbidden")
            return
        who = next((a for u, bs in self.active().items() for b in bs if b.addr == (addr[0], addr[1])
                    and (a := self.accounts.get(u)) and a.enabled), None)
        if who is None:
            h = req.get("authorization") or req.get("proxy-authorization")
            pm = re.search(r'username="?([^",]+)', h)
            acct = self.accounts.get(pm.group(1).lower()) if pm else None
            result = self._check_auth(req, acct) if acct and acct.enabled and req.get("authorization") else \
                "none" if not h else "bad"
            if result in ("none", "stale"):
                self._challenge(req, addr, stale=result == "stale")
                return
            if result != "ok":
                self.failures.setdefault(ip, []).append(time.time())
                log.warning("A call from %s to AthanBox was refused: wrong username or password", ip)
                self._reply(req, addr, "403 Forbidden")
                return
            who = acct
        line = quranline.line(who.site)   # each site has its own extensions and playlist
        kind = line.which(m.group(1) if m else "")
        if kind is None:
            log.info("%s dialled %s on AthanBox, which isn't a Quran by phone extension", who.name or who.username,
                     m.group(1) if m else "?")
            self._reply(req, addr, "404 Not Found")
            return
        quranline.InboundCall(self, req, addr, kind, who.name or who.username, line).start()

    # ------------------------------------------------------------ remote provisioning: "Update now"
    def check_sync(self, usernames: set[str], timeout: float = 4.0) -> list[dict]:
        """Ask every phone signed in with these accounts to download its settings again (NOTIFY with
        Event: check-sync, which Yealink, Grandstream, Snom and others understand). One result per sign-in."""
        jobs = [(self.accounts[u], b) for u, bs in self.active().items() if u in usernames and u in self.accounts
                for b in bs]

        def one(job) -> dict:
            acct, b = job
            _, host, port = _contact_hostport(b.contact)
            lip = self.local_ip_cfg or rtp.local_ip_for(b.addr[0])
            cid, tag, q = f"{_rand(16)}@{lip}", _rand(8), queue.Queue()
            self.calls[cid] = q
            auth, status = None, 0
            try:
                for cseq in (1, 2):
                    lines = [f"NOTIFY {b.contact} SIP/2.0",
                             f"Via: SIP/2.0/UDP {lip}:{self.port};branch=z9hG4bK{_rand(12)};rport",
                             "Max-Forwards: 70", f"From: <sip:azan@{lip}:{self.port}>;tag={tag}", f"To: <{b.contact}>",
                             f"Call-ID: {cid}", f"CSeq: {cseq} NOTIFY", f"Contact: <sip:azan@{lip}:{self.port}>",
                             "Event: check-sync;reboot=false", "Subscription-State: terminated",
                             f"User-Agent: azan-sip/{__version__}"] + ([auth] if auth else []) + ["Content-Length: 0"]
                    self.send(("\r\n".join(lines) + "\r\n\r\n").encode(), b.addr)
                    deadline = time.monotonic() + timeout
                    status, resp = 0, None
                    while time.monotonic() < deadline:
                        try:
                            msg, _ = q.get(timeout=max(0.05, deadline - time.monotonic()))
                        except queue.Empty:
                            break
                        if msg.method or msg.status < 200:
                            continue
                        status, resp = msg.status, msg
                        break
                    if status in (401, 407) and cseq == 1:   # the phone checks it's really its own server
                        hdr = "www-authenticate" if status == 401 else "proxy-authenticate"
                        auth = (f"{'Authorization' if status == 401 else 'Proxy-Authorization'}: "
                                + _digest(resp.get(hdr), "NOTIFY", b.contact, acct.username, acct.password))
                        continue
                    break
            finally:
                self.calls.pop(cid, None)
            ok = 200 <= status < 300
            log.info("Remote provisioning: asked %s (%s, %s) to fetch its settings again: %s", acct.name or acct.username,
                     b.addr[0], b.user_agent or "phone", "accepted" if ok else f"answered {status}" if status else "no answer")
            return {"username": acct.username, "from": f"{b.addr[0]}:{b.addr[1]}", "device": b.user_agent,
                    "ok": ok, "status": status}

        if not jobs:
            return []
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(16, len(jobs))) as ex:
            return list(ex.map(one, jobs))

    # ------------------------------------------------------------ for the UI and calls
    def active(self) -> dict[str, list[Binding]]:
        """username -> its current sign-ins (oldest first)."""
        now = time.time()
        with self.lock:
            out = {u: sorted((b for b in bs.values() if b.expires_at > now), key=lambda b: b.since)
                   for u, bs in self.bindings.items()}
        return {u: bs for u, bs in out.items() if bs}

    def info(self, site: str = sites.MAIN) -> dict:
        """The server and one site's accounts, by the username in that site's settings (login: what the phone
        signs in with)."""
        act, now = self.active(), time.time()
        accounts = []
        cut = 0 if site == sites.MAIN else len(site) + 1
        for u, a in self.accounts.items():
            if a.site != site:
                continue
            bs = act.get(u, [])
            signins = [{"from": f"{b.addr[0]}:{b.addr[1]}", "device": b.user_agent,
                        "expires_in": int(b.expires_at - now)} for b in bs]
            accounts.append({"username": a.username[cut:], "login": a.username, "name": a.name, "kind": a.kind, "registered": bool(bs),
                             "max": a.max_signins, "signins": signins,
                             "from": signins[0]["from"] if signins else "", "device": signins[0]["device"] if signins else ""})
        return {"status": self.status, "detail": self.detail, "port": self.port, "accounts": accounts}


class ServerCallTransport:
    """Lets a SipCall send and receive through the server's socket instead of its own."""

    def __init__(self, server: SipServer, remote: tuple[str, int]):
        self.server, self.remote, self.port = server, remote, server.port
        self.q: queue.Queue = queue.Queue()
        self.call_id = None

    def attach(self, call_id: str):
        self.call_id = call_id
        self.server.calls[call_id] = self.q

    def send(self, data: bytes, addr=None):
        self.server.send(data, addr or self.remote)

    def recv(self, timeout: float):
        try:
            return self.q.get(timeout=max(timeout, 0.01))
        except queue.Empty:
            return None

    def close(self):
        if self.call_id:
            self.server.calls.pop(self.call_id, None)


class ServerManager:
    """Runs the server to match the settings (one per AthanBox, on one port). Group: every site's phones sign
    in to the same server, other sites' with the site code in front of their username."""

    def __init__(self):
        self.server: SipServer | None = None
        self.lock = threading.Lock()
        self.state_file: Path | None = None
        self.cfgs: dict[str, dict] = {}   # site -> its settings

    def check_site(self, cfg: dict, site: str):
        """Settings a site can't use while the other sites use theirs, with a message for the person saving them."""
        if site == sites.MAIN:
            return
        ts = [t for t in cfg.get("targets", []) if t.get("type") in KINDS]
        port = self._port(dict(self.cfgs, **{site: {}}))
        for t in ts:
            if port and int(t.get("port", 5060) or 5060) != port:
                raise ValueError(f"Phones sign in to AthanBox on SIP port {port} for every site, so use port {port} here too")

    @staticmethod
    def _port(cfgs: dict) -> int | None:
        for code in sorted(cfgs, key=lambda c: c != sites.MAIN):   # the first site's port, else any site's
            for t in cfgs[code].get("targets", []):
                if t.get("type") in KINDS:
                    return int(t.get("port", 5060) or 5060)
        return None

    def sync(self, cfg: dict, base: Path | None = None, site: str = sites.MAIN):
        if base is not None:
            self.state_file = Path(base) / ".sip_bindings.json"
        with self.lock:
            self.cfgs[site] = cfg
            self._apply()

    def drop(self, site: str):
        """A site that is removed: its phones can't sign in any more."""
        with self.lock:
            if self.cfgs.pop(site, None) is not None:
                self._apply()

    def _apply(self):
        # entries switched off still let their phones and phone systems sign in (so a phone system doesn't keep
        # retrying, or get banned, while it is off): only the azan calls stop, as off entries aren't played to
        accounts = [Account(login(code, a.get("username", "")), str(a.get("password", "")), a.get("name", ""),
                            KINDS[t["type"]], str(a.get("dial", "")).strip() if t["type"] == "server" else "",
                            a.get("enabled", True), max(1, min(MAX_SIGNINS, int(a.get("max_signins", 1) or 1))),
                            a.get("azan", True) is not False, code)
                    for code, cfg in self.cfgs.items()
                    for t in cfg.get("targets", []) if t.get("type") in KINDS
                    for a in t.get("accounts", []) if isinstance(a, dict)]
        port = self._port(self.cfgs)
        local_ip = (self.cfgs.get(sites.MAIN) or {}).get("network", {}).get("local_ip") or None
        if port is None:
            if self.server:
                self.server.stop()
                self.server = None
                log.info("AthanBox phone server stopped")
            return
        s = self.server
        if s and s.port == port and s.local_ip_cfg == local_ip and s.is_alive():
            s.update_accounts(accounts)
            return
        if s:
            s.stop()
            s.join(timeout=3)
        self.server = SipServer(port, local_ip, accounts, self.state_file)
        self.server.start()
        self.server.started.wait(2)

    def info(self, site: str = sites.MAIN) -> dict | None:
        s = self.server
        return s.info(site) if s else None

    def stop(self):
        with self.lock:
            if self.server:
                self.server.stop()
                self.server.join(timeout=3)
                self.server = None


MANAGER = ServerManager()
KINDS = {"server": "trunk", "phones": "phone"}   # target type -> the kind of account it holds


def check_targets(targets: list) -> None:
    """Both methods share one server: one entry of each, one port, and each username used once."""
    names = {"server": "Register to AthanBox", "phones": "Desk phones sign in to AthanBox"}
    users: set[str] = set()
    ports = set()
    for typ, label in names.items():
        ts = [t for t in targets if t.get("type") == typ]
        if len(ts) > 1:
            raise ValueError(f"Only one '{label}' entry is possible; add more to it instead")
        for t in ts:
            check_accounts(t.get("accounts", []), t.get("port", 5060),
                           "Add at least one desk phone" if typ == "phones" else
                           "Add at least one account for a phone system to sign in with")
            for a in t.get("accounts", []):
                u = str(a.get("username", "")).strip().lower()
                if u in users:
                    raise ValueError(f"The username {u} is used by a desk phone and a SIP trunk; give each its own")
                users.add(u)
            ports.add(int(t.get("port", 5060) or 5060))
    if len(ports) > 1:
        raise ValueError("Desk phones and SIP trunks sign in to the same SIP server, so give both the same SIP port")


SAME_DEVICE_GAP = 1.5   # seconds between calls to lines on the same device (address and port)
# A line that shares its device with other lines and hasn't picked up in this long isn't going to (a DECT
# handset that's off, flat or out of range): stop ringing it so the device is free for its next line.
SAME_DEVICE_ANSWER = 5.0


@dataclass
class ServerTarget:
    """The "Register to AthanBox" (kind trunk) or "Desk phones sign in to AthanBox" (kind phone) method:
    call every sign-in of every account of that kind."""
    name: str
    codec: str = "PCMU"
    ring_timeout: float = 20.0
    local_ip: str | None = None
    kind: str = "trunk"
    only: str | None = None   # a Test button on one account: call only that username's sign-ins
    only_any: frozenset[str] | None = None   # the Test button on one provisioned phone: only its usernames
    site: str | None = None   # the site whose phones it calls (None: the site the running thread works for)

    def play(self, clip, stop: threading.Event) -> bool:
        server = MANAGER.server
        if server is None or server.status != "running":
            log.warning("[%s] AthanBox phone server isn't running%s", self.name,
                        f": {server.detail}" if server else " - press Save changes first")
            return False
        jobs = []
        site = self.site or sites.current() or sites.MAIN
        for u, bs in server.active().items():
            acct = server.accounts.get(u)
            if not acct or acct.site != site:
                continue
            # a Test button rings its own line even when that line is off for the azan
            picked = self.only == u if self.only else u in self.only_any if self.only_any is not None else acct and acct.azan
            if acct and acct.enabled and acct.kind == self.kind and picked:
                # every device / phone system signed in with the account, once each: a device that signed
                # in more than once from the same address gets only its newest sign-in
                newest = {b.addr: b for b in bs}   # bs is oldest first
                jobs += [(acct, b) for b in newest.values()]
        if not jobs:
            log.warning("[%s] no %s is signed in to AthanBox", self.name,
                        "desk phone" if self.kind == "phone" else "phone system")
            return False
        from concurrent.futures import ThreadPoolExecutor
        codecs = [self.codec] + [c for c in ("PCMU", "PCMA") if c != self.codec]

        # Several lines on one device (say DECT handsets on one base, which all sign in from the base's
        # address and port) are called one after another: a base only auto-answers a call while it has no
        # other call still ringing, and rings the rest. So each line waits until the one before it has
        # picked up or given up (and at least SAME_DEVICE_GAP), and a line that doesn't pick up within
        # SAME_DEVICE_ANSWER (its handset is off or flat) stops ringing so the next one can still answer.
        # Phone systems are called all at once.
        per_device: dict = {}
        for acct, b in jobs:
            per_device.setdefault(b.addr, []).append(acct)
        order, prev = [], {}
        for acct, b in jobs:
            shared = self.kind == "phone" and len(per_device[b.addr]) > 1
            settled = threading.Event()
            order.append((acct, b, prev.get(b.addr) if shared else None, settled,
                          min(self.ring_timeout, SAME_DEVICE_ANSWER) if shared else self.ring_timeout))
            prev[b.addr] = settled
        jobs = order

        def call(job):
            acct, b, after, settled, ring_timeout = job
            if after is not None:
                if stop.wait(SAME_DEVICE_GAP):
                    settled.set()
                    return False
                while not after.wait(0.25):
                    if stop.is_set():
                        settled.set()
                        return False
            _, host, port = _contact_hostport(b.contact)
            user = acct.dial or _contact_user(b.contact) or acct.username   # no number: call the device itself
            target = SipTarget(uri=f"sip:{user}@{host}:{port}", username=acct.username, password=acct.password,
                               from_user="azan", display_name="Azan", auto_answer=True,   # phone systems ignore it
                               codecs=codecs, ring_timeout=ring_timeout,
                               local_ip=self.local_ip or rtp.local_ip_for(b.addr[0]))
            try:
                transport = ServerCallTransport(server, b.addr)
                return SipCall(target, clip.get, stop, transport=transport, settled=settled).run()
            finally:
                settled.set()

        with ThreadPoolExecutor(max_workers=min(32, len(jobs))) as ex:
            results = list(ex.map(call, jobs))
        log.info("[%s] %d/%d answered", self.name, sum(results), len(results))
        return any(results)


def _contact_user(contact: str) -> str:
    m = re.match(r"sips?:([^@;>]+)@", contact)
    return m.group(1) if m else ""


def _contact_hostport(contact: str) -> tuple[str, str, int]:
    u = re.sub(r"^sips?:", "", contact).split(";", 1)[0].split("?", 1)[0]
    user, _, hp = u.rpartition("@")
    host, _, port = hp.partition(":")
    return user, host, int(port) if port.isdigit() else 5060
