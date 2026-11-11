"""Reads and saves a plugin's JSON config in oxide/config, keeping a backup of every
version that gets replaced."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .updater import NAME_RE, UpdateError, write_plugin

KEEP_BACKUPS = 20
MAX_CONFIG_BYTES = 5_000_000


class ConfigConflict(UpdateError):
    pass


def config_path(oxide_dir: Path, name: str) -> Path:
    if not NAME_RE.match(name):
        raise UpdateError("Invalid plugin name.")
    return oxide_dir / "config" / f"{name}.json"


def _backup_dir(data_dir: Path, name: str) -> Path:
    return data_dir / "config-backups" / name


def validate(text: str) -> None:
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise UpdateError("Config is too large.")
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise UpdateError(f"Not valid JSON: {exc}") from None
    if not isinstance(parsed, (dict, list)):
        raise UpdateError("A config must be a JSON object or list.")


def read_config(oxide_dir: Path, data_dir: Path, name: str) -> dict:
    path = config_path(oxide_dir, name)
    if not path.is_file():
        return {"exists": False, "text": "", "sha1": None, "error": None, "backups": list_backups(data_dir, name)}
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    error = None
    try:
        validate(text)
    except UpdateError as exc:
        error = str(exc)
    return {"exists": True, "text": text, "sha1": hashlib.sha1(raw).hexdigest(), "error": error,
            "modified": path.stat().st_mtime, "backups": list_backups(data_dir, name)}


def list_backups(data_dir: Path, name: str) -> list[dict]:
    folder = _backup_dir(data_dir, name)
    if not folder.is_dir():
        return []
    return [{"file": p.name, "taken": p.name.removesuffix(".json.bak")}
            for p in sorted(folder.glob("*.json.bak"), key=lambda p: p.name, reverse=True)]


def _backup(data_dir: Path, name: str, content: bytes) -> None:
    folder = _backup_dir(data_dir, name)
    folder.mkdir(parents=True, exist_ok=True)
    stamp, n = time.strftime("%Y%m%d-%H%M%S"), 1
    target = folder / f"{stamp}.json.bak"
    while target.exists():
        n += 1
        target = folder / f"{stamp}-{n}.json.bak"
    target.write_bytes(content)
    for old in sorted(folder.glob("*.json.bak"), key=lambda p: p.name)[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)


def save_config(oxide_dir: Path, data_dir: Path, name: str, text: str, expected_sha1: str | None) -> dict:
    """Validate, back up what is there now, then write. Refuses if the file changed on
    disk since the editor loaded it (the server may have rewritten it)."""
    path = config_path(oxide_dir, name)
    validate(text)
    current = path.read_bytes() if path.is_file() else None
    current_sha = hashlib.sha1(current).hexdigest() if current is not None else None
    if expected_sha1 != current_sha:
        raise ConfigConflict("The config changed on the server since you opened it. Reopen it and redo your edit.")
    new = text.encode("utf-8")
    if current is not None:
        if new == current:
            return {"changed": False, "sha1": current_sha}
        _backup(data_dir, name, current)
    write_plugin(path, new)
    return {"changed": True, "sha1": hashlib.sha1(new).hexdigest()}


def restore_config(oxide_dir: Path, data_dir: Path, name: str, backup_file: str) -> dict:
    folder = _backup_dir(data_dir, name)
    if backup_file not in {b["file"] for b in list_backups(data_dir, name)}:
        raise UpdateError("That backup no longer exists.")
    content = (folder / backup_file).read_bytes()
    path = config_path(oxide_dir, name)
    current = path.read_bytes() if path.is_file() else None
    if current == content:
        return {"changed": False}
    if current is not None:
        _backup(data_dir, name, current)
    write_plugin(path, content)
    return {"changed": True}
