"""Reads installed Oxide plugins from disk. Never writes."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

_STR = r'"((?:[^"\\]|\\.)*)"'
# The version is usually a string, but some plugins pass a bare number: [Info("A", "B", 1.3)]
INFO_RE = re.compile(
    r"\[\s*Info\s*\(\s*" + _STR + r"\s*,\s*" + _STR + r"\s*,\s*(?:" + _STR + r"|([0-9][0-9.]*))", re.S
)
DESC_RE = re.compile(r"\[\s*Description\s*\(\s*" + _STR, re.S)


def parse_source(text: str) -> dict:
    """Pull title, author, version and description out of a plugin's C# source."""
    info = INFO_RE.search(text)
    desc = DESC_RE.search(text)
    return {
        "title": info.group(1) if info else None,
        "author": info.group(2) if info else None,
        "version": (info.group(3) or info.group(4)) if info else None,
        "description": desc.group(1) if desc else None,
    }


def version_key(version: str | None) -> tuple[int, ...] | None:
    """'1.10.2' -> (1, 10, 2). Returns None when there are no numbers to compare."""
    if not version:
        return None
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts) if parts else None


def compare_versions(local: str | None, remote: str | None) -> int | None:
    """-1 if local is older, 0 if equal, 1 if local is newer, None if not comparable."""
    a, b = version_key(local), version_key(remote)
    if a is None or b is None:
        return None
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def scan_plugins(oxide_dir: Path) -> list[dict]:
    plugins_dir = oxide_dir / "plugins"
    config_dir = oxide_dir / "config"
    out = []
    if not plugins_dir.is_dir():
        return out
    for path in sorted(plugins_dir.glob("*.cs"), key=lambda p: p.name.lower()):
        raw = path.read_bytes()
        meta = parse_source(raw.decode("utf-8", errors="replace"))
        name = path.stem
        out.append(
            {
                "name": name,
                "file": path.name,
                "size": len(raw),
                "modified": path.stat().st_mtime,
                "sha1": hashlib.sha1(raw).hexdigest(),
                "has_config": (config_dir / f"{name}.json").is_file(),
                **meta,
            }
        )
    return out
