# HTTP API

The page is a thin client over this API, so anything the page does can be scripted.

## Rules for every request

- **Login.** When a password is set, every request needs HTTP Basic credentials: any
  username, and that password. Without them the answer is `401`.
- **Lockout.** After `max_attempts` wrong passwords (5 by default) from one IP address, every
  request from it gets `429` with a plain-text explanation and a `Retry-After` header, even
  with the correct password, until the lockout ends or is cleared on the server.
- **`GET /healthz`** needs no login and returns `{"ok": true}`. The container's health check
  uses it.
- **Changes need a header.** Every request that is not `GET` or `HEAD` must send
  `X-Nitrous: 1`, otherwise the answer is `403`. This stops other websites from triggering
  changes through a logged-in browser.
- **Changes need write access.** They return `403` if no password is set or the Oxide
  folder is read-only, and `409` while another change is running.
- **Errors** are JSON: `{"detail": "what went wrong"}`.

Example:

```bash
curl -u any:PASSWORD -H "X-Nitrous: 1" -X POST http://HOST:8790/api/plugins/Kits/update
```

## Long-running changes

Updates, installs, rollbacks and reloads run in the background, one at a time. The request
returns immediately with `{"started": [...]}`. Poll `GET /api/plugins` and read `job`:

```json
{
  "running": false,
  "current": null,
  "step": "",
  "remaining": 0,
  "results": [
    {
      "name": "Kits", "action": "update", "ok": true, "verified": true,
      "rolled_back": false, "from": "4.4.4", "to": "4.4.10", "at": 1794340000.0,
      "message": "V4.4.10 loaded on the server."
    }
  ]
}
```

`results` holds the latest 60 outcomes, newest first. `action` is `update`, `install`,
`rollback` or `reload`. `verified` is true only when the server's log confirmed the load.

## Endpoints

### `GET /api/plugins`

Everything the page shows.

| Field | Meaning |
|---|---|
| `plugins[]` | One entry per installed `.cs` file (below) |
| `summary` | Count of plugins per `status` |
| `broken` | How many currently fail to compile |
| `progress` | State of the background uMod check: `running`, `done`, `total`, `finished_at`, `error` |
| `job` | State of the change runner (above) |
| `oxide_dir_found` | Whether `plugins/` exists under the mounted folder |
| `can_change` | Whether changes are allowed (password set and folder writable) |
| `rcon` | Whether RCON is configured |

Each plugin:

| Field | Meaning |
|---|---|
| `name`, `file` | File name without and with `.cs` |
| `title`, `author`, `version`, `description` | From the plugin's `[Info]` and `[Description]` attributes |
| `sha1`, `size`, `modified` | Of the installed file |
| `has_config` | Whether `config/<name>.json` exists |
| `status` | `up_to_date`, `update_available`, `modified`, `newer_local`, `not_on_umod`, `error`, `unchecked` |
| `latest` | Latest version on uMod |
| `umod` | uMod's record: `url`, `download_url`, `latest_release_at`, `downloads`, … |
| `server_state` | `loaded`, `failed`, `unloaded`, `unknown` |
| `server_error` | The compiler error when `server_state` is `failed` |
| `backups` | Number of plugin-file backups kept |

### `POST /api/refresh`

Re-check every plugin against uMod now. Progress appears in `progress`.

### `POST /api/plugins/{name}/update`

Update one plugin from uMod, with backup and automatic rollback on compile failure.

### `POST /api/update-all`

Update every plugin whose status is `update_available`, one at a time.

### `POST /api/plugins/{name}/rollback`

Restore the most recent backup that differs from the installed file.

### `GET /api/plugins/{name}/backups`

`{"backups": [{"file": "...", "taken": "20261110-150400", "version": "1.2.3"}]}`

### `POST /api/plugins/{name}/reload`

Reload one plugin on the server (RCON `oxide.reload`, or a file refresh without RCON).

### `GET /api/plugins/{name}/config`

```json
{
  "exists": true,
  "text": "{\n  \"Option\": true\n}",
  "sha1": "5ed9…",
  "error": null,
  "modified": 1794342748.9,
  "backups": [{"file": "20261110-161801.json.bak", "taken": "20261110-161801"}]
}
```

`error` is set when the file on disk is not valid JSON.

### `PUT /api/plugins/{name}/config`

Body:

```json
{"text": "{ ...the whole file... }", "sha1": "hash from the GET", "reload": true}
```

Validates the JSON, backs up the current file, writes the new one, and (unless `reload` is
false) reloads the plugin. Returns `{"changed": true, "sha1": "…"}`.

- `400` if the text is not valid JSON or not an object/list.
- `409` if `sha1` does not match the file now on disk (it changed since you read it).

### `POST /api/plugins/{name}/config/restore`

Body: `{"file": "20261110-161801.json.bak"}`. Backs up the current config, restores that
version, and reloads the plugin.

### `GET /api/umod/search?q=&page=1&sort=downloads`

Search uMod's Rust plugins. `sort` is `downloads`, `latest_release_at` or `title`.

```json
{
  "page": 1, "pages": 142, "total": 1419,
  "results": [{"name": "Kits", "title": "Rust Kits", "latest_release_version": "4.4.10",
               "latest_release_at": "2026-09-27 …", "downloads": 527071, "installed": true, "…": "…"}]
}
```

Results are cached for 10 minutes.

### `POST /api/umod/install/{name}`

Install a plugin from uMod by its file name (the `name` field of a search result). If it
fails to compile, the file is removed again.
