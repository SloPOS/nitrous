"""Updates a plugin from uMod with a backup first, then checks Oxide actually loaded it.
If the new version fails to compile, the previous file is put back."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from pathlib import Path

import httpx

from . import oxidelog
from .scanner import parse_source
from .umod import UmodClient

KEEP_BACKUPS = 10
MAX_PLUGIN_BYTES = 5_000_000
VERIFY_TIMEOUT = float(os.environ.get("VERIFY_TIMEOUT", "75"))
NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


class UpdateError(Exception):
    pass


def plugin_path(oxide_dir: Path, name: str) -> Path:
    if not NAME_RE.match(name):
        raise UpdateError("Invalid plugin name.")
    return oxide_dir / "plugins" / f"{name}.cs"


def can_write(oxide_dir: Path) -> bool:
    return os.access(oxide_dir / "plugins", os.W_OK)


# ---- backups -------------------------------------------------------------

def backup(data_dir: Path, name: str, content: bytes, version: str | None) -> Path:
    folder = data_dir / "backups" / name
    folder.mkdir(parents=True, exist_ok=True)
    safe_version = re.sub(r"[^0-9A-Za-z.]", "", version or "unknown") or "unknown"
    target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}_v{safe_version}.cs.bak"
    n = 1
    while target.exists():  # two backups within the same second
        n += 1
        target = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}_v{safe_version}.cs.bak"
    target.write_bytes(content)
    for old in sorted(folder.glob("*.cs.bak"), key=lambda p: p.name)[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def list_backups(data_dir: Path, name: str) -> list[dict]:
    folder = data_dir / "backups" / name
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.cs.bak"), key=lambda p: p.name, reverse=True):
        stamp, _, version = path.name.removesuffix(".cs.bak").partition("_v")
        out.append({"file": path.name, "taken": stamp, "version": version})
    return out


# ---- writing and verifying -----------------------------------------------

def write_plugin(path: Path, content: bytes) -> None:
    """Write in place (as a file copy would) so Oxide's watcher sees a normal change,
    keeping the owner and permissions the Rust server expects."""
    previous = path.stat() if path.exists() else None
    path.write_bytes(content)
    if previous is not None:
        try:
            os.chown(path, previous.st_uid, previous.st_gid)
            os.chmod(path, previous.st_mode & 0o777)
        except (AttributeError, OSError):
            pass  # not supported on this platform, or not permitted


async def wait_for_result(oxide_dir: Path, mark: dict, name: str, titles: set[str],
                          timeout: float | None = None) -> tuple[str, str | None]:
    """Watch Oxide's log for this plugin. Returns ('loaded', version), ('failed', error)
    or ('timeout', None) if Oxide said nothing."""
    wanted = {t.lower() for t in titles if t} | {name.lower()}
    deadline = time.monotonic() + (VERIFY_TIMEOUT if timeout is None else timeout)
    while True:
        for ev in oxidelog.events_since(oxide_dir, mark):
            if ev["kind"] == "failed" and ev["key"].lower() == name.lower():
                return "failed", ev["message"]
            if ev["kind"] == "loaded" and ev["key"].lower() in wanted:
                return "loaded", ev["version"]
        if time.monotonic() >= deadline:
            return "timeout", None
        await asyncio.sleep(1)


def _describe(outcome: tuple[str, str | None], version: str | None) -> str:
    kind, detail = outcome
    if kind == "loaded":
        return f"v{detail or version} loaded on the server"
    if kind == "failed":
        return f"v{version} failed to compile: {detail}"
    return f"v{version} is in place, but the server did not confirm loading it (is it running?)"


async def _download(umod: UmodClient, client: httpx.AsyncClient, name: str) -> tuple[dict, bytes, dict]:
    entry = await umod.lookup(client, name)
    if entry.get("error"):
        raise UpdateError(f"Could not reach uMod: {entry['error']}")
    if not entry["found"]:
        raise UpdateError("This plugin is not on uMod.")
    url = entry["data"].get("download_url")
    if not url:
        raise UpdateError("uMod lists no download for this plugin.")
    resp = await umod._get(client, url)
    if resp.status_code != 200:
        raise UpdateError(f"Download failed (HTTP {resp.status_code}).")
    content = resp.content
    meta = parse_source(content.decode("utf-8", errors="replace"))
    if not content or len(content) > MAX_PLUGIN_BYTES or not meta["version"] or b"Oxide.Plugins" not in content:
        raise UpdateError("The download does not look like an Oxide plugin; nothing was changed.")
    entry["download_sha1"] = hashlib.sha1(content).hexdigest()
    return entry, content, meta


async def update_plugin(oxide_dir: Path, data_dir: Path, umod: UmodClient, client: httpx.AsyncClient,
                        name: str, step=lambda s: None) -> dict:
    result = {"name": name, "action": "update", "ok": False, "rolled_back": False, "at": time.time()}
    try:
        path = plugin_path(oxide_dir, name)
        if not path.is_file():
            raise UpdateError("Plugin file not found.")
        old = path.read_bytes()
        old_meta = parse_source(old.decode("utf-8", errors="replace"))
        result["from"] = old_meta["version"]

        step("Downloading from uMod")
        entry, new, new_meta = await _download(umod, client, name)
        result["to"] = new_meta["version"]
        if hashlib.sha1(old).hexdigest() == entry["download_sha1"]:
            result.update(ok=True, message="Already identical to uMod's latest file.")
            return result

        step("Backing up current version")
        backup(data_dir, name, old, old_meta["version"])

        step("Installing and waiting for the server to compile it")
        titles = {new_meta["title"], old_meta["title"]}
        mark = oxidelog.marker(oxide_dir)
        write_plugin(path, new)
        outcome = await wait_for_result(oxide_dir, mark, name, titles)

        if outcome[0] != "failed":
            result.update(ok=True, verified=outcome[0] == "loaded", message=_describe(outcome, new_meta["version"]).capitalize() + ".")
            return result

        step("New version failed to compile; restoring previous version")
        mark = oxidelog.marker(oxide_dir)
        write_plugin(path, old)
        restored = await wait_for_result(oxide_dir, mark, name, titles)
        result.update(
            rolled_back=True,
            message=f"{_describe(outcome, new_meta['version']).capitalize()}. Restored the previous file: "
                    f"{_describe(restored, old_meta['version'])}.",
        )
    except UpdateError as exc:
        result["message"] = str(exc)
    except OSError as exc:
        result["message"] = f"File error: {exc}"
    return result


async def install_plugin(oxide_dir: Path, umod: UmodClient, client: httpx.AsyncClient,
                         name: str, step=lambda s: None) -> dict:
    """Add a plugin from uMod. If it fails to compile, the new file is removed again."""
    result = {"name": name, "action": "install", "ok": False, "rolled_back": False, "at": time.time()}
    try:
        plugin_path(oxide_dir, name)  # validates the name
        step("Downloading from uMod")
        entry, content, meta = await _download(umod, client, name)
        # Oxide needs the file name to match the plugin's class name exactly, so use uMod's spelling.
        canonical = entry["data"].get("name") or name
        path = plugin_path(oxide_dir, canonical)
        result.update(name=canonical, to=meta["version"])
        existing = {p.name.lower() for p in path.parent.glob("*.cs")}
        if path.name.lower() in existing:
            raise UpdateError("This plugin is already installed.")
        if canonical != name:
            umod.cache[canonical] = umod.cache.pop(name)

        step("Installing and waiting for the server to compile it")
        mark = oxidelog.marker(oxide_dir)
        path.write_bytes(content)
        try:  # match the owner of the plugins folder so the Rust server can manage the file
            owner = path.parent.stat()
            os.chown(path, owner.st_uid, owner.st_gid)
            os.chmod(path, 0o664)
        except (AttributeError, OSError):
            pass
        outcome = await wait_for_result(oxide_dir, mark, canonical, {meta["title"]})
        if outcome[0] == "failed":
            path.unlink(missing_ok=True)
            result.update(rolled_back=True, message=f"{_describe(outcome, meta['version']).capitalize()}. "
                                                     "The file was removed again.")
        else:
            result.update(ok=True, verified=outcome[0] == "loaded",
                          message="Installed: " + _describe(outcome, meta["version"]) + ".")
    except UpdateError as exc:
        result["message"] = str(exc)
    except OSError as exc:
        result["message"] = f"File error: {exc}"
    return result


async def reload_plugin(oxide_dir: Path, name: str, step=lambda s: None, reason: str = "") -> dict:
    """Ask the server to reload one plugin (so it picks up a changed config), then confirm
    from Oxide's log. Uses RCON; without it, rewrites the plugin file to trigger a reload."""
    from . import rcon  # imported here so the updater works without RCON installed

    result = {"name": name, "action": "reload", "ok": False, "rolled_back": False, "at": time.time()}
    try:
        path = plugin_path(oxide_dir, name)
        if not path.is_file():
            raise UpdateError("Plugin file not found.")
        content = path.read_bytes()
        meta = parse_source(content.decode("utf-8", errors="replace"))
        step("Reloading the plugin on the server")
        mark = oxidelog.marker(oxide_dir)
        how = "RCON"
        try:
            await rcon.command(f"oxide.reload {name}")
        except rcon.RconError as exc:
            if not can_write(oxide_dir):
                raise UpdateError(f"{exc} The plugin was not reloaded.") from None
            how = "file refresh"
            write_plugin(path, content)
        outcome = await wait_for_result(oxide_dir, mark, name, {meta["title"]})
        if outcome[0] == "loaded":
            message = f"reloaded via {how}: v{outcome[1]} loaded on the server"
        elif outcome[0] == "failed":
            message = f"the plugin failed to compile on reload: {outcome[1]}"
        else:
            message = "the server did not confirm the reload (is it running?)"
        result.update(ok=outcome[0] == "loaded", verified=outcome[0] == "loaded",
                      message=(reason + " " if reason else "") + message[0].upper() + message[1:] + ".")
    except UpdateError as exc:
        result["message"] = (reason + " " if reason else "") + str(exc)
    except OSError as exc:
        result["message"] = f"File error: {exc}"
    return result


async def rollback_plugin(oxide_dir: Path, data_dir: Path, name: str, step=lambda s: None) -> dict:
    """Put back the most recent backup that differs from what is installed now."""
    result = {"name": name, "action": "rollback", "ok": False, "rolled_back": False, "at": time.time()}
    try:
        path = plugin_path(oxide_dir, name)
        current = path.read_bytes() if path.is_file() else b""
        cur_meta = parse_source(current.decode("utf-8", errors="replace"))
        result["from"] = cur_meta["version"]
        folder = data_dir / "backups" / name
        chosen = None
        for item in list_backups(data_dir, name):
            content = (folder / item["file"]).read_bytes()
            if content != current:
                chosen = content
                break
        if chosen is None:
            raise UpdateError("No earlier version is backed up for this plugin.")
        meta = parse_source(chosen.decode("utf-8", errors="replace"))
        result["to"] = meta["version"]

        step("Backing up current version")
        if current:
            backup(data_dir, name, current, cur_meta["version"])
        step("Restoring and waiting for the server to compile it")
        mark = oxidelog.marker(oxide_dir)
        write_plugin(path, chosen)
        outcome = await wait_for_result(oxide_dir, mark, name, {meta["title"], cur_meta["title"]})
        result.update(ok=outcome[0] != "failed", verified=outcome[0] == "loaded",
                      message="Rolled back: " + _describe(outcome, meta["version"]) + ".")
    except UpdateError as exc:
        result["message"] = str(exc)
    except OSError as exc:
        result["message"] = f"File error: {exc}"
    return result
