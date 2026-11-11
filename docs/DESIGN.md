# Design notes

Why Nitrous works the way it does, and the non-obvious things learned while building it.

## Principles

- **Files and logs, not a game integration.** Oxide already hot-loads changed plugin files and
  logs every load and compile error. Nitrous uses exactly that, so it needs no plugin of its
  own inside the server and keeps working across Rust and Oxide updates.
- **Never trust a change until the server confirms it.** Every write is followed by reading
  Oxide's log for the outcome.
- **Always keep the previous version.** Nothing is replaced without a backup, and a failed
  update undoes itself.
- **One change at a time.** Changes run through a single queue so a log line can be attributed
  to the plugin that was just changed.

## Reading installed plugins (`scanner.py`)

Version, title and author come from the `[Info("Title", "Author", "1.2.3")]` attribute. Two
details matter:

- The version is occasionally a **bare number**, `[Info("Perms UI", "Camoec", 1.3)]`, not a
  string. The parser accepts both.
- Versions are compared numerically part by part (`1.9.6` < `1.10.0`), padding with zeros
  (`2.0` equals `2.0.0`).

## Talking to uMod (`umod.py`)

- `https://umod.org/plugins/<FileName>.json` resolves by **file name**, not just by the URL
  slug (`AdvancedGather.cs` lives at the slug `advance-gather`, and both addresses work). That
  removes any need for fuzzy name matching. The response's `name` is checked against the file
  name to be sure it is the same plugin.
- `latest_release_version_checksum` is the **SHA-1 of the downloadable file**, which allows an
  exact "is my file identical?" test.
- That checksum is **sometimes stale**: for at least one plugin the listed checksum did not
  match the file uMod actually serves. So when a file has the same version number but a
  different hash, Nitrous downloads the real file once and compares against that before
  calling the plugin "modified".
- uMod allows **30 requests per minute**. Requests are spaced 2.4 seconds apart by booking
  time slots. The background check only ever has one request booked ahead, so an interactive
  search slots in right behind it instead of waiting for the whole check. A `429` pushes the
  next slot out by the server's `Retry-After`.
- Lookups are cached on disk for `CHECK_INTERVAL_HOURS`; searches in memory for 10 minutes.

### Status logic

| Condition | Status |
|---|---|
| uMod has no plugin with this file name | `not_on_umod` |
| File hash equals uMod's checksum, or the hash of the real download | `up_to_date` |
| Installed version is lower | `update_available` |
| Same version, different file | `modified` |
| Installed version is higher | `newer_local` |

## Knowing what the server did (`oxidelog.py`)

Oxide writes `oxide/logs/oxide_<date>.txt` with lines such as:

```
14:16 [Info] Loaded plugin Unburnable Meat v1.0.1 by S642667
14:16 [Info] Unloaded plugin Instant Craft v2.2.7 by Vlad-0003
14:16 [Error] Error while compiling Kits: There is no argument given … | Line: 1798, Pos: 33
```

Note the asymmetry: load and unload lines use the plugin's **title**, compile errors use the
**file name**. Nitrous maps titles back to files using the `[Info]` title of each installed
plugin.

- The **current state** of a plugin is its most recent event in the last two log files.
- To **verify a change**, Nitrous records the length of each log file before writing, then
  reads only what was appended afterwards, until it sees a load or a compile error for that
  plugin or `VERIFY_TIMEOUT` passes.

## Changing plugins (`updater.py`)

- Files are **written in place**, the same way a file copy would, because that is what
  Oxide's file watcher is known to react to. The previous owner and permissions are restored
  after writing so the game server can still manage the file.
- A download is only accepted if it parses as a plugin (has an `[Info]` version and mentions
  `Oxide.Plugins`) and is under 5 MB.
- **Update:** backup → write → wait. On a compile error the old bytes are written back and
  the result of *that* load is reported too. On silence the new file stays and the result is
  marked unverified.
- **Install:** uses uMod's spelling of the file name, because Oxide requires the file name to
  match the class name. A failed compile deletes the new file.
- **Reload:** `oxide.reload <Name>` over RCON; if RCON is unavailable, rewriting the plugin
  file with its own contents triggers the same reload.

## Editing configs (`configs.py` and the page)

- The form is generated from the **JSON file the plugin wrote**, not from the plugin source.
  Plugin authors define options in too many different ways for source parsing to be reliable,
  while the generated file always lists every option with its current value.
- **Friendly names are display-only.** A key is reworded only when it is a single code-style
  identifier (camelCase, PascalCase, snake_case). Keys containing spaces already read as text.
  Keys containing `/` or `.`, and single all-lowercase words, are treated as data (prefab
  paths, permissions, item names) and shown verbatim, because rewording them would mislead.
  The tree being edited always holds the original keys, so saving cannot rename anything.
- **Booleans are dropdowns; numbers are not booleans.** A survey of 30 real configs found about
  1,300 true/false values and about 1,400 numeric `0`/`1` values, and none of the numeric ones
  were switches: they were multipliers (`Loot Multiplier: 1`), coordinates, counts, version
  parts and IDs. So a number is never converted by value. It is shown as a switch only when its
  name is exactly *Enabled*, *Disabled* or *Active*, and it is still written back as `0` or `1`
  because that is the type the plugin reads.
- **Special values come from the author.** Sentinels such as `-1` or `0` mean different things
  in different plugins (unlimited, disabled, "all skins"). The only trustworthy source is the
  option's own name, which often documents it in parentheses, e.g. `(0 = disabled)`. Those are
  parsed into shortcut buttons; undocumented sentinels are left as plain numbers.
- **Large integers.** Steam IDs and skin IDs have 17 or more digits, beyond what a JavaScript
  number can hold exactly. Before parsing, the page wraps any integer of 16 or more digits as
  `{"$bigint": "…"}`, edits it as text, and unwraps it when writing, so the digits are
  preserved exactly. The server validates with Python, which has no such limit.
- **Optimistic locking.** The editor sends back the SHA-1 of the file it loaded. If the file
  on disk no longer matches (plugins rewrite their own configs), the save is refused.
- Many plugins **rewrite their config on load**, normalising formatting. A config that looks
  slightly different after a reload is expected.

## Security model

See the [Security](../README.md#security) section of the README. In short: single shared
password over HTTP Basic, a lockout after repeated wrong passwords, a required custom header on every change to defeat cross-site
requests, strict plugin-name validation (`[A-Za-z0-9_]+`) before any path is built, and no
secrets in logs or API responses. It is designed for a trusted LAN or VPN, not the open internet.

### Login and lockout (`auth.py`)

- The password lives in a file (`config.json`) rather than only in an environment variable so
  the server's owner can change it, or recover from a forgotten one, by editing a file. The
  file's modification time is checked on each request and it is re-read when it changes.
- Wrong passwords are counted **per client IP**. A single global counter would let anyone on
  the network lock the owner out; per-IP counting limits the damage to the guesser. Behind a
  reverse proxy every visitor shares the proxy's address, so they share one counter.
- A request with **no** password (a browser's first visit) is not counted; only a wrong one is.
- While locked, the **correct password is refused too**. Otherwise a lockout would not slow
  a guesser down at all.
- Lockouts are written to `lockouts.json` so a container restart does not clear them, and so
  that deleting the file is a simple manual override. Changing the password clears them too.

## Ideas not built yet

- Uninstall, with the plugin file and config moved to backups.
- Flagging config options that a plugin update added.
- Sources other than uMod.
- An Unraid Community Applications template.
