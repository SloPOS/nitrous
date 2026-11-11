"""Nitrous: Oxide plugin manager. Inventory, uMod update check, one-press update with
backup and automatic rollback."""
from __future__ import annotations

import asyncio
import base64
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import configs, oxidelog, rcon, updater
from .scanner import compare_versions, scan_plugins
from .umod import UmodClient, new_http_client

OXIDE_DIR = Path(os.environ.get("OXIDE_DIR", "/oxide"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CHECK_HOURS = float(os.environ.get("CHECK_INTERVAL_HOURS", "6"))
PASSWORD = os.environ.get("MANAGER_PASSWORD", "")
STATIC = Path(__file__).parent / "static"

umod = UmodClient(DATA_DIR / "umod_cache.json", ttl_seconds=CHECK_HOURS * 3600)
progress = {"running": False, "done": 0, "total": 0, "finished_at": None, "error": None}
job = {"running": False, "current": None, "step": "", "remaining": 0, "results": []}


def classify(local: dict, entry: dict | None) -> dict:
    """Combine what is on disk with what uMod says into one status."""
    row = {**local, "status": "unchecked", "latest": None, "umod": None, "checked_at": None}
    if not entry:
        return row
    row["checked_at"] = entry["fetched_at"]
    if entry.get("error"):
        row["status"], row["error"] = "error", entry["error"]
        return row
    if not entry["found"]:
        row["status"] = "not_on_umod"
        return row
    data = entry["data"]
    row["umod"] = data
    row["latest"] = data.get("latest_release_version")
    if local["sha1"] in (data.get("latest_release_version_checksum"), entry.get("download_sha1")):
        row["status"] = "up_to_date"
        return row
    cmp = compare_versions(local["version"], row["latest"])
    row["status"] = {-1: "update_available", 0: "modified", 1: "newer_local"}.get(cmp, "update_available")
    return row


def current_rows() -> list[dict]:
    plugins = scan_plugins(OXIDE_DIR)
    states = oxidelog.load_states(OXIDE_DIR, plugins)
    rows = []
    for p in plugins:
        row = classify(p, umod.cached(p["name"]))
        state = states.get(p["name"], {})
        row["server_state"] = state.get("state", "unknown")
        row["server_error"] = state.get("message")
        row["backups"] = len(updater.list_backups(DATA_DIR, p["name"]))
        rows.append(row)
    return rows


async def refresh(force: bool) -> None:
    if progress["running"]:
        return
    local = {p["name"]: p for p in scan_plugins(OXIDE_DIR)}
    todo = [n for n in local if force or not umod.is_fresh(n)]
    progress.update(running=True, done=0, total=len(todo), error=None)
    try:
        async with new_http_client() as client:
            for name in todo:
                entry = await umod.lookup(client, name)
                if classify(local[name], entry)["status"] == "modified":
                    await umod.verify_download(client, name)
                progress["done"] += 1
        umod.save()
    except Exception as exc:  # keep the service up; surface the problem in the UI
        progress["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        progress.update(running=False, finished_at=time.time())


async def periodic() -> None:
    while True:
        await refresh(force=False)
        await asyncio.sleep(CHECK_HOURS * 3600)


async def run_job(action: str, names: list[str], reason: str = "") -> None:
    """Updates run one at a time so each result in Oxide's log belongs to one plugin."""
    job.update(running=True, remaining=len(names))

    def step(text: str) -> None:
        job["step"] = text

    try:
        async with new_http_client() as client:
            for name in names:
                job.update(current=name, step="Starting")
                try:
                    if action == "update":
                        result = await updater.update_plugin(OXIDE_DIR, DATA_DIR, umod, client, name, step)
                    elif action == "install":
                        result = await updater.install_plugin(OXIDE_DIR, umod, client, name, step)
                    elif action == "reload":
                        result = await updater.reload_plugin(OXIDE_DIR, name, step, reason)
                    else:
                        result = await updater.rollback_plugin(OXIDE_DIR, DATA_DIR, name, step)
                except Exception as exc:
                    result = {"name": name, "action": action, "ok": False, "at": time.time(),
                              "message": f"Unexpected error: {type(exc).__name__}: {exc}"}
                job["results"] = ([result] + job["results"])[:60]
                job["remaining"] -= 1
        umod.save()
    finally:
        job.update(running=False, current=None, step="", remaining=0)


def require_changes() -> None:
    if not PASSWORD:
        raise HTTPException(403, "Changes are disabled until MANAGER_PASSWORD is set on the container.")
    if not updater.can_write(OXIDE_DIR):
        raise HTTPException(403, "The Oxide plugins folder is mounted read-only.")
    if job["running"]:
        raise HTTPException(409, "Another change is already running. Try again when it finishes.")


def start_job(action: str, names: list[str], reason: str = "") -> dict:
    require_changes()
    if not names:
        raise HTTPException(400, "Nothing to do.")
    asyncio.create_task(run_job(action, names, reason))
    return {"started": names}


def config_call(fn, *args):
    try:
        return fn(*args)
    except configs.ConfigConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except updater.UpdateError as exc:
        raise HTTPException(400, str(exc)) from None
    except OSError as exc:
        raise HTTPException(500, f"File error: {exc}") from None


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(periodic())
    yield
    task.cancel()


app = FastAPI(title="Nitrous", lifespan=lifespan)


@app.middleware("http")
async def guard(request: Request, call_next):
    if PASSWORD:
        supplied = ""
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                supplied = base64.b64decode(header[6:]).decode("utf-8", "replace").partition(":")[2]
            except ValueError:
                supplied = ""
        if not secrets.compare_digest(supplied.encode(), PASSWORD.encode()):
            return Response("Password required.", 401, {"WWW-Authenticate": 'Basic realm="Nitrous"'})
    # Changes must come from this page's own script, not a form on another site.
    if request.method not in ("GET", "HEAD") and request.headers.get("x-nitrous") != "1":
        return JSONResponse({"detail": "Missing X-Nitrous header."}, 403)
    return await call_next(request)


@app.get("/api/plugins")
def api_plugins():
    rows = current_rows()
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {
        "plugins": rows,
        "summary": summary,
        "broken": sum(1 for r in rows if r["server_state"] == "failed"),
        "progress": progress,
        "job": job,
        "oxide_dir_found": (OXIDE_DIR / "plugins").is_dir(),
        "can_change": bool(PASSWORD) and updater.can_write(OXIDE_DIR),
        "rcon": rcon.configured(),
    }


@app.post("/api/refresh")
async def api_refresh():
    if not progress["running"]:
        asyncio.create_task(refresh(force=True))
    return {"started": True}


@app.post("/api/plugins/{name}/update")
async def api_update(name: str):
    return start_job("update", [name])


@app.post("/api/plugins/{name}/rollback")
async def api_rollback(name: str):
    return start_job("rollback", [name])


@app.post("/api/update-all")
async def api_update_all():
    return start_job("update", [r["name"] for r in current_rows() if r["status"] == "update_available"])


@app.get("/api/plugins/{name}/backups")
def api_backups(name: str):
    return {"backups": updater.list_backups(DATA_DIR, name)}


@app.get("/api/umod/search")
async def api_search(q: str = "", page: int = 1, sort: str = "downloads"):
    if sort not in ("downloads", "latest_release_at", "title"):
        sort = "downloads"
    try:
        async with new_http_client() as client:
            found = await umod.search(client, q[:80], max(1, min(page, 500)), sort)
    except Exception as exc:
        raise HTTPException(502, f"uMod search failed: {type(exc).__name__}") from None
    installed = {p["name"].lower() for p in scan_plugins(OXIDE_DIR)}
    return {**found, "results": [{**r, "installed": str(r.get("name", "")).lower() in installed}
                                 for r in found["results"]]}


@app.post("/api/umod/install/{name}")
async def api_install(name: str):
    return start_job("install", [name])


@app.post("/api/plugins/{name}/reload")
async def api_reload(name: str):
    return start_job("reload", [name])


@app.get("/api/plugins/{name}/config")
def api_config(name: str):
    return config_call(configs.read_config, OXIDE_DIR, DATA_DIR, name)


@app.put("/api/plugins/{name}/config")
async def api_config_save(name: str, request: Request):
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        raise HTTPException(400, "Expected a JSON body with the config text.")
    require_changes()
    saved = config_call(configs.save_config, OXIDE_DIR, DATA_DIR, name, body["text"], body.get("sha1"))
    if saved["changed"] and body.get("reload", True):
        start_job("reload", [name], "Config saved.")
    return saved


@app.post("/api/plugins/{name}/config/restore")
async def api_config_restore(name: str, request: Request):
    body = await request.json()
    require_changes()
    restored = config_call(configs.restore_config, OXIDE_DIR, DATA_DIR, name, str(body.get("file", "")))
    if restored["changed"]:
        start_job("reload", [name], "Config restored from backup.")
    return restored


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
