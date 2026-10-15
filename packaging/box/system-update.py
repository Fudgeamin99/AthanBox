#!/usr/bin/python3 -I
"""AthanBox: sets up the system parts of each new AthanBox version by itself, as root.

Settings > Updates replaces the program in /opt/azan-sip, but the control panel runs as the
unprivileged user "azan", so it can't install packages or change system services (fail2ban, the
firewall, systemd units). Before this, a version that needed such changes asked the owner to run
a sudo command over SSH.

systemd starts this script (azan-sip-system.service) when /opt/azan-sip/BOX_CHANGELOG.md changes,
which happens on every update and rollback, at start-up, and once an hour. It:

1. reads which AthanBox version is installed (the newest "## x.y.z" heading of that changelog);
2. if this version's system parts haven't been set up yet, downloads release box-vx.y.z from
   github.com/Fudgeamin99/azan-sip-box itself, as root, into a folder only root can write to;
3. runs that release's packaging/box/system-setup.sh, and records the result in
   /var/lib/azan-sip/system.json (the Security page shows it).

The version number is the only thing taken from /opt/azan-sip, which the azan user can change:
the code that runs as root always comes from an official release on GitHub, never from that
folder. This file is installed to /usr/local/lib/azan-sip by system-setup.sh. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = "Fudgeamin99/azan-sip-box"
STATE_DIR = Path("/var/lib/azan-sip")
STATE = STATE_DIR / "system.json"
LOG = STATE_DIR / "system-setup.log"
RETRY_SECONDS = 3600          # after a failure (say no internet), try the same version again an hour later
VERSION = re.compile(r"^## (\d{1,4}\.\d{1,4}\.\d{1,4})(?: |$)", re.M)
UA = {"User-Agent": "azan-sip-box-system-update"}


def installed_version(base: Path) -> str | None:
    """The newest version in BOX_CHANGELOG.md, read without following a symlink."""
    try:
        fd = os.open(base / "BOX_CHANGELOG.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            text = f.read(200_000).decode("utf-8", "replace")
    except OSError:
        return None
    m = VERSION.search(text)
    return m.group(1) if m else None


def read_state() -> dict:
    try:
        s = json.loads(STATE.read_text(encoding="utf-8"))
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(s: dict):
    STATE_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".system.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE)


def download(tag: str, dest: Path) -> Path:
    """The release's source, from GitHub over HTTPS, unpacked into dest. Returns its folder."""
    url = f"https://codeload.github.com/{REPO}/tar.gz/refs/tags/{tag}"
    req = urllib.request.Request(url, headers=UA)
    archive = dest / "release.tar.gz"
    with urllib.request.urlopen(req, timeout=120) as r, open(archive, "wb") as f:
        shutil.copyfileobj(r, f)
    out = dest / "src"
    out.mkdir()
    # GNU tar refuses absolute paths and ".." by default
    subprocess.run(["tar", "-xzf", str(archive), "-C", str(out), "--no-same-owner"], check=True,
                   capture_output=True, timeout=120)
    roots = [p for p in out.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise RuntimeError("unexpected layout in the release download")
    return roots[0]


def setup(version: str) -> tuple[bool, str]:
    tag = f"box-v{version}"
    with tempfile.TemporaryDirectory(prefix="azan-system-") as tmp:   # mode 0700, owned by root
        try:
            root = download(tag, Path(tmp))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return True, f"{tag} isn't a release on GitHub, so there is nothing to set up"
            return False, f"Couldn't download {tag} from GitHub: {exc}"
        except (OSError, urllib.error.URLError, subprocess.SubprocessError, RuntimeError) as exc:
            return False, f"Couldn't download {tag} from GitHub: {exc}"
        script = root / "packaging" / "box" / "system-setup.sh"
        if not script.is_file():
            return True, f"{tag} has no system parts to set up"
        STATE_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} setting up {tag}\n")
            log.flush()
            env = dict(os.environ, DEBIAN_FRONTEND="noninteractive", AZAN_SYSTEM_AUTO="1")
            try:
                r = subprocess.run(["sh", str(script)], cwd=str(root), stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=env, timeout=1800)
            except subprocess.TimeoutExpired:
                return False, f"Setting up {tag} took too long; see {LOG}"
        if r.returncode != 0:
            return False, f"Setting up {tag} failed (exit code {r.returncode}); see {LOG}"
        return True, f"{tag} set up"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: system-update.py /opt/azan-sip", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("Please run as root", file=sys.stderr)
        return 1
    version = installed_version(Path(argv[1]))
    if not version:
        return 0
    s = read_state()
    if s.get("version") == version and s.get("ok"):
        return 0
    if s.get("version") == version and not s.get("ok") and time.time() - float(s.get("tried") or 0) < RETRY_SECONDS:
        return 0
    write_state(dict(s, version=version, ok=False, running=True, tried=time.time(),
                     message=f"Setting up version {version}…"))
    ok, message = setup(version)
    # system-setup.sh records the version itself; keep what it wrote and add the outcome
    s = read_state()
    write_state(dict(s, version=version, ok=ok, running=False, tried=time.time(), message=message,
                     at=time.strftime("%Y-%m-%d %H:%M:%S")))
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
