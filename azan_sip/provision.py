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
from datetime import datetime, timedelta, timezone

from . import security, sites
from .sipserver import login

log = logging.getLogger("azan-sip")

DEFAULT_PORT = 8766
PREFIX = "/provision/"
_KEY = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
MAX_TRIES, TRY_WINDOW, LOCKOUT = 10, 600, 900   # wrong keys from one address before it is refused for a while
MAX_LINES = 16
MAX_HANDSETS = 8   # a Yealink DECT base (W60B and others) takes up to 8 handsets


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
            handset = a.get("handset")
            if handset not in (None, ""):
                try:
                    handset = int(handset)
                except (TypeError, ValueError):
                    handset = 0
                if not 1 <= handset <= MAX_HANDSETS:
                    raise ValueError(f"The handset for {a.get('username')} must be between 1 and {MAX_HANDSETS}")
                if not m:
                    raise ValueError(f"Type the base's MAC address for {a.get('username')}, so its handset can be set")
            keep_lines(a.get("keep_lines"))   # raises with a friendly message


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


def add_phone(cfg: dict, phone_mac: str, name: str = "", lines: int = 1, first_line: int = 0,
              cordless: bool = False, keep="") -> list[dict]:
    """Add a phone in one step: `lines` new desk phone extensions with its MAC address, on its next free lines
    (from `first_line` if given), each with its own new extension number and password. Changes cfg in place.

    `cordless`: a Yealink DECT base, one extension per handset. Its handsets only pick up by themselves when the
    line rings that one handset, so each extension also gets the next free handset, and the base is told to ring
    it on that handset only (plus the base's own `keep` lines, such as the phone system's line 1)."""
    m = mac(phone_mac)
    if not m:
        raise ValueError("Type the phone's MAC address. It looks like 80:5E:C0:12:34:56, on a sticker under the phone or base")
    if not 1 <= lines <= 8:
        raise ValueError("Choose 1 to 8 handsets" if cordless else "Choose 1 to 8 lines")
    keep_list = keep_lines(keep)
    targets = cfg.setdefault("targets", [])
    t = next((x for x in targets if x.get("type") == "phones"), None)
    if t is None:
        port = next((x.get("port") for x in targets if x.get("type") == "server"), None) or 5060
        t = {"name": "Desk phones sign in to AthanBox", "type": "phones", "port": port, "codec": "PCMU",
             "enabled": True, "accounts": []}
        targets.append(t)
    used = {str(a.get("username", "")).lower() for x in targets if x.get("type") in ("server", "phones")
            for a in x.get("accounts", [])}
    taken = {n for n, _, _ in lines_for(cfg, m)} | set(keep_list)
    handsets = {int(a["handset"]) for _, a, _ in lines_for(cfg, m) if str(a.get("handset") or "").isdigit()}
    handset = 1
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
        if cordless:
            while handset in handsets:
                handset += 1
            if handset > MAX_HANDSETS:
                raise ValueError(f"That base has no free handset left (up to {MAX_HANDSETS})")
            a["name"] = f"{name} {handset}"
            a["handset"] = handset
            if keep_list:
                a["keep_lines"] = keep_list
            handsets.add(handset)
        t.setdefault("accounts", []).append(a)
        used.add(str(ext))
        taken.add(line)
        added.append(a)
    return added


def _desk_target(cfg: dict) -> dict:
    """The "Desk phones sign in to AthanBox" entry, made if there isn't one yet."""
    targets = cfg.setdefault("targets", [])
    t = next((x for x in targets if x.get("type") == "phones"), None)
    if t is None:
        port = next((x.get("port") for x in targets if x.get("type") == "server"), None) or 5060
        t = {"name": "Desk phones sign in to AthanBox", "type": "phones", "port": port, "codec": "PCMU",
             "enabled": True, "accounts": []}
        targets.append(t)
    return t


def phone_name(accts: list[dict]) -> str:
    """The name the Remote provisioning page shows for a phone: the one given to it, else its first line's."""
    for a in accts:
        if _v(a.get("phone")):
            return _v(a.get("phone"))
    return _v(accts[0].get("name")) if accts else ""


def assign_phone(cfg: dict, phone_mac: str, name: str = "", cordless: bool = False, keep="",
                 rows: list | None = None, old_mac: str = "") -> list[dict]:
    """The Remote provisioning page's phone: which extensions it has, on which line (desk phone) or handset
    (cordless base). Changes cfg in place and returns the extensions it made.

    Each row is {"line" or "handset": n, "username": an existing desk phone extension, or "new" for a new one
    with its own number and password}; a row without a username is left out (such as the phone system's line 1).
    Extensions that were on this phone and aren't in the rows any more stay, without the phone."""
    m = mac(phone_mac)
    if not m:
        raise ValueError("Type the phone's MAC address. It looks like 80:5E:C0:12:34:56, on a sticker under the phone or base")
    old = mac(old_mac) or m
    if old != m and m in phones(cfg):
        raise ValueError(f"{pretty(m)} is already in the list. Open it there instead")
    rows = [r for r in (rows or []) if isinstance(r, dict) and str(r.get("username") or "").strip()]
    keep_list = keep_lines(keep) if cordless else []
    t = _desk_target(cfg)
    accts = t.setdefault("accounts", [])
    name = _v(name)[:40]
    slot = "handset" if cordless else "line"
    seen_slot, seen_user = set(), set()
    for r in rows:
        try:
            n = int(r.get(slot))
        except (TypeError, ValueError):
            n = 0
        top = MAX_HANDSETS if cordless else MAX_LINES
        if not 1 <= n <= top:
            raise ValueError(f"Choose a {slot} from 1 to {top}")
        if n in seen_slot:
            raise ValueError(f"{slot.capitalize()} {n} is used twice")
        if not cordless and n in keep_list:
            raise ValueError(f"Line {n} is kept for your phone system")
        seen_slot.add(n)
        u = str(r["username"]).strip()
        if u != "new":
            if u.lower() in seen_user:
                raise ValueError(f"Extension {u} is used twice on this phone")
            seen_user.add(u.lower())
            if not any(str(a.get("username", "")).lower() == u.lower() for a in accts):
                raise ValueError(f"There is no desk phone extension {u}. Add it under Your phones first, or pick New extension")
    # This phone's lines so far: an extension that stays keeps its line on the phone
    before = {str(a.get("username", "")).lower(): a.get("line") for a in accts if _mac_or_none(a.get("mac")) in (m, old)}
    label = name or phone_name([a for a in accts if _mac_or_none(a.get("mac")) in (m, old)]) or f"Phone {pretty(m)[-5:]}"
    for a in accts:
        if _mac_or_none(a.get("mac")) in (m, old):
            for k in ("mac", "line", "handset", "keep_lines", "phone"):
                a.pop(k, None)
    used = {str(a.get("username", "")).lower() for x in cfg.get("targets", []) if x.get("type") in ("server", "phones")
            for a in x.get("accounts", [])}
    chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    added, chosen, ext = [], [], 101
    for r in sorted(rows, key=lambda r: int(r.get(slot))):
        n, u = int(r.get(slot)), str(r["username"]).strip()
        if u == "new":
            while str(ext) in used:
                ext += 1
            a = {"name": f"{label} {n}" if cordless or len(rows) > 1 else label, "username": str(ext),
                 "password": "".join(secrets.choice(chars) for _ in range(14)), "kind": "phone", "max_signins": 1}
            accts.append(a)
            used.add(str(ext))
            added.append(a)
        else:
            a = next(a for a in accts if str(a.get("username", "")).lower() == u.lower())
            for k in ("mac", "line", "handset", "keep_lines", "phone"):   # moved here from another phone
                a.pop(k, None)
        a["mac"] = pretty(m)
        if name:
            a["phone"] = name
        if cordless:
            a["handset"] = n
            if keep_list:
                a["keep_lines"] = keep_list
        else:
            a["line"] = n
        chosen.append(a)
    if cordless:   # each handset's own line on the base: the one it had, else the next free one
        taken = set(keep_list)
        for a in chosen:
            ln = before.get(str(a["username"]).lower())
            if str(ln or "").isdigit() and int(ln) not in taken:
                a["line"] = int(ln)
                taken.add(int(ln))
        ln = 1
        for a in chosen:
            if "line" not in a:
                while ln in taken:
                    ln += 1
                if ln > MAX_LINES:
                    raise ValueError(f"That base has no free line left (up to {MAX_LINES})")
                a["line"] = ln
                taken.add(ln)
    return added


def remove_phone(cfg: dict, phone_mac: str) -> int:
    """Take a phone off Remote provisioning. Its extensions stay, without the phone. Returns how many it had."""
    m = mac(phone_mac)
    n = 0
    for t in cfg.get("targets", []):
        if t.get("type") != "phones":
            continue
        for a in t.get("accounts", []):
            if _mac_or_none(a.get("mac")) == m:
                for k in ("mac", "line", "handset", "keep_lines", "phone"):
                    a.pop(k, None)
                n += 1
    return n


def delete_phone(cfg: dict, phone_mac: str) -> int:
    """Remove a phone and the desk phone extensions on it (All sites › Find a phone). Returns how many went.
    A desk phones entry left with no extensions goes too, as it can't be saved empty."""
    m = mac(phone_mac)
    n = 0
    for t in [t for t in cfg.get("targets", []) if t.get("type") == "phones"]:
        keep = [a for a in t.get("accounts", []) if _mac_or_none(a.get("mac")) != m]
        n += len(t.get("accounts", [])) - len(keep)
        t["accounts"] = keep
        if not keep:
            cfg["targets"].remove(t)
    return n


def _mac_or_none(text) -> str | None:
    try:
        return mac(text) or None
    except ValueError:
        return None


def model(agent: str) -> str:
    """The phone's model from its User-Agent, such as "T54W" from "Yealink SIP-T54W 96.86.0.100 ..."."""
    hit = re.search(r"Yealink\s+(?:SIP-|W\d+B-)?([A-Za-z0-9-]{2,20})", agent or "")
    return hit.group(1) if hit else ""


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


def keep_lines(text) -> list[int]:
    """The base's other lines that keep ringing every handset, from "1" or "1, 2" (or a list)."""
    items = text if isinstance(text, list) else re.split(r"[\s,]+", str(text or "").strip())
    out = []
    for x in items:
        if str(x).strip() == "":
            continue
        try:
            n = int(x)
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= MAX_LINES:
            raise ValueError(f"Other lines on the base are line numbers from 1 to {MAX_LINES}, such as 1 or 1, 2")
        if n not in out:
            out.append(n)
    return sorted(out)


def _v(text) -> str:
    """A value for a phone's config file: one line, no control characters."""
    return re.sub(r"[\x00-\x1f\x7f]", "", str(text or "")).strip()


NTP_SERVERS = ("pool.ntp.org", "time.google.com")


def _offset(seconds: float) -> str:
    """A UTC offset as Yealink writes it: +0, -5, +5:30."""
    sign = "-" if seconds < 0 else "+"
    m = int(abs(seconds)) // 60
    return f"{sign}{m // 60}" + (f":{m % 60:02d}" if m % 60 else "")


def clock(cfg: dict, now: datetime | None = None) -> dict:
    """The phones' clock from AthanBox's own time zone (Prayer times page): the standard UTC offset and this
    year's daylight-saving start and end, as wall-clock times, so the phones show the same time as AthanBox."""
    from .praytimes import get_zone
    zone = get_zone((cfg.get("location") or {}).get("timezone"))
    now = now or datetime.now(timezone.utc)

    def off(t: datetime) -> timedelta:
        return t.astimezone(zone).utcoffset() if zone else t.astimezone().utcoffset()

    year = now.astimezone(zone).year if zone else now.astimezone().year
    t = datetime(year, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) + timedelta(days=1)
    changes, prev = [], off(t)
    while t < end:   # each hour of the year: where the offset changes
        nxt = t + timedelta(hours=1)
        cur = off(nxt)
        if cur != prev:
            lo, hi = t, nxt   # to the minute (a few zones change on the half hour)
            while hi - lo > timedelta(minutes=1):
                mid = lo + (hi - lo) / 2
                lo, hi = (mid, hi) if off(mid) == prev else (lo, mid)
            changes.append((hi, prev, cur))
            prev = cur
        t = nxt
    changes = [c for c in changes if (c[0] + c[1]).year == year]
    offsets = [off(datetime(year, m, 1, tzinfo=timezone.utc)) for m in range(1, 13)] + [off(now)]
    std = min(offsets)
    out = {"offset": _offset(std.total_seconds()), "dst": None}
    starts = [c for c in changes if c[2] > c[1]]
    ends = [c for c in changes if c[2] < c[1]]
    if starts and ends:
        wall = lambda c: c[0] + c[1]   # the wall clock just before the change
        a, b = wall(starts[0]), wall(ends[0])
        out["dst"] = {"start": f"{a.month}/{a.day}/{a.hour}", "end": f"{b.month}/{b.day}/{b.hour}",
                      "minutes": int((max(offsets) - std).total_seconds() // 60)}
    return out


# ------------------------------------------------------------------ brands
class Yealink:
    """Yealink desk phones and DECT bases (such as the W60B with W52H/W56H handsets). The phone asks for
    y0000000000XX.cfg (settings for every phone of its model) and then <mac>.cfg (its own)."""
    name = "Yealink"
    common = re.compile(r"^y0{6,}[0-9a-f]{2,4}\.cfg$")
    own = re.compile(r"^([0-9a-f]{12})\.cfg$")

    @staticmethod
    def common_file(clk: dict | None = None) -> str:
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
            *(Yealink.clock_lines(clk) if clk else []),
            ""])

    @staticmethod
    def clock_lines(clk: dict) -> list[str]:
        out = ["# The clock: AthanBox's time zone, daylight saving and internet time (a time server from DHCP still comes first)",
               f"local_time.time_zone = {clk['offset']}",
               f"local_time.ntp_server1 = {NTP_SERVERS[0]}",
               f"local_time.ntp_server2 = {NTP_SERVERS[1]}",
               "local_time.interval = 3600"]
        d = clk.get("dst")
        if d:
            out += ["local_time.summer_time = 1", "local_time.dst_time_type = 0",
                    f"local_time.start_time = {d['start']}", f"local_time.end_time = {d['end']}",
                    f"local_time.offset_time = {d['minutes']}"]
        else:
            out.append("local_time.summer_time = 0")
        return out

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
        # A DECT base only auto-answers when the line rings one handset: ring each AthanBox line on its own handset
        handsets: dict[int, set[int]] = {}
        for line, a, _t in lines:
            if str(a.get("handset") or "").isdigit():
                handsets.setdefault(int(a["handset"]), set()).update([line, *keep_lines(a.get("keep_lines"))])
        if handsets:
            out.append("# Cordless handsets: each AthanBox line rings its own handset only, so it picks up by itself")
            out += [f"handset.{h}.incoming_lines = {','.join(str(x) for x in sorted(ls))}" for h, ls in sorted(handsets.items())]
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

    def recent(self, limit: int = 20, site: str = sites.MAIN) -> list[dict]:
        """The phones that asked through one site's link, newest first."""
        with self.lock:
            items = sorted((x for x in self.seen.values() if x.get("site", sites.MAIN) == site), key=lambda x: -x["at"])[:limit]
        now = time.time()
        return [{**x, "ago": int(now - x["at"])} for x in items]

    def handle(self, cfgs: dict[str, dict], path: str, ip: str, host_header: str, box_ip: str,
               agent: str = "") -> tuple[int, bytes, str]:
        """(status, body, content type) for a GET of /provision/<key>/<brand>/<file>. `cfgs`: every site's
        settings (code -> config), the first site's first; the key says whose link it is."""
        plain = "text/plain; charset=utf-8"
        on = {code: c for code, c in cfgs.items() if settings(c)["enabled"] and settings(c)["key"]}
        if not on:
            return 404, b"Not found", plain
        if self._refused(ip):
            return 403, b"Forbidden", plain
        parts = path[len(PREFIX):].split("/")
        key = parts[0] if parts else ""
        site = None
        for code, c in on.items():   # compare with every key, so the time taken doesn't tell which matched
            if secrets.compare_digest(key.encode(), settings(c)["key"].encode()):
                site = code
        if site is None:
            self._failed(ip)
            # Fail2ban reads this line (Security page); keep the address at the end
            log.warning("Wrong provisioning link from %s", ip)
            return 404, b"Not found", plain
        cfg, s = cfgs[site], settings(cfgs[site])
        shared = settings(next(iter(cfgs.values())))   # the port and public address: the first site's
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
            try:
                clk = clock(cfg)
            except Exception:   # never keep a phone from its lines over the clock
                log.exception("Provisioning: couldn't work out the phones' clock")
                clk = None
            return 200, brand.common_file(clk).encode(), plain
        m = brand.own.match(name)
        if not m:
            return 404, b"Not found", plain   # the other files phones ask for (contacts, -local.cfg, ...)
        phone = m.group(1)
        lines = lines_for(cfg, phone)
        with self.lock:
            self.seen[f"{site}/{phone}"] = {"mac": pretty(phone), "key": phone, "ip": ip, "brand": brand.name, "at": time.time(),
                                "model": model(agent), "lines": [a.get("username") for _, a, _ in lines], "site": site}
            if len(self.seen) > 200:
                del self.seen[min(self.seen, key=lambda k: self.seen[k]["at"])]
        if not lines:
            log.info("Provisioning: %s phone %s (%s) asked for its settings, but no desk phone has that MAC "
                     "address on the Phones page", brand.name, pretty(phone), ip)
            return 404, b"Not found", plain
        host = shared["host"] or _host_from(host_header) or box_ip
        sip_port = int(lines[0][2].get("port", 5060) or 5060)
        # another site's phones sign in with the site code in front (wcp-201)
        lines = [(n, {**a, "username": login(site, a.get("username", ""))}, t) for n, a, t in lines]
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
