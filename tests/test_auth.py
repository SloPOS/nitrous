import json
import os

from app.auth import Auth


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def write_config(data, **settings):
    data.mkdir(parents=True, exist_ok=True)
    path = data / "config.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    # make sure the change is visible even on filesystems with coarse timestamps
    stamp = path.stat().st_mtime + write_config.bump
    write_config.bump += 5
    os.utime(path, (stamp, stamp))


write_config.bump = 5


def make(tmp_path, **settings):
    clock = Clock()
    write_config(tmp_path, **({"password": "right"} | settings))
    return Auth(tmp_path, now=clock), clock


def test_no_password_means_open(tmp_path):
    auth = Auth(tmp_path)
    assert auth.check("1.1.1.1", None) == ("open", 0.0)
    assert auth.password_set is False


def test_env_password_is_used_without_config_file(tmp_path):
    auth = Auth(tmp_path, env_password="fromenv")
    assert auth.check("a", "fromenv")[0] == "ok"
    assert auth.check("a", "nope")[0] == "bad"


def test_config_file_password_beats_env(tmp_path):
    write_config(tmp_path, password="fromfile")
    auth = Auth(tmp_path, env_password="fromenv")
    assert auth.check("a", "fromfile")[0] == "ok"
    assert auth.check("a", "fromenv")[0] == "bad"


def test_locks_after_five_wrong_attempts_even_for_the_right_password(tmp_path):
    auth, clock = make(tmp_path)
    for _ in range(4):
        assert auth.check("a", "wrong")[0] == "bad"
    result, left = auth.check("a", "wrong")
    assert result == "locked" and left == 15 * 60
    assert auth.check("a", "right")[0] == "locked"
    assert auth.check("b", "right")[0] == "ok"  # other devices are unaffected


def test_missing_password_is_not_counted(tmp_path):
    auth, _ = make(tmp_path)
    for _ in range(20):
        assert auth.check("a", None)[0] == "missing"
    assert auth.check("a", "right")[0] == "ok"


def test_success_resets_the_count(tmp_path):
    auth, _ = make(tmp_path)
    for _ in range(4):
        auth.check("a", "wrong")
    assert auth.check("a", "right")[0] == "ok"
    for _ in range(4):
        assert auth.check("a", "wrong")[0] == "bad"


def test_lockout_expires(tmp_path):
    auth, clock = make(tmp_path)
    for _ in range(5):
        auth.check("a", "wrong")
    clock.t += 15 * 60 - 1
    assert auth.check("a", "right")[0] == "locked"
    clock.t += 2
    assert auth.check("a", "right")[0] == "ok"


def test_changing_the_password_on_the_server_unlocks(tmp_path):
    auth, _ = make(tmp_path)
    for _ in range(5):
        auth.check("a", "wrong")
    write_config(tmp_path, password="brand-new")
    assert auth.check("a", "right")[0] == "bad"
    assert auth.check("a", "brand-new")[0] == "ok"


def test_deleting_lockouts_file_unlocks(tmp_path):
    auth, _ = make(tmp_path)
    for _ in range(5):
        auth.check("a", "wrong")
    assert (tmp_path / "lockouts.json").exists()
    (tmp_path / "lockouts.json").unlink()
    assert auth.check("a", "right")[0] == "ok"


def test_lockout_survives_a_restart(tmp_path):
    auth, clock = make(tmp_path)
    for _ in range(5):
        auth.check("a", "wrong")
    restarted = Auth(tmp_path, now=clock)
    assert restarted.check("a", "right")[0] == "locked"


def test_settings_are_configurable(tmp_path):
    auth, clock = make(tmp_path, max_attempts=2, lockout_minutes=0)
    auth.check("a", "wrong")
    result, left = auth.check("a", "wrong")
    assert result == "locked" and left == float("inf")
    clock.t += 10 * 365 * 86400
    assert auth.check("a", "right")[0] == "locked"  # 0 minutes = until unlocked by hand


def test_broken_config_file_falls_back_to_env(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    auth = Auth(tmp_path, env_password="fromenv")
    assert auth.check("a", "fromenv")[0] == "ok"


def test_http_guard(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    write_config(tmp_path, password="right")
    monkeypatch.setattr(main, "auth", Auth(tmp_path))
    client = TestClient(main.app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/plugins").status_code == 401
    assert client.get("/api/plugins", auth=("x", "right")).status_code == 200
    for _ in range(5):
        client.get("/api/plugins", auth=("x", "wrong"))
    locked = client.get("/api/plugins", auth=("x", "right"))
    assert locked.status_code == 429 and "config.json" in locked.text and "Retry-After" in locked.headers
