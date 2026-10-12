"""Password protection for the control panel when it runs on an Azan SIP Box (box mode).

Only used in box mode: the desktop app keeps its panel on 127.0.0.1 with no password.

- Each box has a factory password (printed on the sticker). Its hash is in auth/factory.json,
  written when the box is prepared; a factory reset goes back to it.
- The owner can choose their own password; its hash is in auth/password.json.
- Logging in gives a session cookie that lasts 30 days. Only a hash of each session is kept, in
  auth/sessions.json, so phones stay signed in when the box restarts or updates.
- Until first-time setup is finished there is also a one-time setup code (shown as a QR code
  on the box's screen) that signs a phone in without the sticker password.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

ITERATIONS = 200_000
MIN_LENGTH = 8
SESSION_SECONDS = 30 * 24 * 3600
COOKIE = "azan_session"


class LoginError(Exception):
    """Wrong password, expired setup code or too many tries (message is shown to the user)."""


def hash_password(password: str, iterations: int = ITERATIONS) -> dict:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"algo": "pbkdf2-sha256", "iterations": iterations,
            "salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(dk).decode()}


def check_password(password: str, rec: dict | None) -> bool:
    if not rec or rec.get("algo") != "pbkdf2-sha256":
        return False
    try:
        salt = base64.b64decode(rec["salt"])
        want = base64.b64decode(rec["hash"])
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rec["iterations"]))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, want)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


class Auth:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.lock = threading.Lock()
        self.failures: dict[str, tuple[int, float]] = {}  # client ip -> (count, locked until)
        self.setup_code: str | None = None
        self.setup_sessions: set[str] = set()  # signed in with the setup code (ended with setup, never saved)
        now = time.time()
        saved = _read(self.sessions_file) or {}
        self.sessions: dict[str, float] = {}  # sha256(token) -> expiry (wall clock)
        for k, v in saved.items():
            try:
                if float(v) > now:
                    self.sessions[str(k)] = float(v)
            except (TypeError, ValueError):
                pass

    @property
    def factory_file(self) -> Path:
        return self.folder / "factory.json"

    @property
    def password_file(self) -> Path:
        return self.folder / "password.json"

    @property
    def sessions_file(self) -> Path:
        return self.folder / "sessions.json"

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _save_sessions(self):
        """Call with self.lock held. Sessions opened with the setup code are left out."""
        try:
            _write(self.sessions_file, {k: e for k, e in self.sessions.items() if k not in self.setup_sessions})
        except OSError:
            pass

    # -- passwords
    def has_password(self) -> bool:
        return bool(_read(self.password_file) or _read(self.factory_file))

    def custom_password(self) -> bool:
        return bool(_read(self.password_file))

    def verify(self, password: str) -> bool:
        rec = _read(self.password_file) or _read(self.factory_file)
        return check_password(password, rec)

    def set_password(self, password: str):
        if len(password) < MIN_LENGTH:
            raise ValueError(f"The password must be at least {MIN_LENGTH} characters")
        _write(self.password_file, hash_password(password))

    def set_factory_password(self, password: str, serial: str = ""):
        if len(password) < MIN_LENGTH:
            raise ValueError(f"The password must be at least {MIN_LENGTH} characters")
        _write(self.factory_file, dict(hash_password(password), serial=serial))

    def use_factory_password(self):
        """During first-time setup: keep the sticker password (forget one chosen a moment ago)."""
        self.password_file.unlink(missing_ok=True)

    def reset_to_factory(self):
        """Forget the owner's password (the sticker password works again) and log everyone out."""
        self.password_file.unlink(missing_ok=True)
        with self.lock:
            self.sessions.clear()
            self.setup_sessions.clear()
            self.failures.clear()
            self._save_sessions()

    # -- brute-force protection: after 5 wrong tries, wait 30 s, doubling up to 15 min
    def wait_seconds(self, ip: str) -> int:
        with self.lock:
            n, until = self.failures.get(ip, (0, 0.0))
        return max(0, int(until - time.monotonic() + 0.999))

    def _failed(self, ip: str):
        with self.lock:
            n, _ = self.failures.get(ip, (0, 0.0))
            n += 1
            until = time.monotonic() + (min(900, 30 * 2 ** (n - 5)) if n >= 5 else 0)
            self.failures[ip] = (n, until)

    def login(self, ip: str, password: str = "", setup_code: str = "") -> str:
        """Return a new session token, or raise LoginError with a message for the user."""
        wait = self.wait_seconds(ip)
        if wait:
            raise LoginError(f"Too many wrong tries. Please wait {wait} seconds and try again.")
        if setup_code:
            ok = bool(self.setup_code) and hmac.compare_digest(setup_code, self.setup_code)
        else:
            ok = self.verify(password)
        if not ok:
            self._failed(ip)
            raise LoginError("That setup code has expired. Use the password on the sticker."
                                  if setup_code else "Wrong password")
        with self.lock:
            self.failures.pop(ip, None)
            token = secrets.token_urlsafe(32)
            now = time.time()
            self.sessions = {k: e for k, e in self.sessions.items() if e > now}
            self.sessions[self._key(token)] = now + SESSION_SECONDS
            if setup_code:
                self.setup_sessions.add(self._key(token))
            self._save_sessions()
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        key = self._key(token)
        with self.lock:
            exp = self.sessions.get(key)
            if exp is None:
                return False
            if exp < time.time():
                self.sessions.pop(key, None)
                return False
            return True

    def logout(self, token: str | None):
        if not token:
            return
        with self.lock:
            self.sessions.pop(self._key(token), None)
            self._save_sessions()

    def logout_others(self, keep: str | None):
        key = self._key(keep) if keep else None
        with self.lock:
            self.sessions = {k: e for k, e in self.sessions.items() if k == key}
            self._save_sessions()

    # -- one-time setup code for the first-run QR code
    def new_setup_code(self) -> str:
        self.setup_code = secrets.token_urlsafe(12)
        return self.setup_code

    def end_setup(self, keep: str | None = None):
        """Setup is finished: the code stops working, and so do the sessions it opened, except
        `keep` (the phone or PC that finished setup carries on without typing the password)."""
        self.setup_code = None
        key = self._key(keep) if keep else None
        with self.lock:
            for k in self.setup_sessions - {key}:
                self.sessions.pop(k, None)
            self.setup_sessions.clear()
            self._save_sessions()
