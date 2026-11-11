"""uMod lookups with a disk cache and a rate limiter (uMod allows 30 requests/minute)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

BASE = "https://umod.org/plugins"
USER_AGENT = "Nitrous/0.1 (Oxide plugin manager)"  # identifies the tool honestly to uMod
MIN_INTERVAL = 2.4  # seconds between requests, ~25/minute
KEEP = (
    "name", "title", "slug", "author", "description", "url", "download_url",
    "latest_release_version", "latest_release_version_checksum", "latest_release_at",
    "status_detail", "tags_all", "downloads",
)


class UmodClient:
    def __init__(self, cache_file: Path, ttl_seconds: float):
        self.cache_file = cache_file
        self.ttl = ttl_seconds
        self.cache: dict[str, dict] = {}
        self._next_slot = 0.0
        self._searches: dict[tuple, tuple[float, dict]] = {}
        if cache_file.is_file():
            try:
                self.cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.cache = {}

    def cached(self, name: str) -> dict | None:
        return self.cache.get(name)

    def is_fresh(self, name: str) -> bool:
        entry = self.cache.get(name)
        return bool(entry) and not entry.get("error") and time.time() - entry["fetched_at"] < self.ttl

    def save(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache), encoding="utf-8")
        tmp.replace(self.cache_file)

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        # Each request books the next free time slot. The background check only ever has
        # one request booked ahead, so a search slots in right after it instead of
        # queueing behind the whole check.
        for _ in range(3):
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + MIN_INTERVAL
            if slot > now:
                await asyncio.sleep(slot - now)
            resp = await client.get(url)
            if resp.status_code != 429:
                # uMod reports the remaining allowance in its headers. If it is nearly used
                # up, back off before the next request rather than running into the limit.
                remaining = resp.headers.get("X-RateLimit-Remaining", "")
                if remaining.isdigit() and int(remaining) <= 2:
                    self._next_slot = max(self._next_slot, time.monotonic() + 30)
                return resp
            retry = resp.headers.get("Retry-After") or resp.headers.get("X-Retry-After") or "30"
            pause = min(float(retry) if retry.isdigit() else 30.0, 90.0) + 1
            self._next_slot = max(self._next_slot, time.monotonic() + pause)
        return resp

    async def lookup(self, client: httpx.AsyncClient, name: str) -> dict:
        """Fetch one plugin by its file name (uMod resolves /plugins/<Name>.json)."""
        entry: dict = {"fetched_at": time.time(), "found": False}
        try:
            resp = await self._get(client, f"{BASE}/{name}.json")
            if resp.status_code == 200:
                data = resp.json()
                # uMod answers by slug too; make sure this is really the same plugin.
                if str(data.get("name", "")).lower() == name.lower():
                    entry["found"] = True
                    entry["data"] = {k: data.get(k) for k in KEEP}
            elif resp.status_code != 404:
                entry["error"] = f"uMod returned HTTP {resp.status_code}"
        except (httpx.HTTPError, ValueError) as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        self.cache[name] = entry
        return entry

    async def search(self, client: httpx.AsyncClient, query: str, page: int, sort: str) -> dict:
        """Search uMod's Rust plugins. Results are cached briefly to stay under the rate limit."""
        key = (query.strip().lower(), page, sort)
        hit = self._searches.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        params = {"query": query.strip(), "page": page, "sort": sort,
                  "sortdir": "asc" if sort == "title" else "desc", "categories[]": "rust"}
        resp = await self._get(client, f"{BASE}/search.json?{urlencode(params)}")
        if resp.status_code != 200:
            raise httpx.HTTPError(f"uMod returned HTTP {resp.status_code}")
        body = resp.json()
        result = {
            "page": body.get("current_page", page),
            "pages": body.get("last_page", 1),
            "total": body.get("total", 0),
            "results": [{k: item.get(k) for k in KEEP} for item in body.get("data", [])],
        }
        if len(self._searches) > 200:
            self._searches.clear()
        self._searches[key] = (time.time(), result)
        return result

    async def verify_download(self, client: httpx.AsyncClient, name: str) -> None:
        """Hash the real download. uMod's listed checksum is sometimes stale, so when a
        same-version file doesn't match it, this is the authoritative comparison."""
        entry = self.cache.get(name)
        url = entry and entry.get("found") and entry["data"].get("download_url")
        if not url:
            return
        try:
            resp = await self._get(client, url)
            if resp.status_code == 200:
                entry["download_sha1"] = hashlib.sha1(resp.content).hexdigest()
        except httpx.HTTPError:
            pass


def new_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
