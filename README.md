# Nitrous

A small web-based plugin manager for [Oxide/uMod](https://umod.org) on a Rust dedicated server.
It runs in a Docker container beside the game server, reads the server's `oxide` folder
directly, and uses [uMod](https://umod.org/plugins?categories=rust) as its source of truth.

- **See what you run.** Every installed plugin, its version, and whether the server actually
  loaded it or it fails to compile (with the compiler error).
- **Update with one press.** The current file is backed up, the new one installed, and the
  server's log watched until the plugin loads. If the new version fails to compile, the
  previous file is put back automatically.
- **Edit configs safely.** A form view generated from each plugin's JSON config, a raw JSON
  view, a backup on every save, and a plugin reload over RCON.
- **Find and install plugins.** Search uMod from the page and install with one press.

Nitrous is not affiliated with uMod, Oxide or Facepunch.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Configuration](#configuration)
- [Using Nitrous](#using-nitrous)
- [Backups](#backups)
- [Security](#security)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Project layout](#project-layout)

Further reading: [HTTP API](docs/API.md) · [Design notes](docs/DESIGN.md)

## How it works

```
                    +--------------------+
   browser  ----->  |      Nitrous       |  ---- HTTPS ---->  umod.org (versions, downloads)
  (LAN / VPN)       |  FastAPI + 1 page  |
                    +---------+----------+
                              |  reads/writes files            WebSocket RCON
                              v                                 (oxide.reload)
                    <rust>/oxide/plugins/*.cs   <----------+        |
                    <rust>/oxide/config/*.json             |        v
                    <rust>/oxide/logs/oxide_*.txt  ---->  Rust dedicated server + Oxide
```

Nitrous never talks to the game except through three things Oxide already provides:

1. **The plugins folder.** Oxide watches it and hot-loads any `.cs` file that changes, so
   installing or updating a plugin is just writing a file.
2. **Oxide's log.** After every change Nitrous reads `oxide/logs/oxide_<date>.txt` for
   `Loaded plugin …` or `Error while compiling …` to learn what really happened.
3. **RCON** (optional). Used only to send `oxide.reload <Plugin>` after a config change.
   Without RCON, Nitrous rewrites the plugin file instead, which triggers the same reload.

Update checks compare the installed file's SHA-1 and version against uMod. See
[Design notes](docs/DESIGN.md) for the details and the edge cases found along the way.

## Requirements

- A Rust dedicated server with Oxide installed, with Oxide's plugin watcher enabled
  (`"PluginWatchers": true` in `oxide.config.json`, which is the default).
- Docker on a machine that can see the server's `oxide` folder as a local path. The usual
  setup is the same host that runs the Rust server container (for example Unraid).
- Outbound HTTPS access to `umod.org`.
- Optional: the server's WebSocket RCON (`rcon.web 1`) reachable from the container.

## Install

### Option 1: deploy script (Rust server in Docker on the same host)

Copy this repository to the Docker host and run:

```bash
RUST_CONTAINER=my-rust-server RUST_OXIDE=/mnt/user/appdata/rust/oxide bash deploy.sh
```

The script builds the image, generates a page password on first run, reads the RCON address,
port and password from the Rust container's environment (`RCON_PORT`, `RCON_PASSWORD`), and
starts Nitrous. Run it again to upgrade, or after changing the RCON password.

| Variable | Default | Meaning |
|---|---|---|
| `RUST_CONTAINER` | `rust-server` | Name of the Rust server container |
| `RUST_OXIDE` | `/mnt/user/appdata/rust/oxide` | The server's `oxide` folder on the host |
| `BASE` | `/mnt/user/appdata/oxide-manager` | Where Nitrous keeps its password, cache and backups |
| `NAME` | `oxide-manager` | Name of the Nitrous container |
| `PORT` | `8790` | Port for the web page |
| `TZ` | `America/New_York` | Time zone |
| `RCON_HOST`, `RCON_PORT`, `RCON_PASSWORD` | read from the Rust container | Override if your image uses other variable names |

When it finishes, open `http://<docker-host>:8790`. The browser asks for a password: use any
username and the contents of `$BASE/password.txt`.

### Option 2: plain Docker

```bash
docker build -t nitrous .
```

```bash
docker run -d --name nitrous --restart unless-stopped -p 8790:8080 \
  -e MANAGER_PASSWORD='choose-a-long-password' \
  -e RCON_HOST=192.0.2.10 -e RCON_PORT=28016 -e RCON_PASSWORD='your-rcon-password' \
  -v /path/to/rust/oxide:/oxide \
  -v /path/to/nitrous-data:/data \
  nitrous
```

### Read-only mode

To only look, mount the Oxide folder read-only (`-v /path/to/rust/oxide:/oxide:ro`) or leave
`MANAGER_PASSWORD` unset. Either one disables every button that changes something.

## Configuration

All settings are environment variables on the container.

| Variable | Default | Meaning |
|---|---|---|
| `MANAGER_PASSWORD` | *(empty)* | Password for the page (HTTP Basic, any username). **Unset = no login and read-only.** |
| `OXIDE_DIR` | `/oxide` | Where the server's `oxide` folder is mounted |
| `DATA_DIR` | `/data` | Cache and backups |
| `CHECK_INTERVAL_HOURS` | `6` | How often to re-check uMod in the background |
| `VERIFY_TIMEOUT` | `75` | Seconds to wait for the server to log a load or compile result |
| `RCON_HOST` | *(empty)* | Rust server address for RCON. Unset = reload by rewriting the plugin file |
| `RCON_PORT` | `28016` | WebSocket RCON port |
| `RCON_PASSWORD` | *(empty)* | RCON password |

## Using Nitrous

### The plugin list

| Column | Meaning |
|---|---|
| **Installed / Latest** | Version in your `.cs` file and the latest on uMod |
| **uMod** | *Up to date*, *Update available*, *Differs from uMod* (same version number, different file), *Newer than uMod*, *Not on uMod* (probably from another site) |
| **On server** | *Loaded*, *Fails to compile* (with the error), *Unloaded*, or *Unknown* if Oxide's recent log says nothing |

A plugin that is **up to date and fails to compile** is broken against the current Rust build.
No update can fix it until its author publishes one. The **Fails to compile** filter lists these.

**Check for updates** re-queries uMod for every plugin. A full check takes roughly 2.5 seconds
per plugin because uMod allows 30 requests a minute.

### Updating

**Update** on a row, or **Update all**, does this for each plugin, one at a time:

1. Download the latest file from uMod and check it looks like an Oxide plugin.
2. Back up the installed file.
3. Write the new file and wait for the server to compile it.
4. If it **loaded**: done. If it **failed to compile**: restore the previous file and report
   the compiler error. If the server said **nothing** (it is stopped, for example): leave the
   new file in place and report it as unconfirmed.

**Roll back** restores the most recent backup that differs from the installed file.

The **Activity** panel shows each step and the outcome.

### Editing configs

**Config** opens the plugin's `oxide/config/<Plugin>.json`.

- **Form** shows every option as a field: checkboxes, numbers, text, collapsible groups, and
  lists with add/remove.
- **Raw JSON** is the file as text, for anything the form cannot do, such as adding new keys.
- **Save and reload plugin** validates the JSON, backs up the current file, writes the new
  one and reloads the plugin. The Activity panel reports whether it loaded.
- **Earlier versions** restores any of the last 20 saved versions.

If the file changed on the server after you opened it, the save is refused rather than
overwriting the newer version. Reopen the config and redo the edit.

### Adding plugins

**Add plugins** searches uMod's Rust catalogue (empty search = most downloaded). Results
older than two years are marked, since they are more likely to be broken on the current game.
**Install** downloads the file and waits for the server to load it; if it fails to compile,
the file is removed again.

## Backups

Everything Nitrous replaces is kept under the data folder:

| What | Where | Kept |
|---|---|---|
| Plugin files | `data/backups/<Plugin>/<timestamp>_v<version>.cs.bak` | Last 10 per plugin |
| Config files | `data/config-backups/<Plugin>/<timestamp>.json.bak` | Last 20 per plugin |

They are plain copies. To restore one by hand, copy it back over the original file.

## Security

Nitrous can replace code that runs inside your game server, so treat it like an admin tool.

- **Keep it off the internet.** Do not port-forward it. Reach it over your LAN or a VPN.
- **Set `MANAGER_PASSWORD`.** Without it there is no login, and all changes are disabled.
- **The login is HTTP Basic over plain HTTP.** That is acceptable on a trusted LAN or VPN.
  Put it behind an HTTPS reverse proxy if anyone else shares the network.
- **Changes need a custom header** (`X-Nitrous: 1`), so another website cannot trigger them
  through your logged-in browser.
- **Plugins are code.** Nitrous only downloads from uMod and checks that a download looks like
  an Oxide plugin; it does not review what the plugin does. Install plugins you trust.
- The RCON password is only used inside the container and is never logged or returned by the API.

## Limitations

- **uMod only.** Plugins from Codefling or other sites show as *Not on uMod*: they are listed
  and their configs can be edited, but they cannot be updated from here.
- **"Broken" is detected, not predicted.** uMod publishes no "works on this game version" flag,
  so Nitrous reports what the server's own compiler said.
- **Loading is not the same as working.** A plugin can compile and still misbehave at runtime;
  Nitrous only sees load and compile results.
- **Config forms come from the JSON file,** not the plugin source, so there are no descriptions
  beyond the option names, and options a plugin has not written to its file do not appear.
- **No uninstall button** yet. Delete the `.cs` file to remove a plugin.
- **Whole numbers lose their `.0`.** A value written as `1.0` is saved as `1` from the form
  view. Plugins read both the same way.
- **One Rust server per Nitrous container.**

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Banner: *Read-only: updates are disabled…* | `MANAGER_PASSWORD` is unset, or `/oxide` is mounted read-only |
| Banner: *The Oxide plugins folder was not found* | The `/oxide` mount does not point at the folder containing `plugins/` |
| Every plugin shows *Unloaded* | The server shut down or is restarting; Oxide logs an unload for each plugin on exit |
| Every plugin shows *Unknown* | Oxide has not written a log in the last two log files, or logs are elsewhere |
| *The server did not confirm…* after a change | The server is not running, or took longer than `VERIFY_TIMEOUT` to compile |
| *Could not reach the server's RCON* | Wrong `RCON_HOST`/`RCON_PORT`, server down, or `rcon.web` is not `1`. Nitrous falls back to a file refresh |
| Search or checks are slow | uMod's rate limit (30 requests/minute). Nitrous spaces requests about 2.4 seconds apart |
| *Check failed* on a plugin | uMod was unreachable or returned an error; hover the status for the reason |

## Development

Python 3.11 or newer.

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt pytest
```

```bash
.venv/bin/python -m pytest -q
```

(On Windows the paths are `.venv\Scripts\pip` and `.venv\Scripts\python`.)

The tests use a temporary folder and a fake "server" that writes Oxide-style log lines, so
they need neither a Rust server nor network access.

To run the app against a copy of a real `oxide` folder:

```bash
OXIDE_DIR=/path/to/oxide-copy DATA_DIR=/tmp/nitrous-data MANAGER_PASSWORD=dev \
  .venv/bin/python -m uvicorn app.main:app --port 8791
```

## Project layout

| Path | Purpose |
|---|---|
| `app/main.py` | FastAPI app: routes, login guard, background check, job runner |
| `app/scanner.py` | Reads installed plugins: version, author, hash |
| `app/umod.py` | uMod client: lookup, search, cache, rate limiting |
| `app/oxidelog.py` | Parses Oxide's log into loaded / failed / unloaded events |
| `app/updater.py` | Update, install, rollback, reload, and verification against the log |
| `app/configs.py` | Config read, validate, save, backup, restore |
| `app/rcon.py` | Minimal WebSocket RCON client |
| `app/static/index.html` | The whole front end: one page, no build step |
| `tests/` | Unit tests |
| `deploy.sh` | Build and run beside a Rust server container |
| `docs/` | API reference and design notes |
