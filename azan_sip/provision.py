"""Remote provisioning: desk phones fetch their AthanBox line from a link, on the network or from anywhere.

The Phones page turns it on and shows the link, such as http://203.0.113.7:8766/provision/<key>/yealink/.
Typed into a phone's Auto Provision page (or handed out by DHCP option 66), the phone downloads its
settings from it: the line for each desk phone account whose MAC address matches the phone, with
auto-answer on, so nobody has to type the SIP details on the phone.

- The link carries a long random key; a wrong key is logged (fail2ban's control panel jail reads it)
  and an address that keeps guessing is refused for a while. "New link" makes the old one stop working.
- An optional allow list limits which addresses may download (say the office's public address).
- It answers on its own port (8766 unless changed) as well as the control panel's, so a router can
  forward that one port for phones outside the network without opening the control panel to the internet.
- The SIP server address in the settings is the address the phone used to reach the link, unless the
  Phones page names one, so phones outside the network sign in to the same public address.

Brands: the link names the brand, as each brand asks for its own file names. Yealink for now; another
brand is another entry in BRANDS.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import secrets
import threading
import time

from . import security

log = logging.getLogger("azan-sip")

DEFAULT_PORT = 8766
PREFIX = "/provision/"
_KEY = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
MAX_TRIES, TRY_WINDOW, LOCKOUT = 10, 600, 900   # wrong keys from one address before it is refused for a while
MAX_LINES = 16


def new_key() -> str:
    return secrets.token_urlsafe(24)


def settings(cfg: dict) -> dict:
    p = cfg.get("provisioning") or {}
    try:
        port = int(p.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    allow = p.get("allow") or []
    return {"enabled": bool(p.get("enabled")), "key": str(p.get("key") or ""), "port": port,
            "host": str(p.get("host") or "").strip(),
            "allow": [str(x) for x in allow] if isinstance(allow, list) else []}


def mac(text) -> str:
    """A MAC address as the phones name their files: 12 lower-case hex digits ("" for none)."""
    s = re.sub(r"[\s:.\-]", "", str(text or "")).lower()
    if not s:
        return ""
    if not re.fullmatch(r"[0-9a-f]{12}", s):
        raise ValueError(f"{text} isn't a MAC address. It looks like 80:5E:C0:12:34:56, on a sticker under the phone or base")
    return s


def check(cfg: dict) -> None:
    """Refuse provisioning settings that can't work, with a message for the person saving them."""
    s = settings(cfg)
    raw = (cfg.get("provisioning") or {}).get("port")
    if raw not in (None, ""):
        try:
            int(raw)
        except (TypeError, ValueError):
            raise ValueError("The provisioning port must be a number, such as 8766") from None
    if not 1024 <= s["port"] <= 65535:
        raise ValueError("Choose a provisioning port between 1024 and 65535, such as 8766")
    general = int((cfg.get("general") or {}).get("port") or 8765)
    if s["port"] in (80, general, 8770):
        raise ValueError(f"Port {s['port']} is already used by AthanBox. Choose another provisioning port, such as 8766")
    if s["host"]:
        h = s["host"]
        try:
            ipaddress.ip_address(h)
        except ValueError:
            if not _HOST.match(h):
                raise ValueError("Type the public address as a name (azan.example.com) or an IP address, "
                                 "without http:// or a port") from None
    for x in s["allow"]:
        security.parse(x)   # raises with a friendly message
    if s["enabled"] and not _KEY.match(s["key"]):
        raise ValueError("The provisioning link has no key yet. Press New link")
    seen: dict[tuple[str, int], str] = {}
    for t in cfg.get("targets", []):
        if t.get("type") != "phones":
            continue
        for a in t.get("accounts", []):
            m = mac(a.get("mac"))
            line = a.get("line")
            if line not in (None, ""):
                try:
                    line = int(line)
                except (TypeError, ValueError):
                    line = 0
                if not 1 <= line <= MAX_LINES:
                    raise ValueError(f"The line for {a.get('username')} must be between 1 and {MAX_LINES}")
                if m:
                    other = seen.get((m, line))
                    if other:
                        raise ValueError(f"{other} and {a.get('username')} both use line {line} on the same phone")
                    seen[(m, line)] = str(a.get("username"))


def phones(cfg: dict) -> dict[str, list[dict]]:
    """MAC address -> the desk phone accounts provisioned to that phone (the Phones page's list)."""
    out: dict[str, list[dict]] = {}
    for t in cfg.get("targets", []):
        if t.get("type") != "phones":
            continue
        for a in t.get("accounts", []):
            try:
                m = mac(a.get("mac"))
            except ValueError:
                continue
            if m:
                out.setdefault(m, []).append(a)
    return out


def pretty(m: str) -> str:
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)).upper()


def add_phone(cfg: dict, phone_mac: str, name: str = "", lines: int = 1, first_line: int = 0) -> list[dict]:
    """Add a phone in one step: `lines` new desk phone extensions with its MAC address, on its next free lines
    (from `first_line` if given), each with its own new extension number and password. Changes cfg in place."""
    m = mac(phone_mac)
    if not m:
        raise ValueError("Type the phone's MAC address. It looks like 80:5E:C0:12:34:56, on a sticker under the phone or base")
    if not 1 <= lines <= 8:
        raise ValueError("Choose 1 to 8 lines")
    targets = cfg.setdefault("targets", [])
    t = next((x for x in targets if x.get("type") == "phones"), None)
    if t is None:
        port = next((x.get("port") for x in targets if x.get("type") == "server"), None) or 5060
        t = {"name": "Desk phones sign in to AthanBox", "type": "phones", "port": port, "codec": "PCMU",
             "enabled": True, "accounts": []}
        targets.append(t)
    used = {str(a.get("username", "")).lower() for x in targets if x.get("type") in ("server", "phones")
            for a in x.get("accounts", [])}
    taken = {n for n, _, _ in lines_for(cfg, m)}
    name = _v(name)[:40] or f"Phone {pretty(m)[-5:]}"
    added, ext, line = [], 101, max(1, first_line)
    chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for i in range(lines):
        while str(ext) in used:
            ext += 1
        while line in taken:
            line += 1
        if line > MAX_LINES:
            raise ValueError(f"That phone has no free line left (up to {MAX_LINES})")
        a = {"name": name if lines == 1 else f"{name} {i + 1}", "username": str(ext),
             "password": "".join(secrets.choice(chars) for _ in range(14)), "kind": "phone", "max_signins": 1,
             "mac": pretty(m), "line": line}
        t.setdefault("accounts", []).append(a)
        used.add(str(ext))
        taken.add(line)
        added.append(a)
    return added


def lines_for(cfg: dict, phone_mac: str) -> list[tuple[int, dict, dict]]:
    """(line on the phone, account, its desk phones entry) for each desk phone account with this MAC address.
    Accounts without a line take the lowest free ones, in the order they're listed."""
    found = []
    for t in cfg.get("targets", []):
        if t.get("type") != "phones":
            continue
        for a in t.get("accounts", []):
            try:
                if a.get("enabled", True) and mac(a.get("mac")) == phone_mac:
                    found.append((a, t))
            except ValueError:
                continue
    taken = {int(a["line"]) for a, _ in found if str(a.get("line") or "").isdigit()}
    out, nxt = [], 1
    for a, t in found:
        line = int(a["line"]) if str(a.get("line") or "").isdigit() else 0
        if not line:
            while nxt in taken:
                nxt += 1
            line = nxt
            taken.add(line)
        out.append((line, a, t))
    return sorted(out, key=lambda x: x[0])


def _v(text) -> str:
    """A value for a phone's config file: one line, no control characters."""
    return re.sub(r"[\x00-\x1f\x7f]", "", str(text or "")).strip()


# ------------------------------------------------------------------ brands
class Yealink:
    """Yealink desk phones and DECT bases (such as the W60B with W52H/W56H handsets). The phone asks for
    y0000000000XX.cfg (settings for every phone of its model) and then <mac>.cfg (its own)."""
    name = "Yealink"
    common = re.compile(r"^y0{6,}[0-9a-f]{2,4}\.cfg$")
    own = re.compile(r"^([0-9a-f]{12})\.cfg$")

    @staticmethod
    def common_file() -> str:
        return "\n".join([
            "#!version:1.0.0.1",
            "## From AthanBox: settings for every phone that uses this provisioning link.",
            "# Answer intercom (auto-answer) calls by themselves, with the microphone off",
            "features.intercom.allow = 1",
            "features.intercom.mute = 1",
            "features.intercom.tone = 1",
            "# DECT handsets: Auto Intercom on",
            "custom.handset.auto_intercom = 1",
            "# \"Update now\" on AthanBox's Phones page (SIP NOTIFY check-sync): download the settings again, without a restart",
            "sip.notify_reboot_enable = 0",
            "# Check for changes once a day, so a new password reaches the phone by itself",
            "auto_provision.repeat.enable = 1",
            "auto_provision.repeat.minutes = 1440",
            ""])

    @staticmethod
    def phone_file(lines: list[tuple[int, dict, dict]], host: str, port: int) -> str:
        out = ["#!version:1.0.0.1", "## From AthanBox: this phone's AthanBox lines."]
        for line, a, _t in lines:
            n = f"account.{line}"
            label = _v(a.get("name")) or "Azan"
            out += [f"# {label} (extension {_v(a.get('username'))})",
                    f"{n}.enable = 1",
                    f"{n}.label = {label}",
                    f"{n}.display_name = {label}",
                    f"{n}.user_name = {_v(a.get('username'))}",
                    f"{n}.auth_name = {_v(a.get('username'))}",
                    f"{n}.password = {_v(a.get('password'))}",
                    f"{n}.sip_server.1.address = {host}",
                    f"{n}.sip_server.1.port = {port}",
                    f"{n}.sip_server.1.transport_type = 0",
                    f"{n}.sip_server.1.expires = 300",
                    # AthanBox only ever calls this line for the azan: answer it straight away
                    f"{n}.auto_answer = 1",
                    # behind a router (a phone outside AthanBox's network): keep the way back open
                    f"{n}.nat.udp_update_enable = 1",
                    f"{n}.nat.udp_update_time = 30",
                    f"{n}.nat.rport = 1"]
        return "\n".join(out) + "\n"


BRANDS = {"yealink": Yealink}


# ------------------------------------------------------------------ the downloads
class Provisioner:
    def __init__(self):
        self.lock = threading.Lock()
        self.failures: dict[str, list[float]] = {}
        self.seen: dict[str, dict] = {}   # mac -> the last download, for the Phones page

    def _refused(self, ip: str) -> bool:
        now = time.time()
        with self.lock:
            tries = [t for t in self.failures.get(ip, []) if now - t < max(TRY_WINDOW, LOCKOUT)]
            self.failures[ip] = tries
            recent = [t for t in tries if now - t < TRY_WINDOW]
            return len(recent) >= MAX_TRIES or (len(tries) >= MAX_TRIES and now - tries[-1] < LOCKOUT)

    def _failed(self, ip: str):
        with self.lock:
            self.failures.setdefault(ip, []).append(time.time())

    def recent(self, limit: int = 20) -> list[dict]:
        with self.lock:
            items = sorted(self.seen.values(), key=lambda x: -x["at"])[:limit]
        now = time.time()
        return [{**x, "ago": int(now - x["at"])} for x in items]

    def handle(self, cfg: dict, path: str, ip: str, host_header: str, box_ip: str) -> tuple[int, bytes, str]:
        """(status, body, content type) for a GET of /provision/<key>/<brand>/<file>."""
        s = settings(cfg)
        plain = "text/plain; charset=utf-8"
        if not s["enabled"]:
            return 404, b"Not found", plain
        if self._refused(ip):
            return 403, b"Forbidden", plain
        parts = path[len(PREFIX):].split("/")
        key = parts[0] if parts else ""
        if not s["key"] or not secrets.compare_digest(key.encode(), s["key"].encode()):
            self._failed(ip)
            # Fail2ban reads this line (Security page); keep the address at the end
            log.warning("Wrong provisioning link from %s", ip)
            return 404, b"Not found", plain
        if s["allow"]:
            try:
                addr = ipaddress.ip_address(ip.removeprefix("::ffff:"))
                ok = any(addr in security.parse(x) for x in s["allow"]
                         if security.parse(x).version == addr.version)
            except ValueError:
                ok = False
            if not ok:
                log.warning("Provisioning: refused %s, which isn't on the provisioning allow list", ip)
                return 403, b"Forbidden", plain
        brand = BRANDS.get(parts[1].lower()) if len(parts) > 1 else None
        name = parts[2].lower() if len(parts) == 3 else ""
        if brand is None or not name:
            return 404, b"Not found", plain
        if brand.common.match(name):
            return 200, brand.common_file().encode(), plain
        m = brand.own.match(name)
        if not m:
            return 404, b"Not found", plain   # the other files phones ask for (contacts, -local.cfg, ...)
        phone = m.group(1)
        lines = lines_for(cfg, phone)
        with self.lock:
            self.seen[phone] = {"mac": pretty(phone), "key": phone, "ip": ip, "brand": brand.name, "at": time.time(),
                                "lines": [a.get("username") for _, a, _ in lines]}
            if len(self.seen) > 200:
                del self.seen[min(self.seen, key=lambda k: self.seen[k]["at"])]
        if not lines:
            log.info("Provisioning: %s phone %s (%s) asked for its settings, but no desk phone has that MAC "
                     "address on the Phones page", brand.name, pretty(phone), ip)
            return 404, b"Not found", plain
        host = s["host"] or _host_from(host_header) or box_ip
        sip_port = int(lines[0][2].get("port", 5060) or 5060)
        log.info("Provisioning: sent %s phone %s (%s) its AthanBox line%s: %s", brand.name, pretty(phone), ip,
                 "s" if len(lines) > 1 else "", ", ".join(f"{a.get('username')} on line {n}" for n, a, _ in lines))
        return 200, brand.phone_file(lines, host, sip_port).encode(), plain


def _host_from(raw: str) -> str:
    """The name or address in a Host header, if it is one a phone can use as its SIP server."""
    raw = (raw or "").strip()
    host = raw[1:raw.index("]")] if raw.startswith("[") and "]" in raw else raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return host if _HOST.match(host) else ""


PROVISIONER = Provisioner()
