"""The Update button for the Azan SIP Box in Docker.

A container can't replace itself, so this runs in a small container of its own (the "updater"
service in compose.yaml), from the same image. It is the only part that can reach Docker, through
/var/run/docker.sock. The control panel asks it over 127.0.0.1 only, with a secret token from
/data/auth/updater-token, which the panel writes.

- POST /check   pulls the newest image for the Box's container and says whether it's newer
- POST /update  pulls it too if needed, then replaces the Box's container with one from that image:
                same settings, volumes and network. If the new one won't start, the old one is put back.

Run it with:  python -m azan_sip.dockerupdate
"""
from __future__ import annotations

import hmac
import http.client
import json
import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("azan-sip-updater")

SOCK = os.environ.get("AZAN_DOCKER_SOCKET", "/var/run/docker.sock")
TARGET = os.environ.get("AZAN_UPDATE_TARGET", "azan-sip")          # the Box's container_name
TOKEN_FILE = Path(os.environ.get("AZAN_UPDATER_TOKEN_FILE", "/data/auth/updater-token"))
PORT = int(os.environ.get("AZAN_UPDATER_PORT", "8770"))
LABEL_VERSION = "org.opencontainers.image.version"
LABEL_REVISION = "org.opencontainers.image.revision"


class DockerError(RuntimeError):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s


def docker(method: str, path: str, body: dict | None = None, timeout: float = 60):
    """One Docker Engine API call (unversioned paths: the daemon's own API version)."""
    try:
        c = _UnixConnection(SOCK, timeout)
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
        r = c.getresponse()
        raw = r.read()
        c.close()
    except (OSError, http.client.HTTPException) as exc:
        raise DockerError(f"Can't reach Docker at {SOCK} - is it mounted into the updater? ({exc})") from exc
    if r.status >= 400:
        try:
            msg = json.loads(raw).get("message") or raw.decode(errors="replace")
        except ValueError:
            msg = raw.decode(errors="replace")
        raise DockerError(msg.strip() or f"Docker answered {r.status}")
    if raw and (r.getheader("Content-Type") or "").startswith("application/json"):
        try:
            return json.loads(raw)
        except ValueError:
            pass
    return raw


def container() -> dict:
    try:
        return docker("GET", f"/containers/{quote(TARGET)}/json")
    except DockerError as exc:
        raise DockerError(f"Can't find the container '{TARGET}' ({exc}). Set AZAN_UPDATE_TARGET in compose.yaml "
                          "to the Box's container_name.") from exc


def image(ref: str) -> dict:
    return docker("GET", f"/images/{quote(ref, safe='')}/json")


# Each image's own settings, remembered while it still has its name: once a pull moves the name to
# the new image, Docker may no longer show the old one, but recreate() needs to know which of the
# container's settings came from it.
_CONFIGS: dict[str, dict] = {}


def image_config(image_id: str) -> dict | None:
    if image_id not in _CONFIGS:
        try:
            _CONFIGS[image_id] = image(image_id).get("Config") or {}
        except DockerError:
            return None
    return _CONFIGS[image_id]


def split_ref(ref: str) -> tuple[str, str]:
    if "@" in ref:
        raise DockerError(f"The container uses a fixed image ({ref}), so there is nothing newer to pull")
    name, _, tag = ref.rpartition(":")
    if not name or "/" in tag:  # no tag (a ":" only in a registry port)
        return ref, "latest"
    return name, tag


def pull(ref: str):
    name, tag = split_ref(ref)
    raw = docker("POST", f"/images/create?fromImage={quote(name, safe='')}&tag={quote(tag, safe='')}", timeout=1800)
    for line in (raw if isinstance(raw, bytes) else json.dumps(raw).encode()).splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("error"):
            raise DockerError(f"Couldn't download {ref}: {msg['error']}")


def describe(image_id: str, cfg: dict | None) -> str:
    """A short name for an image: its Box version, or the commit of a test build."""
    labels = (cfg or {}).get("Labels") or {}
    if labels.get(LABEL_VERSION):
        return labels[LABEL_VERSION]
    if labels.get(LABEL_REVISION):
        return f"test build {labels[LABEL_REVISION][:7]}"
    return f"image {image_id.removeprefix('sha256:')[:12]}"


def status() -> dict:
    c = container()
    return {"ok": True, "target": TARGET, "image": c["Config"]["Image"],
            "current": describe(c["Image"], image_config(c["Image"])), "running": c["State"].get("Running", False)}


def check() -> dict:
    c = container()
    ref = c["Config"]["Image"]
    cur = describe(c["Image"], image_config(c["Image"]))  # before the pull takes its name away
    pull(ref)
    new = image(ref)
    return {"ok": True, "image": ref, "available": new["Id"] != c["Image"], "current": cur,
            "latest": describe(new["Id"], new.get("Config")), "latest_date": new.get("Created", "")[:10]}


def _create_body(c: dict, ref: str) -> dict:
    """The old container's settings for a new container from `ref`. Settings that only came from the
    old image (its environment, command, labels...) are left out, so the new image's own apply."""
    old = image_config(c["Image"])
    cfg = dict(c["Config"], Image=ref)
    if old is not None:
        cfg["Env"] = [e for e in cfg.get("Env") or [] if e not in (old.get("Env") or [])]
        cfg["Labels"] = {k: v for k, v in (cfg.get("Labels") or {}).items() if (old.get("Labels") or {}).get(k) != v}
        for k in ("Entrypoint", "Cmd", "WorkingDir", "User", "ExposedPorts", "Volumes", "Healthcheck", "StopSignal"):
            if k in cfg and cfg[k] == old.get(k):
                del cfg[k]
    else:  # the old image is gone: the Box's container sets no command of its own, so use the new image's
        cfg["Labels"] = {k: v for k, v in (cfg.get("Labels") or {}).items() if not k.startswith("org.opencontainers.")}
        for k in ("Entrypoint", "Cmd", "WorkingDir", "User", "ExposedPorts", "Volumes", "Healthcheck", "StopSignal"):
            cfg.pop(k, None)
    if cfg.get("Hostname") == c["Id"][:12]:  # Docker's default: the new container gets its own
        del cfg["Hostname"]
    host = dict(c["HostConfig"])
    # Keep volumes Docker made up by itself (no name in compose.yaml), or the settings would be lost
    named = {m.get("Target") for m in host.get("Mounts") or []}
    named |= {b.split(":")[1] for b in host.get("Binds") or [] if ":" in b}
    extra = [{"Type": "volume", "Source": m["Name"], "Target": m["Destination"]}
             for m in c.get("Mounts") or [] if m.get("Type") == "volume" and m.get("Destination") not in named]
    if extra:
        host["Mounts"] = (host.get("Mounts") or []) + extra
    body = dict(cfg, HostConfig=host)
    mode = host.get("NetworkMode") or ""
    if mode not in ("host", "none") and not mode.startswith("container:"):
        nets = (c.get("NetworkSettings") or {}).get("Networks") or {}
        body["NetworkingConfig"] = {"EndpointsConfig": {
            n: {k: v[k] for k in ("Aliases", "IPAMConfig", "Links", "DriverOpts") if v.get(k)} for n, v in nets.items()}}
    return body


def recreate() -> dict:
    """Replace the Box's container with one from the newest image already pulled."""
    c = container()
    ref = c["Config"]["Image"]
    new = image(ref)
    version = describe(new["Id"], new.get("Config"))
    if new["Id"] == c["Image"]:
        return {"ok": True, "updated": False, "version": version}
    body = _create_body(c, ref)
    name, old_id = c["Name"].lstrip("/"), c["Id"]
    log.info("Updating %s to %s (%s)", name, ref, version)
    docker("POST", f"/containers/{old_id}/stop?t=20", timeout=60)
    docker("POST", f"/containers/{old_id}/rename?name={quote(name + '-old-' + old_id[:6])}")
    new_id = None
    try:
        new_id = docker("POST", f"/containers/create?name={quote(name)}", body)["Id"]
        docker("POST", f"/containers/{new_id}/start")
        for _ in range(8):  # still running a few seconds later (a crash shows as Restarting)
            time.sleep(1)
            info = docker("GET", f"/containers/{new_id}/json")
            st = info["State"]
            if not st.get("Running") or st.get("Restarting") or info.get("RestartCount"):
                raise DockerError("the new version stopped straight away")
    except DockerError as exc:
        log.error("Update failed, putting the old container back: %s", exc)
        if new_id:
            try:
                docker("DELETE", f"/containers/{new_id}?force=1")
            except DockerError:
                pass
        docker("POST", f"/containers/{old_id}/rename?name={quote(name)}")
        docker("POST", f"/containers/{old_id}/start")
        raise
    docker("DELETE", f"/containers/{old_id}")
    try:
        docker("DELETE", f"/images/{c['Image']}")  # the old image, unless something else still uses it
    except DockerError:
        pass
    log.info("Updated %s", name)
    return {"ok": True, "updated": True, "version": version}


# ------------------------------------------------------------------ HTTP, for the control panel
class Handler(BaseHTTPRequestHandler):
    lock = threading.Lock()
    last_error = ""

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return bool(token) and hmac.compare_digest(got, token)

    def _run(self, fn):
        if not self._allowed():
            return self._json({"ok": False, "error": "Not allowed"}, 403)
        try:
            return self._json(fn())
        except DockerError as exc:
            return self._json({"ok": False, "error": str(exc)}, 502)

    def do_GET(self):
        if self.path == "/status":
            return self._run(lambda: dict(status(), last_error=Handler.last_error))
        self._json({"ok": False, "error": "Not found"}, 404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/check":
            def run():
                if not Handler.lock.acquire(timeout=1):
                    raise DockerError("An update is already running")
                try:
                    return check()
                finally:
                    Handler.lock.release()
            return self._run(run)
        if self.path == "/update":
            # Answer first: replacing the container stops the control panel that asked.
            def run():
                if not Handler.lock.acquire(timeout=1):
                    raise DockerError("An update is already running")
                try:
                    c = container()
                    image_config(c["Image"])
                    pull(c["Config"]["Image"])  # usually already there from /check
                    new = image(c["Config"]["Image"])
                except DockerError:
                    Handler.lock.release()
                    raise
                version = describe(new["Id"], new.get("Config"))
                if new["Id"] == c["Image"]:
                    Handler.lock.release()
                    return {"ok": True, "updated": False, "version": version}

                def work():
                    try:
                        Handler.last_error = ""
                        recreate()
                    except DockerError as exc:
                        Handler.last_error = str(exc)
                    finally:
                        Handler.lock.release()
                threading.Timer(1.0, work).start()
                return {"ok": True, "updated": True, "started": True, "version": version}
            return self._run(run)
        self._json({"ok": False, "error": "Not found"}, 404)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        log.info("Azan SIP updater for container '%s' (Docker %s), waiting on 127.0.0.1:%d",
                 TARGET, docker("GET", "/version").get("Version"), PORT)
        image_config(container()["Image"])
    except DockerError as exc:
        log.warning("%s", exc)
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
