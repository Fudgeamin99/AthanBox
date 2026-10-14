"""The Security page on the Azan SIP Box: fail2ban, the whitelist and the blacklist.

The owner's choices are kept in security/settings.json next to config.toml:

- fail2ban: which jails are on (SSH, the control panel, SIP sign-ins), how many wrong tries are
  allowed and for how long an address is banned.
- whitelist: addresses and ranges that are never banned or blocked. The box's own network and
  localhost are on it from the start, so the owner can't lock themselves out.
- blacklist: addresses and ranges that are always blocked.

The control panel runs as an ordinary user, so a small helper that runs as root
(packaging/box/security-helper.py, set up by packaging/box/system-setup.sh) applies them: systemd starts it
whenever settings.json or request.json change. It reports back in /run/azan-sip-security/status.json.

The blacklist also works without the helper (in Docker, say): the control panel and the SIP server
refuse blacklisted addresses themselves (FILTER below).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import rtp

log = logging.getLogger("azan-sip")

STATUS_FILE = Path("/run/azan-sip-security/status.json")
HELPER = Path("/usr/local/lib/azan-sip/security-helper.py")
# Debian: the box sets up each version's system parts by itself after an update (azan-sip-system.service,
# from packaging/box/system-setup.sh). A box installed before 0.5.1 needs that set up once by hand.
SYSTEM_STATE = Path("/var/lib/azan-sip/system.json")
SYSTEM_UPDATER = Path("/usr/local/lib/azan-sip/system-update.py")
SETUP_COMMAND = "sudo sh packaging/box/system-setup.sh"
MAX_ENTRIES = 500
JAILS = ("sshd", "panel", "sip")
DEFAULT_FAIL2BAN = {"enabled": True, "jails": {k: True for k in JAILS}, "maxretry": 5, "findtime": 600,
                    "bantime": 3600, "increment": True}
BANTIMES = (600, 3600, 6 * 3600, 86400, 7 * 86400, 30 * 86400, -1)   # -1: until it's unbanned
FINDTIMES = (300, 600, 1800, 3600, 86400)
LOOPBACK = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]


def parse(text: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """An address (192.168.1.20) or a range (192.168.1.0/24, 2001:db8::/32), normalised."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("Type an IP address, such as 203.0.113.7, or a range, such as 203.0.113.0/24")
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        raise ValueError(f"“{text[:60]}” isn't an IP address or range. Use one like 203.0.113.7 "
                         "or 203.0.113.0/24") from None
    if isinstance(net, ipaddress.IPv6Network) and net.network_address.ipv4_mapped:
        net = ipaddress.ip_network(f"{net.network_address.ipv4_mapped}/{max(0, net.prefixlen - 96)}", strict=False)
    return net


def fmt(net) -> str:
    """192.168.1.20 for a single address, 192.168.1.0/24 for a range."""
    return str(net.network_address) if net.prefixlen == net.max_prefixlen else str(net)


def address(ip: str):
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return None
    return a.ipv4_mapped if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped else a


def _contains(nets, addr) -> bool:
    return any(addr.version == n.version and addr in n for n in nets)


def _clean_note(text) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or "")).strip()[:80]


def local_networks() -> list[tuple[str, str]]:
    """The private networks this box is on, as (range, interface), for the whitelist's first entries."""
    out = []
    try:
        r = subprocess.run(["ip", "-j", "-4", "addr", "show", "scope", "global"], capture_output=True,
                           text=True, timeout=5)
        for iface in json.loads(r.stdout or "[]"):
            name = str(iface.get("ifname") or "")
            if name.startswith(("docker", "br-", "veth", "virbr", "lxc", "tailscale", "wg")):
                continue
            for a in iface.get("addr_info") or []:
                net = ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}").network
                if net.is_private and net.prefixlen >= 8:
                    out.append((fmt(net), name))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    if not out:
        try:
            ip = ipaddress.ip_address(rtp.local_ip_for())
            if ip.is_private and not ip.is_loopback:
                out.append((fmt(ipaddress.ip_network(f"{ip}/24", strict=False)), ""))
        except ValueError:
            pass
    return list(dict.fromkeys(out))


class Filter:
    """Who the control panel and the SIP server refuse: the blacklist, minus the whitelist."""

    def __init__(self):
        self.allow: list = []
        self.block: list = []
        self.cache: dict[str, bool] = {}

    def set(self, allow: list, block: list):
        self.allow, self.block, self.cache = list(allow), list(block), {}

    def blocked(self, ip: str) -> bool:
        if not self.block:
            return False
        hit = self.cache.get(ip)
        if hit is None:
            a = address(ip)
            hit = bool(a and not a.is_loopback and not _contains(self.allow, a) and _contains(self.block, a))
            if len(self.cache) > 4096:
                self.cache.clear()
            self.cache[ip] = hit
        return hit


FILTER = Filter()


class Security:
    def __init__(self, base: Path, docker: bool = False):
        self.folder = Path(base) / "security"
        self.docker = docker
        self.lock = threading.Lock()
        self.settings = self._load()
        self._publish()

    @property
    def settings_file(self) -> Path:
        return self.folder / "settings.json"

    @property
    def request_file(self) -> Path:
        return self.folder / "request.json"

    # -- settings
    def _defaults(self) -> dict:
        when = time.strftime("%Y-%m-%d")
        wl = [{"ip": net, "note": f"This box's network{f' ({name})' if name else ''}", "added": when}
              for net, name in local_networks()]
        return {"rev": 1, "fail2ban": json.loads(json.dumps(DEFAULT_FAIL2BAN)), "whitelist": wl, "blacklist": []}

    def _load(self) -> dict:
        try:
            s = json.loads(self.settings_file.read_text(encoding="utf-8"))
            if not isinstance(s, dict):
                raise ValueError
        except FileNotFoundError:
            s = self._defaults()
            try:
                self._write(s)
                log.info("Security: whitelist started with %s", ", ".join(e["ip"] for e in s["whitelist"]) or "localhost only")
            except OSError as exc:
                log.warning("Security settings couldn't be saved: %s", exc)
            return s
        except (OSError, ValueError):
            log.warning("Security settings in %s couldn't be read - starting again from the defaults", self.settings_file)
            s = self._defaults()
        f = dict(DEFAULT_FAIL2BAN, **(s.get("fail2ban") if isinstance(s.get("fail2ban"), dict) else {}))
        f["jails"] = dict(DEFAULT_FAIL2BAN["jails"], **(f.get("jails") if isinstance(f.get("jails"), dict) else {}))
        s["fail2ban"] = f
        for key in ("whitelist", "blacklist"):
            good = []
            for e in s.get(key) if isinstance(s.get(key), list) else []:
                try:
                    good.append({"ip": fmt(parse(e["ip"])), "note": _clean_note(e.get("note")), "added": str(e.get("added", ""))[:10]})
                except (ValueError, KeyError, TypeError):
                    pass
            s[key] = good
        s["rev"] = int(s.get("rev") or 1) if str(s.get("rev", "")).isdigit() else 1
        return s

    def _write(self, s: dict):
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=1), encoding="utf-8")
        os.replace(tmp, self.settings_file)   # the helper is started when this file changes

    def _save(self):
        """Call with self.lock held."""
        self.settings["rev"] = int(self.settings.get("rev") or 0) + 1
        self._write(self.settings)
        self._publish()

    def _publish(self):
        FILTER.set([parse(e["ip"]) for e in self.settings["whitelist"]] + LOOPBACK,
                   [parse(e["ip"]) for e in self.settings["blacklist"]])

    def reset(self):
        """Factory reset: back to the defaults (this network on the whitelist, nothing blocked)."""
        with self.lock:
            rev = int(self.settings.get("rev") or 0)
            self.settings = self._defaults()
            self.settings["rev"] = rev
            self._save()

    # -- what the page shows
    def helper_status(self) -> dict | None:
        try:
            st = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            return st if isinstance(st, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def system_status() -> dict | None:
        """How the box is getting on with setting up the installed version's system parts."""
        if not SYSTEM_UPDATER.is_file():
            return None
        try:
            s = json.loads(SYSTEM_STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            s = {}
        if not isinstance(s, dict):
            s = {}
        return {"auto": True, "version": str(s.get("version") or ""), "ok": bool(s.get("ok")),
                "running": bool(s.get("running")), "message": str(s.get("message") or "")[:300]}

    def state(self, client_ip: str) -> dict:
        st = self.helper_status()
        installed = HELPER.is_file() or st is not None
        with self.lock:
            s = json.loads(json.dumps(self.settings))
        a = address(client_ip)
        allow = [parse(e["ip"]) for e in s["whitelist"]]
        for e in s["blacklist"]:
            n = parse(e["ip"])
            e["allowed"] = any(n.version == w.version and n.subnet_of(w) for w in allow)   # whitelisted: no effect
        for e in s["whitelist"]:
            n = parse(e["ip"])
            e["you"] = bool(a and a.version == n.version and a in n)
        return {"settings": s, "status": st, "helper": installed, "docker": self.docker,
                "pending": bool(st) and installed and st.get("rev") != s["rev"],
                "setup_command": SETUP_COMMAND, "system": None if self.docker else self.system_status(), "client_ip": str(a) if a else client_ip,
                "client_whitelisted": bool(a and (a.is_loopback or _contains(allow, a))),
                "bantimes": BANTIMES, "findtimes": FINDTIMES, "fail2ban_on_path": bool(shutil.which("fail2ban-client"))}

    # -- changes
    def set_fail2ban(self, req: dict):
        with self.lock:
            f = self.settings["fail2ban"]
            if "enabled" in req:
                f["enabled"] = bool(req["enabled"])
            if isinstance(req.get("jails"), dict):
                for k in JAILS:
                    if k in req["jails"]:
                        f["jails"][k] = bool(req["jails"][k])
            if "maxretry" in req:
                n = int(req["maxretry"])
                if not 1 <= n <= 100:
                    raise ValueError("Wrong tries allowed must be between 1 and 100")
                f["maxretry"] = n
            if "findtime" in req:
                n = int(req["findtime"])
                if not 60 <= n <= 7 * 86400:
                    raise ValueError("Choose how long wrong tries are counted for")
                f["findtime"] = n
            if "bantime" in req:
                n = int(req["bantime"])
                if n != -1 and not 60 <= n <= 365 * 86400:
                    raise ValueError("Choose how long an address is banned for")
                f["bantime"] = n
            if "increment" in req:
                f["increment"] = bool(req["increment"])
            self._save()
        log.info("Security: fail2ban settings changed")

    def add(self, which: str, ip: str, note: str, client_ip: str, own_ips: list[str]):
        if which not in ("whitelist", "blacklist"):
            raise ValueError("Unknown list")
        net = parse(ip)
        with self.lock:
            entries = self.settings[which]
            if any(e["ip"] == fmt(net) for e in entries):
                raise ValueError(f"{fmt(net)} is already on the {which}")
            if len(entries) >= MAX_ENTRIES:
                raise ValueError(f"The {which} is full ({MAX_ENTRIES} entries). Remove some, or use a range instead")
            if which == "blacklist":
                you = address(client_ip)
                if you and you.version == net.version and you in net:
                    raise ValueError(f"You're connected from {you}, so blocking {fmt(net)} would lock you out of this page")
                if any(n.version == net.version and n.overlaps(net) for n in LOOPBACK):
                    raise ValueError("The box's own address (localhost) can't be blocked")
                for own in own_ips:
                    a = address(own)
                    if a and a.version == net.version and a in net:
                        raise ValueError(f"{fmt(net)} includes the box's own address ({a}), so it can't be blocked")
                for e in self.settings["whitelist"]:
                    w = parse(e["ip"])
                    if w.version == net.version and net.subnet_of(w):
                        raise ValueError(f"{fmt(net)} is on the whitelist ({fmt(w)}), which wins. Remove it from the whitelist first")
            entries.append({"ip": fmt(net), "note": _clean_note(note), "added": time.strftime("%Y-%m-%d")})
            self._save()
        log.info("Security: %s added to the %s", fmt(net), which)
        return fmt(net)

    def remove(self, which: str, ip: str):
        if which not in ("whitelist", "blacklist"):
            raise ValueError("Unknown list")
        net = fmt(parse(ip))
        with self.lock:
            before = len(self.settings[which])
            self.settings[which] = [e for e in self.settings[which] if e["ip"] != net]
            if len(self.settings[which]) == before:
                raise ValueError(f"{net} isn't on the {which}")
            self._save()
        log.info("Security: %s removed from the %s", net, which)

    def request(self, unban: list[str] | None = None):
        """Ask the helper to unban addresses, or just to refresh the status."""
        ips = []
        for ip in unban or []:
            a = address(ip)
            if a is None:
                raise ValueError(f"{ip} isn't an IP address")
            ips.append(str(a))
        rid = secrets.token_hex(8)
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.request_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"id": rid, "unban": ips}), encoding="utf-8")
        os.replace(tmp, self.request_file)
        if ips:
            log.info("Security: unban %s", ", ".join(ips))
        return rid
