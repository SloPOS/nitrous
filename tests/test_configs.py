import pytest

from app import configs
from app.updater import UpdateError

ORIGINAL = '{\n  "Speed": 1.0,\n  "Owner": 76561198000000001\n}'


def make(tmp_path):
    oxide, data = tmp_path / "oxide", tmp_path / "data"
    (oxide / "config").mkdir(parents=True)
    (oxide / "config" / "Foo.json").write_bytes(ORIGINAL.encode())  # bytes: no newline translation
    return oxide, data


def test_read_reports_text_and_hash(tmp_path):
    oxide, data = make(tmp_path)
    cfg = configs.read_config(oxide, data, "Foo")
    assert cfg["exists"] and cfg["text"] == ORIGINAL and cfg["error"] is None and cfg["backups"] == []
    assert configs.read_config(oxide, data, "Missing")["exists"] is False


def test_save_backs_up_and_preserves_big_numbers(tmp_path):
    oxide, data = make(tmp_path)
    sha = configs.read_config(oxide, data, "Foo")["sha1"]
    new = ORIGINAL.replace("1.0", "2.5")
    saved = configs.save_config(oxide, data, "Foo", new, sha)
    assert saved["changed"]
    assert (oxide / "config" / "Foo.json").read_bytes() == new.encode()
    (bak,) = configs.list_backups(data, "Foo")
    assert (data / "config-backups" / "Foo" / bak["file"]).read_bytes() == ORIGINAL.encode()


def test_save_rejects_invalid_json_and_stale_edits(tmp_path):
    oxide, data = make(tmp_path)
    sha = configs.read_config(oxide, data, "Foo")["sha1"]
    with pytest.raises(UpdateError, match="Not valid JSON"):
        configs.save_config(oxide, data, "Foo", '{"Speed": }', sha)
    with pytest.raises(UpdateError, match="object or list"):
        configs.save_config(oxide, data, "Foo", "42", sha)
    with pytest.raises(configs.ConfigConflict):
        configs.save_config(oxide, data, "Foo", '{"Speed": 3}', "not-the-current-hash")
    with pytest.raises(UpdateError, match="Invalid plugin name"):
        configs.save_config(oxide, data, "../x", "{}", None)
    assert (oxide / "config" / "Foo.json").read_bytes() == ORIGINAL.encode()
    assert configs.list_backups(data, "Foo") == []


def test_unchanged_save_makes_no_backup(tmp_path):
    oxide, data = make(tmp_path)
    sha = configs.read_config(oxide, data, "Foo")["sha1"]
    assert configs.save_config(oxide, data, "Foo", ORIGINAL, sha)["changed"] is False
    assert configs.list_backups(data, "Foo") == []


def test_restore_puts_back_earlier_version(tmp_path):
    oxide, data = make(tmp_path)
    sha = configs.read_config(oxide, data, "Foo")["sha1"]
    configs.save_config(oxide, data, "Foo", '{"Speed": 9}', sha)
    (bak,) = configs.list_backups(data, "Foo")
    assert configs.restore_config(oxide, data, "Foo", bak["file"])["changed"]
    assert (oxide / "config" / "Foo.json").read_bytes() == ORIGINAL.encode()
    assert len(configs.list_backups(data, "Foo")) == 2  # the replaced version was kept too
    with pytest.raises(UpdateError):
        configs.restore_config(oxide, data, "Foo", "../../etc/passwd")
