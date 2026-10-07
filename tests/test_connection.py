import pytest

from sqlscout.config import Server
from sqlscout.connection import (
    build_connection_string, default_database, expand_env, pick_driver, redact,
)
from sqlscout.errors import ConfigError, DriverNotFound

D18 = "ODBC Driver 18 for SQL Server"


def test_pick_driver_prefers_newest():
    assert pick_driver(["SQL Server", "ODBC Driver 17 for SQL Server", D18]) == D18
    assert pick_driver(["SQL Server", "ODBC Driver 17 for SQL Server"]) == "ODBC Driver 17 for SQL Server"
    assert pick_driver(["Microsoft Access Driver", "SQL Server"]) == "SQL Server"


def test_pick_driver_none(monkeypatch):
    monkeypatch.delenv("SQLSCOUT_ODBC_DRIVER", raising=False)
    with pytest.raises(DriverNotFound):
        pick_driver(["Microsoft Access Driver"])


def test_pick_driver_env_override(monkeypatch):
    monkeypatch.setenv("SQLSCOUT_ODBC_DRIVER", "My Driver")
    assert pick_driver(["SQL Server"]) == "My Driver"


def test_windows_auth_string():
    s = Server(name="x", host="localhost\\SQLEXPRESS")
    cs = build_connection_string(s, "Sales", driver=D18)
    assert "SERVER=localhost\\SQLEXPRESS;" in cs
    assert "DATABASE=Sales;" in cs
    assert "Trusted_Connection=yes" in cs
    assert "ApplicationIntent=ReadOnly" in cs
    assert "UID=" not in cs


def test_legacy_driver_omits_modern_attributes():
    cs = build_connection_string(Server(name="x", host="h"), "d", driver="SQL Server")
    assert "TrustServerCertificate" not in cs and "ApplicationIntent" not in cs


def test_sql_auth_string_escapes_password():
    s = Server(name="x", host="h", auth="sql", user="u", password_ref="env:P")
    cs = build_connection_string(s, "d", driver=D18, password="a;b}c")
    assert "PWD={a;b}}c};" in cs


def test_verify_certificate_flag():
    s = Server(name="x", host="h", trust_server_certificate=False)
    assert "TrustServerCertificate" not in build_connection_string(s, "d", driver=D18)


def test_connection_string_env_expansion_and_db_override(monkeypatch):
    monkeypatch.setenv("MY_PWD", "s3cret")
    s = Server(name="x", auth="connection_string",
               connection_string="Server=h;Database=Old;UID=u;PWD=${MY_PWD}")
    cs = build_connection_string(s, "New", driver=D18)
    assert "PWD=s3cret" in cs
    assert "DATABASE=New" in cs and "Old" not in cs
    assert cs.startswith("DRIVER={ODBC Driver 18 for SQL Server};")


def test_missing_env_var_is_reported(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ConfigError, match="NOPE"):
        expand_env("PWD=${NOPE}")


def test_default_database():
    assert default_database(Server(name="x", host="h")) == "master"
    s = Server(name="x", auth="connection_string", connection_string="Server=h;Initial Catalog=Foo")
    assert default_database(s) == "Foo"


def test_redact():
    assert "secret" not in redact("x PWD=secret;UID=u")
    assert "secret" not in redact("Password={se;cret};a=b")
