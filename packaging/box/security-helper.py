#!/usr/bin/python3 -I
"""AthanBox: applies the Security page (fail2ban, whitelist and blacklist) as root.

The control panel runs as the unprivileged user "azan" and can't change the firewall or fail2ban
itself. It writes what the owner chose to /opt/azan-sip/security/settings.json (and one-off
requests such as "unban this address" to request.json). systemd notices the change
(azan-sip-security.path) and runs this script as root, which:

- writes /etc/fail2ban/jail.d/azan-sip.local and the two azan-sip filters, then reloads fail2ban;
- loads the whitelist and blacklist into its own nftables table (inet azan_sip_lists);
- writes what is happening (jails, banned addresses, errors) to /run/azan-sip-security/status.json,
  which the control panel shows. A timer runs it every minute too, to keep that list fresh.

In Docker the same script runs in a container of its own (the "security" service in compose.yaml,
with the host's network and the NET_ADMIN capability): "watch /data" keeps fail2ban running there and
checks the settings every two seconds instead of systemd. The SSH jail isn't available there, and on a
host without nftables (some NAS kernels) the whitelist, blacklist and bans use iptables instead.

On Debian it is installed to /usr/local/lib/azan-sip by packaging/box/system-setup.sh, outside
/opt/azan-sip, so the azan user can't change the code that runs as root. Everything it reads from /opt/azan-sip
is untrusted: addresses are parsed with ipaddress, numbers are clamped and nothing else is used.
Standard library only.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RUN = Path("/run/azan-sip-security")
# Docker: the status goes to the shared /data volume, where the control panel's container reads it
STATUS = Path(os.environ.get("AZAN_SECURITY_STATUS") or RUN / "status.json")
STATE = RUN / "state.json"
JAIL_FILE = Path("/etc/fail2ban/jail.d/azan-sip.local")
FILTER_DIR = Path("/etc/fail2ban/filter.d")
TABLE = "azan_sip_lists"
CHAIN = "AZAN-SIP-LISTS"   # the same lists with iptables, where the kernel has no nftables
CONTAINER = False          # running in Docker ("watch"): no systemd, no SSH jail
FIREWALL: str | None = None   # "nft", "iptables" or None, found by firewall_kind()
# The Security page's names for the jails, and fail2ban's
JAILS = {"sshd": "sshd", "panel": "azan-sip-panel", "sip": "azan-sip-sip"}
MAX_ENTRIES = 2000

# What the control panel writes to azan.log (see auth.py and sipserver.py). The address is matched
# at the end of the line, after the SIP user name, which a caller could choose.
FILTERS = {
    "azan-sip-panel": r"^\s*WARNING\s+Wrong (?:control panel (?:password|setup code)|provisioning link) from <HOST>$",
    "azan-sip-sip": r"^\s*WARNING\s+SIP sign-in for .* from <HOST> failed: wrong username or password$",
}
FILTER_TEXT = """# Written by AthanBox (Security page). Changes here are overwritten.
[Definition]
failregex = {regex}
ignoreregex =
datepattern = ^%%Y-%%m-%%d %%H:%%M:%%S
"""


# ------------------------------------------------------------------ small helpers
def run(*cmd, timeout=30, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def read_json(path: Path, limit: int = 512_000) -> dict:
    """Read a file the azan user wrote: never through a symlink, and never a huge one."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "rb") as f:
            data = f.read(limit + 1)
        if len(data) > limit:
            return {}
        obj = json.loads(data.decode("utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def write_root_file(path: Path, text: str, mode: int = 0o644) -> bool:
    """Write a file in a root-owned folder if it changed. Returns True when it changed."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def mtime(path: Path) -> float:
    try:
        return path.lstat().st_mtime
    except OSError:
        return 0.0


def clamp(v, lo: int, hi: int, default: int) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def networks(items) -> list:
    out = []
    for item in (items if isinstance(items, list) else [])[:MAX_ENTRIES]:
        text = item.get("ip") if isinstance(item, dict) else item
        try:
            net = ipaddress.ip_network(str(text).strip(), strict=False)
        except ValueError:
            continue
        if isinstance(net, ipaddress.IPv6Network) and net.network_address.ipv4_mapped:
            m = net.network_address.ipv4_mapped
            net = ipaddress.ip_network(f"{m}/{max(0, net.prefixlen - 96)}", strict=False)
        out.append(net)
    return out


def clean(raw: dict) -> dict:
    """The settings, keeping only what is valid."""
    f = raw.get("fail2ban") if isinstance(raw.get("fail2ban"), dict) else {}
    jails = f.get("jails") if isinstance(f.get("jails"), dict) else {}
    bantime = clamp(f.get("bantime"), -1, 31_536_000, 3600)
    if 0 <= bantime < 60:
        bantime = 60
    allow = networks(raw.get("whitelist"))
    block = [n for n in networks(raw.get("blacklist")) if not n.is_loopback]
    return {
        "rev": clamp(raw.get("rev"), 0, 2**31, 0),
        "enabled": f.get("enabled") is not False,
        "jails": {k: jails.get(k) is not False for k in JAILS},
        "maxretry": clamp(f.get("maxretry"), 1, 100, 5),
        "findtime": clamp(f.get("findtime"), 60, 7 * 86400, 600),
        "bantime": bantime,
        "increment": f.get("increment") is not False and bantime > 0,
        "allow": allow,
        "block": block,
    }


# ------------------------------------------------------------------ fail2ban
def f2b_installed() -> bool:
    return bool(shutil.which("fail2ban-client"))


def f2b_running() -> bool:
    if CONTAINER:
        return run("fail2ban-client", "ping").returncode == 0
    return run("systemctl", "is-active", "--quiet", "fail2ban").returncode == 0


def f2b_start():
    if CONTAINER:
        Path("/run/fail2ban").mkdir(parents=True, exist_ok=True)
        return run("fail2ban-client", "-x", "start", timeout=90)
    return run("systemctl", "restart", "fail2ban", timeout=90)


def f2b_stop():
    if CONTAINER:
        if f2b_running():
            run("fail2ban-client", "stop", timeout=60)
    elif f2b_running() or run("systemctl", "is-enabled", "--quiet", "fail2ban").returncode == 0:
        run("systemctl", "disable", "--now", "fail2ban", timeout=60)


def firewall_kind() -> str | None:
    """nftables if the kernel has it, else iptables (legacy or nft-based, whichever works)."""
    if shutil.which("nft") and run("nft", "list", "tables").returncode == 0:
        return "nft"
    if shutil.which("iptables") and run("iptables", "-S", "INPUT").returncode == 0:
        return "iptables"
    if CONTAINER and shutil.which("iptables-legacy") and run("iptables-legacy", "-S", "INPUT").returncode == 0:
        # an older kernel (such as a Synology NAS): fail2ban's actions call plain "iptables"
        for name in ("iptables", "ip6tables"):
            run("update-alternatives", "--set", name, f"/usr/sbin/{name}-legacy")
        return "iptables"
    return None


def jail_text(s: dict, log: Path) -> str:
    ignore = " ".join(["127.0.0.1/8", "::1"] + [str(n) for n in s["allow"]])
    have_log = log.is_file()
    lines = [
        "# Written by AthanBox (Security page in the control panel). Changes here are overwritten.",
        "[DEFAULT]",
        f"ignoreip = {ignore}",
        f"bantime = {s['bantime']}",
        f"findtime = {s['findtime']}",
        f"maxretry = {s['maxretry']}",
        f"bantime.increment = {'true' if s['increment'] else 'false'}",
        # a banned address is blocked on every port, TCP and UDP (SIP is UDP)
        *(["banaction = nftables[type=allports]", "banaction_allports = nftables[type=allports]",
           "protocol = tcp,udp"] if FIREWALL != "iptables" else
          ["banaction = iptables-allports", "banaction_allports = iptables-allports", "protocol = all"]),
        "",
        "[sshd]",
        # in Docker the host's SSH sign-ins can't be seen
        f"enabled = {'true' if s['jails']['sshd'] and not CONTAINER else 'false'}",
        "backend = systemd",
        "",
    ]
    for key in ("panel", "sip"):
        name = JAILS[key]
        lines += [f"[{name}]",
                  f"enabled = {'true' if s['jails'][key] and have_log else 'false'}",
                  "backend = auto",
                  f"filter = {name}",
                  f"logpath = {log}",
                  "port = 0:65535",
                  ""]
    return "\n".join(lines)


def apply_fail2ban(s: dict, log: Path, errors: list) -> None:
    if not f2b_installed():
        return
    changed = False
    for name, regex in FILTERS.items():
        changed |= write_root_file(FILTER_DIR / f"{name}.conf", FILTER_TEXT.format(regex=regex))
    changed |= write_root_file(JAIL_FILE, jail_text(s, log))
    if not s["enabled"]:
        f2b_stop()
        return
    if not CONTAINER:
        run("systemctl", "enable", "--quiet", "fail2ban")
    if not f2b_running():
        r = f2b_start()
    elif changed:
        r = run("fail2ban-client", "reload", timeout=90)
    else:
        r = None
    if r is not None and r.returncode != 0:
        errors.append("Fail2ban didn't start: " + ((r.stderr or r.stdout).strip().splitlines() or ["unknown error"])[-1])
    # a newly whitelisted address that is banned right now is let in straight away
    if s["allow"] and f2b_running():
        for ip in banned_ips():
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if any(addr in n for n in s["allow"] if n.version == addr.version):
                run("fail2ban-client", "unban", ip)


def jail_status(name: str) -> dict | None:
    r = run("fail2ban-client", "status", name)
    if r.returncode != 0:
        return None
    out = r.stdout

    def num(label):
        m = re.search(label + r":\s*(\d+)", out)
        return int(m.group(1)) if m else 0
    m = re.search(r"Banned IP list:\s*(.*)", out)
    ips = [x for x in (m.group(1).split() if m else []) if valid_ip(x)]
    until = {}
    t = run("fail2ban-client", "get", name, "banip", "--with-time")
    if t.returncode == 0:   # "1.2.3.4 \t2026-10-14 16:00:00 + 3600 = 2026-10-14 17:00:00"
        for line in t.stdout.splitlines():
            m2 = re.match(r"\s*(\S+)\s+.*=\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*$", line)
            if m2 and valid_ip(m2.group(1)):
                until[m2.group(1)] = m2.group(2)
    return {"failed": num("Currently failed"), "total_failed": num("Total failed"),
            "banned_now": num("Currently banned"), "total_banned": num("Total banned"),
            "banned": [{"ip": ip, "until": until.get(ip, "")} for ip in ips]}


def banned_ips() -> list[str]:
    ips = []
    for name in JAILS.values():
        st = jail_status(name)
        if st:
            ips += [b["ip"] for b in st["banned"]]
    return list(dict.fromkeys(ips))


def valid_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ nftables: whitelist and blacklist
def nft_text(s: dict) -> str:
    def nft_set(name: str, kind: str, nets: list) -> str:
        nets = list(ipaddress.collapse_addresses(nets))
        elems = f" elements = {{ {', '.join(str(n) for n in nets)} }};" if nets else ""
        return f"  set {name} {{ type {kind}; flags interval; auto-merge;{elems} }}\n"
    v4 = lambda ns: [n for n in ns if n.version == 4]  # noqa: E731
    v6 = lambda ns: [n for n in ns if n.version == 6]  # noqa: E731
    return (f"table inet {TABLE}\ndelete table inet {TABLE}\n"
            f"table inet {TABLE} {{\n"
            + nft_set("allow4", "ipv4_addr", v4(s["allow"])) + nft_set("allow6", "ipv6_addr", v6(s["allow"]))
            + nft_set("block4", "ipv4_addr", v4(s["block"])) + nft_set("block6", "ipv6_addr", v6(s["block"]))
            + "  chain input {\n    type filter hook input priority -5; policy accept;\n"
              "    iif \"lo\" accept\n"
              "    ip saddr @allow4 accept\n    ip6 saddr @allow6 accept\n"
              "    ip saddr @block4 drop\n    ip6 saddr @block6 drop\n  }\n}\n")


def lists_present() -> bool:
    if FIREWALL == "nft":
        return run("nft", "list", "table", "inet", TABLE).returncode == 0
    if FIREWALL == "iptables":
        return run("iptables", "-C", "INPUT", "-j", CHAIN).returncode == 0
    return False


def iptables_apply(s: dict) -> subprocess.CompletedProcess:
    """The whitelist and blacklist as a chain of their own, jumped to first from INPUT."""
    for cmd, version in (("iptables", 4), ("ip6tables", 6)):
        if not shutil.which(cmd) or run(cmd, "-S", "INPUT").returncode != 0:
            if version == 4:
                return run(cmd, "-S", "INPUT")
            continue   # no IPv6 firewall on this host
        run(cmd, "-N", CHAIN)
        r = run(cmd, "-F", CHAIN)
        if r.returncode != 0:
            return r
        rules = [["-i", "lo", "-j", "RETURN"]]
        rules += [["-s", str(n), "-j", "RETURN"] for n in ipaddress.collapse_addresses(n for n in s["allow"] if n.version == version)]
        rules += [["-s", str(n), "-j", "DROP"] for n in ipaddress.collapse_addresses(n for n in s["block"] if n.version == version)]
        for rule in rules:
            r = run(cmd, "-A", CHAIN, *rule)
            if r.returncode != 0:
                return r
        if run(cmd, "-C", "INPUT", "-j", CHAIN).returncode != 0:
            r = run(cmd, "-I", "INPUT", "1", "-j", CHAIN)
            if r.returncode != 0:
                return r
    return subprocess.CompletedProcess([], 0, "", "")


def remove_lists():
    """Take the whitelist and blacklist out of the firewall (the Docker container is stopping)."""
    if FIREWALL == "nft":
        run("nft", "delete", "table", "inet", TABLE)
    elif FIREWALL == "iptables":
        for cmd in ("iptables", "ip6tables"):
            run(cmd, "-D", "INPUT", "-j", CHAIN)
            run(cmd, "-F", CHAIN)
            run(cmd, "-X", CHAIN)


def apply_firewall(s: dict, state: dict, errors: list) -> None:
    if FIREWALL is None:
        return
    text = nft_text(s) if FIREWALL == "nft" else json.dumps([str(n) for n in s["allow"] + s["block"]] + [len(s["allow"])])
    digest = hashlib.sha256((FIREWALL + text).encode()).hexdigest()
    if state.get("nft") == digest and lists_present():
        return
    r = run("nft", "-f", "-", stdin=text) if FIREWALL == "nft" else iptables_apply(s)
    if r.returncode != 0:
        errors.append("The firewall didn't accept the lists: " + ((r.stderr or r.stdout).strip().splitlines() or ["unknown error"])[-1])
        state.pop("nft", None)
        return
    state["nft"] = digest


# ------------------------------------------------------------------ main
def handle_request(sec: Path, state: dict) -> str:
    req = read_json(sec / "request.json", 64_000)
    rid = str(req.get("id") or "")[:64]
    if not rid or rid == state.get("request"):
        return state.get("request", "")
    if f2b_installed() and f2b_running():
        for ip in (req.get("unban") if isinstance(req.get("unban"), list) else [])[:50]:
            if valid_ip(str(ip)):
                run("fail2ban-client", "unban", str(ip))
    state["request"] = rid
    return rid


def once(base: Path) -> None:
    """base: AthanBox's folder (/opt/azan-sip)."""
    RUN.mkdir(mode=0o755, exist_ok=True)
    sec = base / "security"
    state = read_json(STATE)   # in /run, so it starts empty after a restart and everything is applied again
    raw = read_json(sec / "settings.json")
    s = clean(raw)
    errors: list[str] = []
    log = base / "azan.log"
    digest = hashlib.sha256(json.dumps([raw, log.is_file()], sort_keys=True).encode()).hexdigest()
    # Docker has no systemd to restart fail2ban if it stops, so start it again here
    restart = CONTAINER and s["enabled"] and f2b_installed() and not f2b_running()
    if raw and (state.get("settings") != digest or restart):
        apply_fail2ban(s, log, errors)
        apply_firewall(s, state, errors)
        state["settings"], state["rev"] = digest, s["rev"]
        state["errors"] = errors
    elif raw:
        apply_firewall(s, state, errors)   # put the table back if something flushed the firewall
        errors = state.get("errors", []) + errors
    rid = handle_request(sec, state)

    installed = f2b_installed()
    running = installed and f2b_running()
    jails = {}
    for key, name in JAILS.items():
        st = jail_status(name) if running else None
        jails[key] = dict(st or {}, active=st is not None, unavailable=CONTAINER and key == "sshd")
    if s["jails"]["panel"] or s["jails"]["sip"]:
        if not log.is_file():
            errors.append(f"{log} doesn't exist yet, so the control panel and SIP jails are waiting for it")
    status = {
        "helper": 1,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rev": state.get("rev", -1),
        "request": rid,
        "fail2ban": {"installed": installed, "running": running,
                     "version": (run("fail2ban-client", "--version").stdout.strip().split() or [""])[-1] if installed else ""},
        "jails": jails,
        "firewall": {"installed": FIREWALL is not None, "kind": FIREWALL or "", "active": lists_present(),
                     "blocked": len(s["block"]), "allowed": len(s["allow"])},
        "container": CONTAINER,
        "errors": errors + ([] if FIREWALL else ["This computer's firewall can't be changed (neither nftables nor iptables "
                                                 "works here), so bans and the blacklist only cover the control panel and SIP"]),
    }
    STATUS.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATUS.parent, prefix=".status.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(status, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATUS)
    fd, tmp = tempfile.mkstemp(dir=RUN, prefix=".state.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


def watch(base: Path) -> int:
    """Docker: apply changes within two seconds, refresh the status every minute, until stopped."""
    global CONTAINER, FIREWALL
    CONTAINER = True
    FIREWALL = firewall_kind()
    STATE.unlink(missing_ok=True)   # a restarted container keeps its /run: apply everything again
    print(f"azan-sip-security: watching {base}/security, firewall: {FIREWALL or 'none'}", flush=True)

    def stop(*_):
        f2b_stop()
        remove_lists()
        print("azan-sip-security: stopped", flush=True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    seen, last = None, 0.0
    while True:
        now = (mtime(base / "security" / "settings.json"), mtime(base / "security" / "request.json"))
        # wait for AthanBox to start and write its settings (and own the folder) the first time
        if now[0] and (now != seen or time.monotonic() - last > 60):
            seen, last = now, time.monotonic()
            try:
                once(base)
            except Exception as exc:
                print(f"azan-sip-security: {exc}", file=sys.stderr, flush=True)
        time.sleep(2)


def main(argv: list[str]) -> int:
    global FIREWALL
    if len(argv) != 3 or argv[1] not in ("apply", "watch"):
        print("usage: security-helper.py apply /opt/azan-sip   (Debian, started by systemd)\n"
              "       security-helper.py watch /data           (Docker)", file=sys.stderr)
        return 2
    base = Path(argv[2])
    if os.geteuid() != 0:
        print("Please run as root", file=sys.stderr)
        return 1
    if argv[1] == "watch":
        return watch(base)
    FIREWALL = firewall_kind()
    # A change made while this was running is applied by going round again (systemd doesn't start
    # a second copy while this one runs).
    for _ in range(5):
        seen = (mtime(base / "security" / "settings.json"), mtime(base / "security" / "request.json"))
        try:
            once(base)
        except Exception as exc:  # report it on the Security page instead of failing the unit
            print(f"azan-sip-security: {exc}", file=sys.stderr)
        if seen == (mtime(base / "security" / "settings.json"), mtime(base / "security" / "request.json")):
            break
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
