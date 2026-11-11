from app.main import classify
from app.scanner import compare_versions, parse_source, scan_plugins


def test_parse_info_and_description():
    src = '''
    namespace Oxide.Plugins {
        [Info("Advanced Gather", "Khan", "2.0.4")]
        [Description("Custom \\"gathering\\" rates")]
        class AdvancedGather : RustPlugin {}
    }'''
    meta = parse_source(src)
    assert meta["title"] == "Advanced Gather"
    assert meta["author"] == "Khan"
    assert meta["version"] == "2.0.4"
    assert meta["description"] == 'Custom \\"gathering\\" rates'


def test_parse_info_multiline_and_missing():
    assert parse_source('[Info(\n "A",\n "B",\n "1.0.0",\n ResourceId = 5)]')["version"] == "1.0.0"
    assert parse_source("class X {}")["version"] is None
    assert parse_source('[Info("Perms UI", "Camoec", 1.3)]')["version"] == "1.3"


def test_compare_versions():
    assert compare_versions("1.9.6", "1.10.0") == -1
    assert compare_versions("2.0", "2.0.0") == 0
    assert compare_versions("3.1.0", "3.0.9") == 1
    assert compare_versions(None, "1.0.0") is None


def test_scan_and_classify(tmp_path):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "plugins" / "Foo.cs").write_bytes(b'[Info("Foo", "me", "1.0.0")]')
    (tmp_path / "config" / "Foo.json").write_text("{}")
    (p,) = scan_plugins(tmp_path)
    assert p["name"] == "Foo" and p["has_config"] and p["version"] == "1.0.0"

    def entry(version, checksum):
        return {"fetched_at": 1, "found": True,
                "data": {"latest_release_version": version, "latest_release_version_checksum": checksum}}

    assert classify(p, None)["status"] == "unchecked"
    assert classify(p, {"fetched_at": 1, "found": False})["status"] == "not_on_umod"
    assert classify(p, entry("1.0.0", p["sha1"]))["status"] == "up_to_date"
    assert classify(p, entry("1.2.0", "x"))["status"] == "update_available"
    assert classify(p, entry("1.0.0", "x"))["status"] == "modified"
    assert classify(p, entry("0.9.0", "x"))["status"] == "newer_local"
    stale = {**entry("1.0.0", "x"), "download_sha1": p["sha1"]}
    assert classify(p, stale)["status"] == "up_to_date"
    assert classify(p, {"fetched_at": 1, "found": False, "error": "boom"})["status"] == "error"
