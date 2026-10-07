import json

import pytest

from sqlscout.catalog import Catalog
from sqlscout.config import Server
from sqlscout.discovery import DiscoveryResult
from sqlscout.errors import CatalogError


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLSCOUT_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".sqlscout.json").write_text(json.dumps({
        "servers": {
            "fixed": {"host": "h", "databases": ["A", "B"], "env": "test"},
            "open": {"host": "h2"},
        },
        "aliases": {"short": {"server": "fixed", "database": "A", "notes": "n"}},
    }))
    c = Catalog()
    local = Server(name="local/default", host="localhost", source="discovered",
                   databases=["Alpha"])
    down = Server(name="local/SQLEXPRESS", host="localhost\\SQLEXPRESS", source="discovered")
    c._discovered = [
        DiscoveryResult(local, "ok", ["Alpha"]),
        DiscoveryResult(down, "unreachable", error="boom"),
    ]
    c._enumerated["open"] = (["X"], "")
    return c


def test_list_databases(cat):
    out = cat.list_databases()
    keys = {d["key"] for d in out["databases"]}
    assert keys == {"local/default/Alpha", "fixed/A", "fixed/B", "open/X", "short"}
    assert out["servers_unavailable"] == [
        {"server": "local/SQLEXPRESS", "status": "unreachable", "error": "boom"}
    ]


def test_resolve_discovered_with_slashes_in_server_name(cat):
    server, db = cat.resolve("local/default/Alpha")
    assert server.name == "local/default" and db == "Alpha"


def test_resolve_alias(cat):
    server, db = cat.resolve("short")
    assert server.name == "fixed" and db == "A"


def test_allowlist_enforced(cat):
    with pytest.raises(CatalogError, match="allowlist"):
        cat.resolve("fixed/Secret")


def test_wildcard_server_allows_any_database(cat):
    server, db = cat.resolve("open/Whatever")
    assert server.name == "open" and db == "Whatever"


@pytest.mark.parametrize("key", ["nope", "nope/db", "/db", "fixed/"])
def test_unknown_keys(cat, key):
    with pytest.raises(CatalogError):
        cat.resolve(key)
