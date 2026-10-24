"""Sites: one AthanBox server for many mosques or homes, each with its own settings.

Enterprise is one site: the AthanBox you know. Group is many sites, filed under groups you name
(London, Manchester ...). The first site keeps AthanBox's own folder; every other site has a folder
of its own under sites/<code>/ with its own config.toml, recordings, time zone and TV screens, and
its own control panel at /s/<code>/.

sites.json in AthanBox's folder lists the groups and sites; each site's settings stay in its own folder.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

log = logging.getLogger("azan-sip")

MAIN = "main"            # the first site, in AthanBox's own folder
MODES = ("enterprise", "group")
CODE_RE = re.compile(r"^[a-z][a-z0-9]{1,11}$")   # also goes in front of a phone's username (wcp-201)
RESERVED = {MAIN, "api", "static", "sites", "tv", "admin", "all"}
MAX_SITES = 200
MAX_GROUPS = 50
NAME_MAX = 60


# -- whose log line is it: each site's control panel shows its own activity log
def current() -> str | None:
    """The site the running thread works for (None: the whole server)."""
    return getattr(threading.current_thread(), "azan_site", None)


def set_current(code: str | None):
    threading.current_thread().azan_site = code


def track_threads():
    """A new thread works for the same site as the thread that started it (calls, the azan, thread pools),
    so the log lines it writes show in that site's control panel."""
    if getattr(threading.Thread, "_azan_site_tracked", False):
        return
    init = threading.Thread.__init__

    def __init__(self, *args, **kwargs):
        init(self, *args, **kwargs)
        self.azan_site = current()

    threading.Thread.__init__ = __init__
    threading.Thread._azan_site_tracked = True


class SiteError(ValueError):
    """A friendly message for the Sites page."""


def _name(value, what: str) -> str:
    v = " ".join(str(value or "").split())[:NAME_MAX]
    if not v:
        raise SiteError(f"Give the {what} a name")
    return v


def check_code(code) -> str:
    code = str(code or "").strip().lower()
    if not CODE_RE.fullmatch(code):
        raise SiteError("The site code is 2 to 12 letters or numbers, starting with a letter (say wcp)")
    if code in RESERVED:
        raise SiteError(f"“{code}” can't be a site code, so choose another")
    return code


class Sites:
    """The groups and sites, and a running control panel (web.Service) for each site.

    root is the first site's Service; make(code, folder, cfg) returns the Service for another site,
    writing cfg as its settings first when it is a new site."""

    def __init__(self, root, make):
        self.root = root
        self.make = make
        self.file = root.base / "sites.json"
        self.dir = root.base / "sites"
        self.lock = threading.RLock()
        self.services = {MAIN: root}
        self.data = self._read()
        for s in self.data["sites"]:
            if s["code"] != MAIN:
                self._start(s["code"])

    # -- sites.json
    def _read(self) -> dict:
        data = {}
        if self.file.exists():
            try:
                data = json.loads(self.file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                log.error("Couldn't read %s (%s), so only the first site is running", self.file.name, exc)
                bad = self.file.with_suffix(f".broken-{int(time.time())}.json")
                shutil.copyfile(self.file, bad)
        mode = data.get("mode") if data.get("mode") in MODES else "enterprise"
        groups = [{"id": str(g["id"]), "name": str(g["name"])} for g in data.get("groups") or []
                  if isinstance(g, dict) and g.get("id") and g.get("name")][:MAX_GROUPS]
        ids = {g["id"] for g in groups}
        sites, seen = [], set()
        for s in data.get("sites") or []:
            if not isinstance(s, dict) or s.get("code") in seen:
                continue
            code = str(s.get("code") or "")
            if code != MAIN and not (CODE_RE.fullmatch(code) and (self.dir / code / "config.toml").is_file()):
                continue
            seen.add(code)
            sites.append({"code": code, "name": str(s.get("name") or code), "group": s.get("group") if s.get("group") in ids else ""})
        if MAIN not in seen:
            sites.insert(0, {"code": MAIN, "name": "Main site", "group": ""})
        return {"mode": mode, "groups": groups, "sites": sites[:MAX_SITES]}

    def _save(self):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.file)

    def _start(self, code: str):
        was = current()
        set_current(code)
        try:
            self.services[code] = self.make(code, self.dir / code, None)
        except Exception as exc:   # one broken site never stops the others
            log.error("Site %s couldn't start: %s", code, exc)
        finally:
            set_current(was)

    # -- reading
    @property
    def mode(self) -> str:
        return self.data["mode"]

    def get(self, code: str):
        """The control panel of a site, or None."""
        with self.lock:
            return self.services.get(code)

    def site(self, code: str) -> dict | None:
        with self.lock:
            s = next((s for s in self.data["sites"] if s["code"] == code), None)
            if not s:
                return None
            g = next((g for g in self.data["groups"] if g["id"] == s["group"]), None)
            return dict(s, group_name=g["name"] if g else "")

    def all_services(self) -> list:
        with self.lock:
            return list(self.services.values())

    def info(self) -> dict:
        """The Sites page: groups, and each site with how it is doing."""
        with self.lock:
            sites = [dict(s) for s in self.data["sites"]]
            data = {"mode": self.mode, "groups": [dict(g) for g in self.data["groups"]]}
        for s in sites:
            svc = self.get(s["code"])
            s["main"] = s["code"] == MAIN
            s.update(svc.summary() if svc else {"error": "This site couldn't start - see the log"})
        data["sites"] = sites
        return data

    # -- changing
    def set_mode(self, mode: str):
        if mode not in MODES:
            raise SiteError("Choose Enterprise or Group")
        with self.lock:
            if mode == "enterprise" and len(self.data["sites"]) > 1:
                raise SiteError("Remove the other sites first: Enterprise runs one site")
            self.data["mode"] = mode
            self._save()

    def save_group(self, req: dict) -> dict:
        name = _name(req.get("name"), "group")
        with self.lock:
            gid = str(req.get("id") or "")
            g = next((g for g in self.data["groups"] if g["id"] == gid), None) if gid else None
            if any(x["name"].lower() == name.lower() and x is not g for x in self.data["groups"]):
                raise SiteError(f"There is already a group called {name}")
            if g:
                g["name"] = name
            else:
                if len(self.data["groups"]) >= MAX_GROUPS:
                    raise SiteError(f"AthanBox has room for {MAX_GROUPS} groups")
                n = 1 + max([int(x["id"][1:]) for x in self.data["groups"] if x["id"][1:].isdigit()] or [0])
                g = {"id": f"g{n}", "name": name}
                self.data["groups"].append(g)
            self._save()
            return dict(g)

    def delete_group(self, gid: str):
        """The group's sites carry on, outside any group."""
        with self.lock:
            if not any(g["id"] == gid for g in self.data["groups"]):
                raise SiteError("That group isn't there any more")
            self.data["groups"] = [g for g in self.data["groups"] if g["id"] != gid]
            for s in self.data["sites"]:
                if s["group"] == gid:
                    s["group"] = ""
            self._save()

    def _group(self, gid) -> str:
        gid = str(gid or "")
        if gid and not any(g["id"] == gid for g in self.data["groups"]):
            raise SiteError("That group isn't there any more")
        return gid

    def add(self, req: dict, first_settings) -> dict:
        """A new site. first_settings(timezone) gives its settings: the first site's, without its phones."""
        name = _name(req.get("name"), "site")
        code = check_code(req.get("code"))
        tz = str(req.get("timezone") or "").strip()
        with self.lock:
            if self.mode != "group":
                raise SiteError("Switch to Group to add more sites")
            if len(self.data["sites"]) >= MAX_SITES:
                raise SiteError(f"AthanBox has room for {MAX_SITES} sites")
            if any(s["code"] == code for s in self.data["sites"]):
                raise SiteError(f"The site code {code} is taken, so choose another")
            gid = self._group(req.get("group"))
            folder = self.dir / code
            if folder.exists():   # left over from a site that was removed by hand
                shutil.move(str(folder), str(self._removed_path(code)))
            folder.mkdir(parents=True)
            cfg = first_settings(name, tz)
            was = current()
            set_current(code)
            try:
                self.services[code] = self.make(code, folder, cfg)
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            finally:
                set_current(was)
            site = {"code": code, "name": name, "group": gid}
            self.data["sites"].append(site)
            self._save()
        log.info("Added site %s (%s)", name, code)
        return dict(site)

    def update(self, req: dict) -> dict:
        code = str(req.get("code") or "")
        with self.lock:
            s = next((s for s in self.data["sites"] if s["code"] == code), None)
            if not s:
                raise SiteError("That site isn't there any more")
            if "name" in req:
                s["name"] = _name(req.get("name"), "site")
            if "group" in req:
                s["group"] = self._group(req.get("group"))
            self._save()
            return dict(s)

    def _removed_path(self, code: str) -> Path:
        d = self.dir / ".removed"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{code}-{time.strftime('%Y%m%d-%H%M%S')}"

    def remove(self, code: str):
        """Stop the site and put its folder aside in sites/.removed (nothing is deleted)."""
        if code == MAIN:
            raise SiteError("The first site can't be removed")
        with self.lock:
            if not any(s["code"] == code for s in self.data["sites"]):
                raise SiteError("That site isn't there any more")
            svc = self.services.pop(code, None)
            self.data["sites"] = [s for s in self.data["sites"] if s["code"] != code]
            self._save()
        if svc:
            svc.shutdown()
        folder = self.dir / code
        if folder.exists():
            shutil.move(str(folder), str(self._removed_path(code)))
        log.info("Removed site %s (its folder is kept in sites/.removed)", code)

    def reset(self):
        """Factory reset: every other site is put aside, and it is Enterprise with no groups again."""
        for code in [s["code"] for s in self.data["sites"] if s["code"] != MAIN]:
            self.remove(code)
        with self.lock:
            self.data = {"mode": "enterprise", "groups": [], "sites": [{"code": MAIN, "name": "Main site", "group": ""}]}
            self.file.unlink(missing_ok=True)

    def shutdown(self):
        for code, svc in list(self.services.items()):
            if code != MAIN:
                svc.shutdown()
