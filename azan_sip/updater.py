"""Self-update from the GitHub repository.

Your settings (config.toml), recordings (audio/), timetable.csv and logs are never touched:
only the program files are replaced. The previous version is kept in backups/ so it can be
restored from the control panel. A box installed with packaging/box/install.sh updates to each new
AthanBox release (tagged box-vX.Y.Z, the same versions the Docker image gets). If the folder is a git
clone and git is installed, a normal `git pull` of main is used instead.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

log = logging.getLogger("azan-sip")

# AthanBox updates from its own repository, never from the desktop app's (ATHAN-TO-SIP),
# so app releases and AthanBox releases can't reach each other's users.
REPO = "Fudgeamin99/azan-sip-box"
BRANCH = "main"
REPO_URL = f"https://github.com/{REPO}"
API = os.environ.get("AZAN_UPDATE_API") or f"https://api.github.com/repos/{REPO}"  # override only for testing
UA = {"User-Agent": f"azan-sip/{__version__}", "Accept": "application/vnd.github+json"}

# Never overwritten or deleted by an update.
USER_FILES = {"config.toml", "config.toml.bak", "config.tmp", "timetable.csv", ".installed.json", ".sessions.json",
              ".sip_bindings.json", "kids.json", "kids.tmp"}
USER_DIRS = {"audio", "auth", "backups", "security", ".git", "__pycache__"}
KEEP_BACKUPS = 3

# The Docker image sets AZAN_DOCKER=1: there the program is replaced by pulling a new image,
# because files changed inside a container are lost when it is recreated.
DOCKER = os.environ.get("AZAN_DOCKER") == "1"
DOCKER_NOTE = ("Running in Docker: to update, pull the new image with 'docker compose pull' and then "
               "'docker compose up -d'. Your settings, recordings and password are kept. For an Update "
               "button here, add the updater service from compose.yaml (see \"Update button\" in the README).")
# The Update button in Docker asks the updater container (azan_sip/dockerupdate.py, the "updater"
# service in compose.yaml) to pull the new image and replace this container.
HELPER_URL = os.environ.get("AZAN_UPDATER_URL", "http://127.0.0.1:8770")
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# AthanBox releases are tagged box-vX.Y.Z. AthanBox's version is the newest "## x.y.z - title" heading of
# BOX_CHANGELOG.md, which ships next to the azan_sip folder; __version__ is the desktop app's.
TAG_PREFIX = "box-v"
PROGRAM_DIR = Path(__file__).resolve().parent.parent
_CHANGELOG_VERSION = re.compile(r"^## (\d+\.\d+\.\d+)(?: |$)", re.M)


def _changelog_version(text: str) -> str | None:
    m = _CHANGELOG_VERSION.search(text or "")
    return m.group(1) if m else None


def box_version(root: Path | None = None) -> str | None:
    """AthanBox version of the program in root (default: the one running), or None if unknown."""
    try:
        return _changelog_version(Path(root or PROGRAM_DIR, "BOX_CHANGELOG.md").read_text(encoding="utf-8"))
    except OSError:
        return None


class HelperMissing(Exception):
    """The updater container isn't set up (or isn't running)."""


def helper_token_file(base: Path) -> Path:
    return Path(base) / "auth" / "updater-token"


def ensure_helper_token(base: Path):
    """Docker: the secret the updater container checks before it does anything."""
    f = helper_token_file(base)
    if f.is_file() and f.read_text(encoding="utf-8").strip():
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass


def _helper(base: Path, method: str, path: str, timeout: float = 10) -> dict:
    try:
        token = helper_token_file(base).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HelperMissing() from exc
    req = urllib.request.Request(HELPER_URL + path, method=method, data=b"{}" if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with _NO_PROXY.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read()).get("error")
        except ValueError:
            msg = None
        raise RuntimeError(f"The updater couldn't do that: {msg}" if msg else f"The updater answered {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise HelperMissing() from exc


def docker_helper(base: Path) -> dict | None:
    """The updater container's status, or None when it isn't set up."""
    try:
        return _helper(base, "GET", "/status", timeout=3)
    except HelperMissing:
        return None
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}


def _get(url: str, timeout: float = 20, raw: bool = False, accept: str | None = None, not_found: str | None = None):
    req = urllib.request.Request(url, headers=dict(UA, **({"Accept": accept} if accept else {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 403 and "rate limit" in exc.read().decode(errors="ignore").lower():
            raise RuntimeError("GitHub's hourly limit was reached - try again in an hour") from exc
        if exc.code == 404:
            raise RuntimeError(not_found or f"Couldn't find {REPO} on GitHub (is it still public?)") from exc
        if exc.code == 409:
            raise RuntimeError(f"Nothing has been published to {REPO} on GitHub yet, so there are no "
                               "updates to install") from exc
        raise RuntimeError(f"GitHub returned an error ({exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("Couldn't reach GitHub - check this PC's internet connection") from exc
    return data if raw else json.loads(data)


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:4])


def latest_release() -> dict:
    """The newest published AthanBox release: its version, tag, commit, title, date and page."""
    r = _get(f"{API}/releases/latest",
             not_found="No AthanBox release has been published on GitHub yet, so there are no updates to install")
    tag = r.get("tag_name") or ""
    if not tag.startswith(TAG_PREFIX):
        raise RuntimeError(f"The newest release on GitHub ({tag or 'untagged'}) isn't an AthanBox release")
    version = tag[len(TAG_PREFIX):]
    return {"tag": tag, "version": version, "commit": _get(f"{API}/commits/{tag}")["sha"],
            "title": (r.get("name") or "").removeprefix(f"{version} - ").strip(),
            "date": r.get("published_at") or "", "url": r.get("html_url") or f"{REPO_URL}/releases/tag/{tag}"}


def _installed(base: Path) -> dict:
    try:
        return json.loads((base / ".installed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _is_git(base: Path) -> bool:
    return (base / ".git").is_dir() and shutil.which("git") is not None


def mode(base: Path) -> str:
    if DOCKER:
        return "docker"
    return "git" if _is_git(base) else "download"


def _git(base: Path, *args, timeout=120) -> str:
    r = subprocess.run(["git", "-C", str(base), *args], capture_output=True, text=True, timeout=timeout)
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
                out.append({"name": p.name, "version": box_version(p) or (m.group(1) if m else "?")})
    return out


# --------------------------------------------------------------------- check
def check(base: Path) -> dict:
    base = Path(base)
    inst = _installed(base)
    info = {"repo": REPO_URL, "current_version": box_version() or __version__, "current_commit": inst.get("commit"),
            "mode": mode(base), "checked_at": datetime.now().isoformat(timespec="seconds"),
            "backups": backups(base)}
    if DOCKER:
        h = docker_helper(base)
        if not h:
            return dict(info, available=False, note=DOCKER_NOTE)
        if not h.get("ok"):
            raise RuntimeError(h.get("error") or "The updater isn't working")
        r = _helper(base, "POST", "/check", timeout=1800)
        return dict(info, helper=True, available=bool(r.get("available")), current_version=r.get("current") or __version__,
                    latest_version=r.get("latest"), latest_date=r.get("latest_date", ""), image=r.get("image"))
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
        try:
            latest = _changelog_version(_git(base, "show", f"origin/{BRANCH}:BOX_CHANGELOG.md"))
        except RuntimeError:
            latest = None
        if not latest:
            m = re.search(r'__version__\s*=\s*"([^"]+)"', _git(base, "show", f"origin/{BRANCH}:azan_sip/__init__.py"))
            latest = m.group(1) if m else "?"
        info.update(current_commit=head, latest_commit=remote, latest_message=msg[0],
                    latest_date=msg[1] if len(msg) > 1 else "", latest_version=latest,
                    available=behind > 0 or changed)
        return info

    rel = latest_release()
    current = box_version()  # None: installed before AthanBox had its own version number
    info.update(current_version=current or "an earlier version", latest_version=rel["version"],
                latest_message=rel["title"], latest_date=rel["date"], latest_commit=rel["commit"],
                latest_url=rel["url"], available=not current or _vtuple(rel["version"]) > _vtuple(current))
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
    if not (root / "azan_sip/auth.py").is_file():  # Box mode lives there; without it AthanBox can't start
        raise RuntimeError("That version doesn't include AthanBox support - update cancelled")
    r = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                        "import azan_sip.web, azan_sip.praytimes, azan_sip.sip, azan_sip.auth; print(azan_sip.__version__)", str(root)],
                       capture_output=True, text=True, timeout=60, cwd=str(root))
    if r.returncode != 0:
        raise RuntimeError("The new version failed its start-up check, so nothing was changed:\n"
                           + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
    return box_version(root) or r.stdout.strip()


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
        os.replace(tmp, target)
    # remove stale modules from the package (never user files)
    pkg = base / "azan_sip"
    for p in list(pkg.rglob("*")):
        rel = p.relative_to(base)
        if p.is_file() and "__pycache__" not in rel.parts and rel not in new:
            p.unlink(missing_ok=True)


def apply(base: Path) -> dict:
    base = Path(base)
    if DOCKER:  # the updater container pulls the image and replaces this container
        try:
            r = _helper(base, "POST", "/update", timeout=1800)
        except HelperMissing as exc:
            raise RuntimeError(DOCKER_NOTE) from exc
        if not r.get("updated"):
            raise RuntimeError(f"Already on the newest image ({r.get('version')})")
        log.info("Updating the container to %s - it restarts in a moment", r.get("version"))
        return {"ok": True, "version": r.get("version"), "docker": True}
    if _is_git(base):
        before = _git(base, "rev-parse", "HEAD")
        _backup(base, box_version() or __version__)
        try:
            _git(base, "fetch", "--quiet", "origin", BRANCH)
            # Program files only; settings/recordings/logs are untracked (.gitignore) so they are untouched.
            _git(base, "reset", "--hard", "--quiet", f"origin/{BRANCH}")
        except RuntimeError as exc:
            raise RuntimeError(f"git update failed: {exc}") from exc
        version = _verify(base)
        log.info("Updated with git pull: %s -> %s", before[:7], _git(base, "rev-parse", "--short", "HEAD"))
        return {"ok": True, "version": version}

    rel = latest_release()
    log.info("Downloading %s %s", REPO, rel["tag"])
    data = _get(f"https://codeload.github.com/{REPO}/zip/refs/tags/{rel['tag']}", timeout=120, raw=True)
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
        backup = _backup(base, box_version() or __version__)
        try:
            _install_tree(root, base)
        except Exception as exc:
            log.error("Update failed while copying (%s) - restoring the previous version", exc)
            _install_tree(backup, base)
            raise RuntimeError(f"Update failed and was undone: {exc}") from exc
    (base / ".installed.json").write_text(json.dumps({
        "commit": rel["commit"], "version": version, "tag": rel["tag"],
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": rel["url"]}, indent=2),
        encoding="utf-8")
    log.info("Updated to version %s (%s)", version, rel["tag"])
    return {"ok": True, "version": version, "commit": rel["commit"]}


def rollback(base: Path, name: str | None = None) -> dict:
    base = Path(base)
    if DOCKER:
        raise RuntimeError(DOCKER_NOTE)
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
def restart_command(base: Path) -> list[str] | None:
    """Command to start a fresh copy of the app, or None under systemd."""
    if os.environ.get("INVOCATION_ID"):  # systemd: just exit, Restart=always brings us back
        return None
    exe = Path(sys.executable)
    orig = getattr(sys, "orig_argv", None) or [str(exe), *sys.argv]
    return [str(exe), *orig[1:]]


def spawn_replacement(base: Path) -> bool:
    args = restart_command(base)
    if args is None:
        return False
    env = dict(os.environ, AZAN_WAIT_FOR_PORT="1", AZAN_NO_BROWSER="1")
    subprocess.Popen(args, cwd=str(base), env=env, close_fds=True, stdin=subprocess.DEVNULL, start_new_session=True)
    return True
