import json

import pytest

from sqlscout.config import (
    Server, load_config, remove_user_server, save_user_server, user_config_path,
)
from sqlscout.errors import ConfigError


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLSCOUT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_no_config_is_empty():
    cfg = load_config()
    assert cfg.servers == {} and cfg.aliases == {}


def test_save_and_reload_windows_server():
    save_user_server(Server(name="dev", host="sql01", env="dev"))
    cfg = load_config()
    assert cfg.servers["dev"].host == "sql01"
    assert cfg.servers["dev"].source == "user"
    assert cfg.servers["dev"].databases == "*"


def test_remove_server():
    save_user_server(Server(name="dev", host="sql01"))
    assert remove_user_server("dev") is True
    assert remove_user_server("dev") is False
    assert load_config().servers == {}


def test_project_overrides_user_and_adds_aliases(isolated_home):
    save_user_server(Server(name="dev", host="user-host"))
    (isolated_home / ".sqlscout.json").write_text(json.dumps({
        "servers": {"dev": {"host": "project-host"}},
        "aliases": {"sales": {"server": "dev", "database": "Sales"}},
    }))
    cfg = load_config()
    assert cfg.servers["dev"].host == "project-host"
    assert cfg.servers["dev"].source == "project"
    assert cfg.aliases["sales"].database == "Sales"


def test_project_config_found_in_parent(isolated_home):
    (isolated_home / ".sqlscout.json").write_text(json.dumps({"servers": {"p": {"host": "h"}}}))
    child = isolated_home / "a" / "b"
    child.mkdir(parents=True)
    assert "p" in load_config(child).servers


@pytest.mark.parametrize("server,msg", [
    (Server(name="bad name", host="h"), "invalid server name"),
    (Server(name="x", host="h", auth="kerberos"), "auth must be"),
    (Server(name="x", auth="windows"), "'host' is required"),
    (Server(name="x", host="h", auth="sql", user="u"), "password_ref"),
    (Server(name="x", host="h", auth="sql", password_ref="env:P"), "'user' is required"),
    (Server(name="x", host="h", auth="sql", user="u", password_ref="hunter2"), "password_ref"),
    (Server(name="x", auth="connection_string", connection_string="Server=h;PWD=secret"),
     "literal password"),
    (Server(name="x", auth="connection_string", connection_string="Server=h;Password=secret"),
     "literal password"),
    (Server(name="x", host="h", databases="Sales"), "databases"),
])
def test_validation_rejects(server, msg):
    with pytest.raises(ConfigError) as exc:
        server.validate()
    assert msg in str(exc.value)


def test_connection_string_placeholder_is_accepted():
    Server(name="x", auth="connection_string",
           connection_string="Server=h;UID=u;PWD=${X_PWD}").validate()


def test_unknown_field_rejected(isolated_home):
    (isolated_home / ".sqlscout.json").write_text(
        json.dumps({"servers": {"p": {"host": "h", "password": "oops"}}}))
    with pytest.raises(ConfigError, match="unknown field"):
        load_config()


def test_bad_json_reports_path(isolated_home):
    p = user_config_path()
    p.parent.mkdir(parents=True)
    p.write_text("{nope")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config()


def test_saved_file_never_contains_password(isolated_home):
    save_user_server(Server(name="s", host="h", auth="sql", user="u", password_ref="env:S_PWD"))
    text = user_config_path().read_text()
    assert "env:S_PWD" in text
