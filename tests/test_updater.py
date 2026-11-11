import asyncio
import hashlib

from app import oxidelog, updater

OLD = b'namespace Oxide.Plugins { [Info("Foo Bar", "me", "1.0.0")] class Foo {} }'
NEW = b'namespace Oxide.Plugins { [Info("Foo Bar", "me", "1.1.0")] class Foo {} }'

REAL_LOG = """14:16 [Info] Unloaded plugin Instant Craft v2.2.7 by Vlad-0003 / Orange / rostov114
14:16 [Error] Error while compiling Kits: There is no argument given | Line: 1798, Pos: 33
14:16 [Error] [Instant Craft] Invalid item short name in config:
14:16 [Info] Loaded plugin Unburnable Meat v1.0.1 by S642667
"""


def make_server(tmp_path):
    oxide, data = tmp_path / "oxide", tmp_path / "data"
    (oxide / "plugins").mkdir(parents=True)
    (oxide / "logs").mkdir()
    (oxide / "plugins" / "Foo.cs").write_bytes(OLD)
    (oxide / "logs" / "oxide_2026-11-10.txt").write_text("13:00 [Info] Loaded plugin Foo Bar v1.0.0 by me\n")
    return oxide, data


async def fake_oxide(oxide, broken_versions=()):
    """Stand-in for the Rust server: logs a load or compile error whenever Foo.cs changes."""
    path, log = oxide / "plugins" / "Foo.cs", oxide / "logs" / "oxide_2026-11-10.txt"
    seen = path.read_bytes()
    while True:
        await asyncio.sleep(0.05)
        now = path.read_bytes()
        if now == seen:
            continue
        seen = now
        version = b"1.1.0" if b"1.1.0" in now else b"1.0.0"
        line = (f"14:00 [Error] Error while compiling Foo: boom | Line: 1, Pos: 1\n" if version in broken_versions
                else f"14:00 [Info] Loaded plugin Foo Bar v{version.decode()} by me\n")
        with log.open("a") as fh:
            fh.write(line)


def run_update(oxide, data, monkeypatch, broken=()):
    async def fake_download(umod, client, name):
        return {"download_sha1": hashlib.sha1(NEW).hexdigest()}, NEW, {"title": "Foo Bar", "version": "1.1.0"}

    monkeypatch.setattr(updater, "_download", fake_download)

    async def go():
        server = asyncio.create_task(fake_oxide(oxide, broken))
        await asyncio.sleep(0.1)  # let the fake server note the starting file
        try:
            return await updater.update_plugin(oxide, data, None, None, "Foo")
        finally:
            server.cancel()

    return asyncio.run(go())


def test_parse_real_log_lines():
    events = oxidelog.parse_events(REAL_LOG)
    assert [(e["kind"], e["key"]) for e in events] == [
        ("unloaded", "Instant Craft"), ("failed", "Kits"), ("loaded", "Unburnable Meat")]
    assert events[1]["message"].startswith("There is no argument given")


def test_load_states_maps_titles_to_files(tmp_path):
    oxide, _ = make_server(tmp_path)
    (oxide / "logs" / "oxide_2026-11-10.txt").write_text(REAL_LOG)
    plugins = [{"name": "Kits", "title": "Kits"}, {"name": "UnburnableMeat", "title": "Unburnable Meat"},
               {"name": "Other", "title": "Other"}]
    states = oxidelog.load_states(oxide, plugins)
    assert states["Kits"]["state"] == "failed"
    assert states["UnburnableMeat"]["state"] == "loaded"
    assert "Other" not in states


def test_update_success_keeps_backup(tmp_path, monkeypatch):
    oxide, data = make_server(tmp_path)
    result = run_update(oxide, data, monkeypatch)
    assert result["ok"] and result["verified"] and not result["rolled_back"]
    assert (result["from"], result["to"]) == ("1.0.0", "1.1.0")
    assert (oxide / "plugins" / "Foo.cs").read_bytes() == NEW
    (bak,) = updater.list_backups(data, "Foo")
    assert bak["version"] == "1.0.0"
    assert (data / "backups" / "Foo" / bak["file"]).read_bytes() == OLD


def test_update_compile_failure_rolls_back(tmp_path, monkeypatch):
    oxide, data = make_server(tmp_path)
    result = run_update(oxide, data, monkeypatch, broken=(b"1.1.0",))
    assert not result["ok"] and result["rolled_back"]
    assert "boom" in result["message"] and "v1.0.0 loaded" in result["message"]
    assert (oxide / "plugins" / "Foo.cs").read_bytes() == OLD


def test_update_unverified_when_server_silent(tmp_path, monkeypatch):
    oxide, data = make_server(tmp_path)
    monkeypatch.setattr(updater, "VERIFY_TIMEOUT", 0.2)

    async def fake_download(umod, client, name):
        return {"download_sha1": "x"}, NEW, {"title": "Foo Bar", "version": "1.1.0"}

    monkeypatch.setattr(updater, "_download", fake_download)
    result = asyncio.run(updater.update_plugin(oxide, data, None, None, "Foo"))
    assert result["ok"] and result["verified"] is False
    assert (oxide / "plugins" / "Foo.cs").read_bytes() == NEW


def test_manual_rollback(tmp_path, monkeypatch):
    oxide, data = make_server(tmp_path)
    run_update(oxide, data, monkeypatch)

    async def go():
        server = asyncio.create_task(fake_oxide(oxide))
        await asyncio.sleep(0.1)
        try:
            return await updater.rollback_plugin(oxide, data, "Foo")
        finally:
            server.cancel()

    result = asyncio.run(go())
    assert result["ok"] and (result["from"], result["to"]) == ("1.1.0", "1.0.0")
    assert (oxide / "plugins" / "Foo.cs").read_bytes() == OLD


def test_rejects_bad_names(tmp_path):
    oxide, data = make_server(tmp_path)
    result = asyncio.run(updater.rollback_plugin(oxide, data, "../evil"))
    assert not result["ok"] and "Invalid" in result["message"]


BAR = b'namespace Oxide.Plugins { [Info("Bar Thing", "me", "2.0.0")] class Bar {} }'


class FakeUmod:
    def __init__(self):
        self.cache = {}


def run_install(oxide, monkeypatch, requested="bar", log_line=None):
    async def fake_download(umod, client, name):
        umod.cache[name] = {"found": True, "data": {"name": "Bar"}}
        return {"data": {"name": "Bar"}, "download_sha1": "x"}, BAR, {"title": "Bar Thing", "version": "2.0.0"}

    monkeypatch.setattr(updater, "_download", fake_download)
    monkeypatch.setattr(updater, "VERIFY_TIMEOUT", 3)

    async def server():
        path = oxide / "plugins" / "Bar.cs"
        while not path.exists():
            await asyncio.sleep(0.05)
        if log_line:
            with (oxide / "logs" / "oxide_2026-11-10.txt").open("a") as fh:
                fh.write(log_line)

    async def go():
        task = asyncio.create_task(server())
        try:
            return await updater.install_plugin(oxide, FakeUmod(), None, requested)
        finally:
            task.cancel()

    return asyncio.run(go())


def test_install_uses_umods_file_name(tmp_path, monkeypatch):
    oxide, _ = make_server(tmp_path)
    result = run_install(oxide, monkeypatch, log_line="14:00 [Info] Loaded plugin Bar Thing v2.0.0 by me\n")
    assert result["ok"] and result["verified"] and result["name"] == "Bar" and result["to"] == "2.0.0"
    assert (oxide / "plugins" / "Bar.cs").read_bytes() == BAR


def test_install_removes_file_when_it_fails_to_compile(tmp_path, monkeypatch):
    oxide, _ = make_server(tmp_path)
    result = run_install(oxide, monkeypatch, log_line="14:00 [Error] Error while compiling Bar: nope | Line: 1, Pos: 1\n")
    assert not result["ok"] and result["rolled_back"] and "nope" in result["message"]
    assert not (oxide / "plugins" / "Bar.cs").exists()


def test_install_refuses_existing_plugin(tmp_path, monkeypatch):
    oxide, _ = make_server(tmp_path)
    (oxide / "plugins" / "Bar.cs").write_bytes(b"mine")
    result = run_install(oxide, monkeypatch)
    assert not result["ok"] and "already installed" in result["message"]
    assert (oxide / "plugins" / "Bar.cs").read_bytes() == b"mine"
