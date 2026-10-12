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
from .env import EXE_NAME, FROZEN

log = logging.getLogger("azan-sip")

REPO = "Fudgeamin99/ATHAN-TO-SIP"
BRANCH = "main"
REPO_URL = f"https://github.com/{REPO}"
API = os.environ.get("AZAN_UPDATE_API") or f"https://api.github.com/repos/{REPO}"  # override only for testing
UA = {"User-Agent": f"azan-sip/{__version__}", "Accept": "application/vnd.github+json"}

# Never overwritten or deleted by an update.
USER_FILES = {"config.toml", "config.toml.bak", "config.tmp", "timetable.csv", ".installed.json"}
USER_DIRS = {"audio", "backups", ".git", "__pycache__"}
KEEP_BACKUPS = 3


def _get(url: str, timeout: float = 20, raw: bool = False, accept: str | None = None):
    req = urllib.request.Request(url, headers=dict(UA, **({"Accept": accept} if accept else {})))
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
    return not FROZEN and (base / ".git").is_dir() and shutil.which("git") is not None


def mode(base: Path) -> str:
    return "exe" if FROZEN else ("git" if _is_git(base) else "download")


def _git(base: Path, *args, timeout=120) -> str:
    r = subprocess.run(["git", "-C", str(base), *args], capture_output=True, text=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"git {args[0]} failed")
    return r.stdout.strip()


def backups(base: Path) -> list[dict]:
    if FROZEN:
        return _exe_backups(Path(base))
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
            "mode": mode(base), "checked_at": datetime.now().isoformat(timespec="seconds"),
            "backups": backups(base)}
    if FROZEN:
        return _exe_check(info)
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
    init_py = _get(f"{API}/contents/azan_sip/__init__.py?ref={sha}", raw=True,
                   accept="application/vnd.github.raw").decode()
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
    if FROZEN:
        return _exe_apply(base)
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
    if FROZEN:
        return _exe_rollback(base, name)
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
    if FROZEN:
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        return [str(exe), "--background"], flags
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
    env = child_env(AZAN_WAIT_FOR_PORT="1", AZAN_NO_BROWSER="1")
    kw = {"cwd": str(base), "env": env, "close_fds": True, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = flags
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)
    return True


# ------------------------------------------------------------ packaged .exe
# The .exe updates itself from the latest GitHub *release* (each release has AzanSIP.exe attached).
# Windows lets a running .exe be renamed but not overwritten, so: download as .new.exe, test it,
# keep a copy of the current one in backups/, rename current -> .old.exe, new -> current, restart.

def child_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not (k.startswith("_PYI_") or k == "_MEIPASS2")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env.update(extra)
    return env


def _exe_paths() -> tuple[Path, Path, Path]:
    exe = Path(sys.executable).resolve()
    return exe, exe.with_name(exe.stem + ".new" + exe.suffix), exe.with_name(exe.stem + ".old" + exe.suffix)


def _exe_check(info: dict) -> dict:
    r = _get(f"{API}/releases/latest")
    latest = r.get("tag_name", "").lstrip("v")
    asset = next((a for a in r.get("assets", []) if a.get("name") == EXE_NAME), None)
    newer = _vtuple(latest) > _vtuple(__version__)
    info.update(latest_version=latest, latest_commit=r.get("tag_name"), latest_message=r.get("name") or "",
                latest_date=r.get("published_at") or "", available=newer and asset is not None,
                asset_url=asset.get("url") if asset else None, asset_size=asset.get("size") if asset else None)
    if newer and asset is None:
        info["note"] = "The new version is still being built on GitHub - check again in a few minutes."
    return info


def _verify_exe(path: Path):
    try:
        r = subprocess.run([str(path), "--self-test"], capture_output=True, timeout=180, env=child_env(),
                           stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ok = r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        path.unlink(missing_ok=True)
        raise RuntimeError("The new version failed its start-up check, so nothing was changed")


def _swap_in(new: Path):
    exe, _, old = _exe_paths()
    try:
        old.unlink(missing_ok=True)  # left over from the previous update
    except OSError:
        old = exe.with_name(f"{exe.stem}.old-{int(time.time())}{exe.suffix}")
    os.replace(exe, old)             # allowed while running on Windows
    try:
        os.replace(new, exe)
    except OSError:
        os.replace(old, exe)         # put the working one back
        raise


def _backup_exe(base: Path, version: str):
    exe = Path(sys.executable).resolve()
    d = base / "backups"
    d.mkdir(exist_ok=True)
    shutil.copy2(exe, d / f"AzanSIP-v{version}.exe")
    olds = sorted(d.glob("AzanSIP-v*.exe"), key=lambda p: p.stat().st_mtime)
    for p in olds[:-KEEP_BACKUPS]:
        p.unlink(missing_ok=True)


def _exe_backups(base: Path) -> list[dict]:
    d = base / "backups"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("AzanSIP-v*.exe"), key=lambda p: p.stat().st_mtime, reverse=True):
        m = re.search(r"AzanSIP-v(.+)\.exe$", p.name)
        if m and m.group(1) != __version__:
            out.append({"name": p.name, "version": m.group(1)})
    return out


def _exe_apply(base: Path) -> dict:
    info = _exe_check({})
    if not info.get("available"):
        raise RuntimeError(info.get("note") or f"You already have the latest version ({__version__})")
    _, new, _ = _exe_paths()
    log.info("Downloading %s %s", EXE_NAME, info["latest_version"])
    data = _get(info["asset_url"], timeout=600, raw=True, accept="application/octet-stream")
    if info.get("asset_size") and len(data) != info["asset_size"]:
        raise RuntimeError("The download was incomplete - please try again")
    new.write_bytes(data)
    if os.name != "nt":
        new.chmod(0o755)
    _verify_exe(new)
    _backup_exe(base, __version__)
    _swap_in(new)
    log.info("Updated %s to version %s", EXE_NAME, info["latest_version"])
    return {"ok": True, "version": info["latest_version"]}


def _exe_rollback(base: Path, name: str | None) -> dict:
    bk = _exe_backups(base)
    if not bk:
        raise RuntimeError("There is no previous version to go back to")
    pick = next((b for b in bk if b["name"] == name), bk[0]) if name else bk[0]
    _, new, _ = _exe_paths()
    shutil.copy2(base / "backups" / pick["name"], new)
    _verify_exe(new)
    _backup_exe(base, __version__)
    _swap_in(new)
    log.info("Restored %s version %s", EXE_NAME, pick["version"])
    return {"ok": True, "version": pick["version"]}


def cleanup_after_update():
    """Remove the previous .exe left behind by an update (called when the new one starts)."""
    if not FROZEN:
        return
    exe, new, _ = _exe_paths()
    for p in [new, *exe.parent.glob(f"{exe.stem}.old*{exe.suffix}")]:
        for _ in range(10):  # the old process may take a moment to exit
            try:
                p.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.5)
