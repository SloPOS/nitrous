"""Page login: one shared password, with a lockout after repeated wrong attempts.

The password and lockout settings live in <data>/config.json so the server's owner can
change them by editing a file. The file is re-read whenever it changes; no restart needed.

    {"password": "...", "max_attempts": 5, "lockout_minutes": 15}

Editing the password, or deleting <data>/lockouts.json, lifts every lockout at once.
"""
from __future__ import annotations

import json
import math
import secrets
import time
from pathlib import Path

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LOCKOUT_MINUTES = 15
FORGET_AFTER = 24 * 3600  # drop failure counts nobody has added to for a day


class Auth:
    def __init__(self, data_dir: Path, env_password: str = "", now=time.time):
        self.config_file = data_dir / "config.json"
        self.lock_file = data_dir / "lockouts.json"
        self.env_password = env_password
        self.now = now
        self._stamp: tuple | None = None
        self._settings: dict = {}
        self._clients: dict[str, dict] = {}
        try:
            saved = json.loads(self.lock_file.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self._clients = {k: v for k, v in saved.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass
        self._load()

    # ---- settings ---------------------------------------------------------

    def _load(self) -> None:
        """Re-read config.json if it changed. A changed password clears all lockouts."""
        try:
            st = self.config_file.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp == self._stamp and self._settings:
            return
        data: dict = {}
        if stamp is not None:
            try:
                loaded = json.loads(self.config_file.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}  # unreadable file: fall back to the environment password

        def number(key: str, default: float, low: float) -> float:
            value = data.get(key, default)
            return max(low, float(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

        file_password = data.get("password")
        new = {
            "password": file_password if isinstance(file_password, str) and file_password else self.env_password,
            "max_attempts": int(number("max_attempts", DEFAULT_MAX_ATTEMPTS, 1)),
            "lockout_minutes": number("lockout_minutes", DEFAULT_LOCKOUT_MINUTES, 0),
        }
        if self._settings and new["password"] != self._settings["password"]:
            self._clients = {}
            self._save()
        self._stamp, self._settings = stamp, new

    @property
    def password_set(self) -> bool:
        self._load()
        return bool(self._settings["password"])

    # ---- lockouts ---------------------------------------------------------

    def _save(self) -> None:
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            if self._clients:
                self.lock_file.write_text(json.dumps(self._clients), encoding="utf-8")
            else:
                self.lock_file.unlink(missing_ok=True)
        except OSError:
            pass  # lockouts still apply in memory

    def _sync(self) -> None:
        # Deleting lockouts.json on the server is the manual "unlock everyone" switch.
        if self._clients and not self.lock_file.exists():
            self._clients = {}
        cutoff = self.now() - FORGET_AFTER
        stale = [c for c, e in self._clients.items() if e.get("last", 0) < cutoff and not self._locked_for(e)]
        for c in stale:
            del self._clients[c]

    def _locked_for(self, entry: dict) -> float:
        """Seconds of lockout left (inf if it never expires, 0 if not locked)."""
        until = entry.get("until")
        if until is None:
            return 0.0
        if until == "manual":
            return math.inf
        return max(0.0, float(until) - self.now())

    def check(self, client: str, supplied: str | None) -> tuple[str, float]:
        """Returns (result, seconds_locked):
        'open' no password is configured; 'ok' correct; 'missing' no password sent;
        'bad' wrong password; 'locked' too many wrong attempts (even a correct one is refused)."""
        self._load()
        password = self._settings["password"]
        if not password:
            return "open", 0.0
        self._sync()
        entry = self._clients.get(client)
        if entry:
            left = self._locked_for(entry)
            if left > 0:
                return "locked", left
            if entry.get("until") is not None:  # lockout has run out: start fresh
                del self._clients[client]
                entry = None
                self._save()
        if supplied is None:
            return "missing", 0.0
        if secrets.compare_digest(supplied.encode(), password.encode()):
            if entry:
                del self._clients[client]
                self._save()
            return "ok", 0.0
        entry = self._clients.setdefault(client, {"count": 0})
        entry["count"] = entry.get("count", 0) + 1
        entry["last"] = self.now()
        result = "bad"
        if entry["count"] >= self._settings["max_attempts"]:
            minutes = self._settings["lockout_minutes"]
            entry["until"] = "manual" if minutes == 0 else self.now() + minutes * 60
            result = "locked"
        self._save()
        return result, self._locked_for(entry)
