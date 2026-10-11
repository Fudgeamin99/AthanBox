"""Self-update from the GitHub repository.

Your settings (config.toml), recordings (audio/), timetable.csv and logs are never touched:
only the program files are replaced. The previous version is kept in backups/ so it can be
restored from the control panel. If the folder is a git clone and git is installed, a normal
`git pull` is used instead of downloading a zip.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

log = logging.getLogger("azan-sip")

REPO = "Fudgeamin99/SIP-TO-ATHAN"
BRANCH = "main"
REPO_URL = f"https://github.com/{REPO}"
API = f"https://api.github.com/repos/{REPO}"
UA = {"User-Agent": f"azan-sip/{__version__}", "Accept": "application/vnd.github+json"}

# Never overwritten or deleted by an update.
USER_FILES = {"config.toml", "config.toml.bak", "config.tmp", "timetable.csv", ".installed.json"}
USER_DIRS = {"audio", "backups", ".git", "__pycache__"}
KEEP_BACKUPS = 3


def _get(url: str, timeout: float = 20, raw: bool = False):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 403 and "rate limit" in exc.read().decode(errors="ignore").lower():
            raise RuntimeError("GitHub's hourly limit was reached - try again in an hour") from exc
        if exc.code == 404:
            raise RuntimeError(f"Couldn't find {REPO} on GitHub (is it still public?)") from exc
        if exc.code == 409:
            raise RuntimeError(f"Nothing has been published to {REPO} on GitHub yet, so there are no "
                               "updates to install") from exc
        raise RuntimeError(f"GitHub returned an error ({exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Couldn't reach GitHub - check this PC's internet connection") from exc
    return data if raw else json.loads(data)


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:4])


def _installed(base: Path) -> dict:
    try:
        return json.loads((base / ".installed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _is_git(base: Path) -> bool:
    return (base / ".git").is_dir() and shutil.which("git") is not None


def _git(base: Path, *args, timeout=120) -> str:
    r = subprocess.run(["git", "-C", str(base), *args], capture_output=True, text=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"git {args[0]} failed")
    return r.stdout.strip()


def backups(base: Path) -> list[dict]:
    d = base / "backups"
    out = []
    if d.is_dir():
        for p in sorted(d.iterdir(), reverse=True):
            if (p / "azan_sip" / "__init__.py").is_file():
                m = re.search(r'__version__\s*=\s*"([^"]+)"', (p / "azan_sip" / "__init__.py").read_text("utf-8"))
                out.append({"name": p.name, "version": m.group(1) if m else "?"})
    return out


# --------------------------------------------------------------------- check
def check(base: Path) -> dict:
    base = Path(base)
    inst = _installed(base)
    info = {"repo": REPO_URL, "current_version": __version__, "current_commit": inst.get("commit"),
            "mode": "git" if _is_git(base) else "download", "checked_at": datetime.now().isoformat(timespec="seconds"),
            "backups": backups(base)}
    if info["mode"] == "git":
        try:
            _git(base, "fetch", "--quiet", "origin", BRANCH)
        except RuntimeError as exc:
            if "couldn't find remote ref" in str(exc):
                raise RuntimeError(f"Nothing has been published to {REPO} on GitHub yet, so there are no "
                                   "updates to install") from exc
            raise
        head = _git(base, "rev-parse", "HEAD")
        remote = _git(base, "rev-parse", f"origin/{BRANCH}")
        behind = int(_git(base, "rev-list", "--count", f"HEAD..origin/{BRANCH}") or 0)
        changed = bool(_git(base, "status", "--porcelain", "--untracked-files=no"))  # e.g. after a Restore
        msg = _git(base, "log", "-1", "--format=%s%x00%cI", f"origin/{BRANCH}").split("\x00")
        init_py = _git(base, "show", f"origin/{BRANCH}:azan_sip/__init__.py")
        m = re.search(r'__version__\s*=\s*"([^"]+)"', init_py)
        info.update(current_commit=head, latest_commit=remote, latest_message=msg[0],
                    latest_date=msg[1] if len(msg) > 1 else "", latest_version=m.group(1) if m else "?",
                    available=behind > 0 or changed)
        return info

    c = _get(f"{API}/commits/{BRANCH}")
    sha = c["sha"]
    init_py = _get(f"https://raw.githubusercontent.com/{REPO}/{sha}/azan_sip/__init__.py", raw=True).decode()
    m = re.search(r'__version__\s*=\s*"([^"]+)"', init_py)
    latest = m.group(1) if m else "0"
    if inst.get("commit"):
        available = inst["commit"] != sha
    else:  # installed by hand: go by the version number
        available = _vtuple(latest) > _vtuple(__version__)
    info.update(latest_commit=sha, latest_version=latest,
                latest_message=c["commit"]["message"].splitlines()[0],
                latest_date=c["commit"]["committer"]["date"], available=available)
    return info


# --------------------------------------------------------------------- apply
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".mp4"}


def _program_files(root: Path) -> list[Path]:
    """Files that belong to the program (relative paths) - never settings, recordings or logs."""
    out = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if not p.is_file() or rel.parts[0] in USER_DIRS or rel.name in USER_FILES:
            continue
        if "__pycache__" in rel.parts or rel.suffix in (".pyc", ".new", ".tmp") or rel.name.startswith("azan.log"):
            continue
        if len(rel.parts) == 1 and (rel.suffix.lower() in AUDIO_EXT or p.stat().st_size > 5_000_000):
            continue  # someone's own files left next to the program
        out.append(rel)
    return out


def _verify(root: Path):
    """Make sure the new version is complete and starts before switching to it."""
    for need in ("azan_sip/__init__.py", "azan_sip/web.py", "azan_sip/static/index.html"):
        if not (root / need).is_file():
            raise RuntimeError(f"The download looks incomplete ({need} missing) - update cancelled")
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe") and Path(exe[:-5] + ".exe").exists():
        exe = exe[:-5] + ".exe"  # pythonw has no output to read
    r = subprocess.run([exe, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                        "import azan_sip.web, azan_sip.praytimes, azan_sip.sip; print(azan_sip.__version__)", str(root)],
                       capture_output=True, text=True, timeout=60, cwd=str(root),
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError("The new version failed its start-up check, so nothing was changed:\n"
                           + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
    return r.stdout.strip()


def _backup(base: Path, version: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = base / "backups" / f"{stamp}-v{version}"
    dest.mkdir(parents=True, exist_ok=True)
    for rel in _program_files(base):
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(base / rel, dest / rel)
    olds = sorted((base / "backups").iterdir())
    for old in olds[:-KEEP_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)
    return dest


def _install_tree(src: Path, base: Path):
    """Copy program files from src into base; remove program files that no longer exist."""
    new = set(_program_files(src))
    for rel in sorted(new):
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".new")
        shutil.copy2(src / rel, tmp)
        for attempt in range(5):  # Windows: retry if antivirus briefly locks a file
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.5)
    # remove stale modules from the package (never user files)
    pkg = base / "azan_sip"
    for p in list(pkg.rglob("*")):
        rel = p.relative_to(base)
        if p.is_file() and "__pycache__" not in rel.parts and rel not in new:
            p.unlink(missing_ok=True)


def apply(base: Path) -> dict:
    base = Path(base)
    if _is_git(base):
        before = _git(base, "rev-parse", "HEAD")
        _backup(base, __version__)
        try:
            _git(base, "fetch", "--quiet", "origin", BRANCH)
            # Program files only; settings/recordings/logs are untracked (.gitignore) so they are untouched.
            _git(base, "reset", "--hard", "--quiet", f"origin/{BRANCH}")
        except RuntimeError as exc:
            raise RuntimeError(f"git update failed: {exc}") from exc
        version = _verify(base)
        log.info("Updated with git pull: %s -> %s", before[:7], _git(base, "rev-parse", "--short", "HEAD"))
        return {"ok": True, "version": version}

    c = _get(f"{API}/commits/{BRANCH}")
    sha = c["sha"]
    log.info("Downloading %s @ %s", REPO, sha[:7])
    data = _get(f"https://codeload.github.com/{REPO}/zip/{sha}", timeout=120, raw=True)
    with tempfile.TemporaryDirectory(prefix="azan-update-") as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():  # refuse paths that escape the folder
                if name.startswith("/") or ".." in Path(name).parts:
                    raise RuntimeError("The download contains unsafe paths - update cancelled")
            z.extractall(tmp)
        roots = [p for p in tmp.iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise RuntimeError("Unexpected download layout - update cancelled")
        root = roots[0]
        version = _verify(root)
        backup = _backup(base, __version__)
        try:
            _install_tree(root, base)
        except Exception as exc:
            log.error("Update failed while copying (%s) - restoring the previous version", exc)
            _install_tree(backup, base)
            raise RuntimeError(f"Update failed and was undone: {exc}") from exc
    (base / ".installed.json").write_text(json.dumps({
        "commit": sha, "version": version, "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": f"{REPO_URL}/commit/{sha}"}, indent=2), encoding="utf-8")
    log.info("Updated to version %s (%s)", version, sha[:7])
    return {"ok": True, "version": version, "commit": sha}


def rollback(base: Path, name: str | None = None) -> dict:
    base = Path(base)
    bk = backups(base)
    if not bk:
        raise RuntimeError("There is no previous version to go back to")
    pick = next((b for b in bk if b["name"] == name), bk[0]) if name else bk[0]
    src = base / "backups" / pick["name"]
    _verify(src)
    _install_tree(src, base)
    (base / ".installed.json").unlink(missing_ok=True)  # unknown commit now; go by version number
    log.info("Restored previous version %s from backups/%s", pick["version"], pick["name"])
    return {"ok": True, "version": pick["version"]}


# ------------------------------------------------------------------- restart
def restart_command(base: Path) -> tuple[list[str], int] | None:
    """Command (and Windows creation flags) to start a fresh copy of the app, or None under systemd."""
    if os.environ.get("INVOCATION_ID"):  # systemd: just exit, Restart=always brings us back
        return None
    exe = Path(sys.executable)
    launcher = base / "Azan SIP.pyw"
    argv0 = Path(sys.argv[0]).name if sys.argv else ""
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | (
            subprocess.DETACHED_PROCESS if exe.name.lower() == "pythonw.exe" else subprocess.CREATE_NEW_CONSOLE)
    if argv0.lower().endswith(".pyw") and launcher.exists():
        return [str(exe), str(launcher), "--background"], flags
    orig = getattr(sys, "orig_argv", None) or [str(exe), *sys.argv]
    return [str(exe), *orig[1:]], flags


def spawn_replacement(base: Path) -> bool:
    cmd = restart_command(base)
    if cmd is None:
        return False
    args, flags = cmd
    env = dict(os.environ, AZAN_WAIT_FOR_PORT="1", AZAN_NO_BROWSER="1")
    kw = {"cwd": str(base), "env": env, "close_fds": True, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = flags
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)
    return True
