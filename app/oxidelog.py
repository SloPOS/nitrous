"""Reads Oxide's own log to learn whether each plugin loaded or failed to compile."""
from __future__ import annotations

import re
from pathlib import Path

LINE_RE = re.compile(r"^\d\d:\d\d \[(?:Info|Error|Warning)\] (.*)$")
LOADED_RE = re.compile(r"^Loaded plugin (.+) v(\S+) by ")
UNLOADED_RE = re.compile(r"^Unloaded plugin (.+) v(\S+) by ")
FAILED_RE = re.compile(r"^Error while compiling (\w+): (.*)$")
TAIL_BYTES = 2_000_000


def parse_events(text: str) -> list[dict]:
    """Loaded/unloaded events are keyed by plugin title, compile failures by file name."""
    events = []
    for line in text.splitlines():
        m = LINE_RE.match(line.rstrip("\r"))
        if not m:
            continue
        body = m.group(1)
        if hit := LOADED_RE.match(body):
            events.append({"kind": "loaded", "key": hit.group(1), "version": hit.group(2)})
        elif hit := UNLOADED_RE.match(body):
            events.append({"kind": "unloaded", "key": hit.group(1), "version": hit.group(2)})
        elif hit := FAILED_RE.match(body):
            events.append({"kind": "failed", "key": hit.group(1), "message": hit.group(2).strip()})
    return events


def log_files(oxide_dir: Path) -> list[Path]:
    return sorted((oxide_dir / "logs").glob("oxide_*.txt"), key=lambda p: p.name)


def _read(path: Path, start: int = 0) -> str:
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(start, size - TAIL_BYTES) if start == 0 else min(start, size))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def marker(oxide_dir: Path) -> dict[str, int]:
    """Remember how long each log is now, so later reads only see what came after."""
    out = {}
    for path in log_files(oxide_dir):
        try:
            out[path.name] = path.stat().st_size
        except OSError:
            pass
    return out


def events_since(oxide_dir: Path, mark: dict[str, int]) -> list[dict]:
    events = []
    for path in log_files(oxide_dir):
        start = mark.get(path.name)
        text = _read(path, start) if start else (_read(path) if start is None else _read(path, 0))
        events.extend(parse_events(text))
    return events


def load_states(oxide_dir: Path, plugins: list[dict]) -> dict[str, dict]:
    """Latest known state per plugin file name: loaded, failed or unloaded."""
    by_title = {}
    for p in plugins:
        by_title[p["name"].lower()] = p["name"]
        if p.get("title"):
            by_title[p["title"].lower()] = p["name"]
    states: dict[str, dict] = {}
    for path in log_files(oxide_dir)[-2:]:
        for ev in parse_events(_read(path)):
            name = by_title.get(ev["key"].lower())
            if name:
                states[name] = {"state": ev["kind"], "message": ev.get("message"), "version": ev.get("version")}
    return states
