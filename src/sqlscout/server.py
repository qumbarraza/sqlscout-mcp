"""MCP server (stdio). Never write to stdout here: it carries the protocol."""

from __future__ import annotations

import datetime as _dt
import decimal as _dec
import sys
import uuid as _uuid
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .catalog import Catalog
from .config import (
    Server, home_dir, project_config_path, remove_user_server, save_user_server,
    user_config_path,
)
from .connection import (
    MAX_ROWS, check_server, installed_drivers, pick_driver, run_query,
)
from .errors import CatalogError, DriverNotFound

mcp = FastMCP("sqlscout")
_catalog: Catalog | None = None


def catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = Catalog()
    return _catalog


# ---- helpers ---------------------------------------------------------------

def _coerce(value: Any) -> Any:
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dec.Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, _uuid.UUID):
        return str(value)
    return value


def _records(cols: list[str], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [{c: _coerce(v) for c, v in zip(cols, r)} for r in rows]


def _split_table(table: str) -> tuple[str | None, str]:
    cleaned = table.replace("[", "").replace("]", "")
    schema, dot, name = cleaned.rpartition(".")
    return (schema or None) if dot else None, name


def _quote(table: str) -> str:
    schema, name = _split_table(table)
    q = lambda s: "[" + s.replace("]", "]]") + "]"  # noqa: E731
    return f"{q(schema)}.{q(name)}" if schema else q(name)


def _check_range(name: str, value: int, hi: int) -> None:
    if not isinstance(value, int) or value < 1 or value > hi:
        raise ValueError(f"{name} must be an int in [1, {hi}]")


# ---- discovery / configuration tools ----------------------------------------

@mcp.tool()
def ping() -> dict[str, Any]:
    """Health check. Touches no database. Shows driver, config paths and counts."""
    try:
        driver = pick_driver()
    except DriverNotFound as e:
        driver = f"<none> ({e})"
    cat = catalog()
    cat.reload_config()
    proj = project_config_path()
    return {
        "server": "sqlscout", "version": __version__,
        "python": sys.version.split()[0],
        "odbc_driver": driver, "odbc_drivers_installed": installed_drivers(),
        "user_config": {"path": str(user_config_path()), "exists": user_config_path().is_file()},
        "project_config": str(proj) if proj else None,
        "configured_servers": len(cat.config.servers),
        "aliases": len(cat.config.aliases),
        "home": str(home_dir()),
        "tools": ["ping", "list_servers", "list_databases", "refresh_discovery", "add_server",
                  "remove_server", "test_connection", "list_tables", "describe_table",
                  "head", "count", "query"],
    }


@mcp.tool()
def list_servers() -> list[dict[str, Any]]:
    """List auto-discovered local instances and configured servers, with status."""
    return catalog().list_servers()


@mcp.tool()
def list_databases() -> dict[str, Any]:
    """List every database key usable as `db` in other tools.

    Keys look like `local/default/MyDb` (auto-discovered) or `<server>/<db>`
    (configured), plus any aliases. `servers_unavailable` lists servers that
    were found or configured but could not be queried, with the reason
    (e.g. needs_auth: add it with add_server).
    """
    return catalog().list_databases()


@mcp.tool()
def refresh_discovery() -> dict[str, Any]:
    """Re-scan this machine for SQL Server instances and re-read config files."""
    cat = catalog()
    cat.refresh()
    return cat.list_databases()


@mcp.tool()
def add_server(
    name: str,
    host: str = "",
    auth: str = "windows",
    user: str = "",
    password_env: str = "",
    connection_string: str = "",
    databases: list[str] | str = "*",
    env: str = "",
    notes: str = "",
    trust_server_certificate: bool = True,
) -> dict[str, Any]:
    """Add a SQL Server to the user config (~/.sqlscout/config.json) and test it.

    auth="windows": needs `host`.
    auth="sql": needs `host`, `user`, and `password_env` = NAME of an environment
        variable holding the password. Passwords are never accepted here; to store
        one in Windows Credential Manager run `sqlscout add-server --prompt-password`.
    auth="connection_string": needs `connection_string`; reference secrets as
        ${ENV_VAR}, e.g. "Server=h;UID=u;PWD=${MY_PWD}". Literal passwords are rejected.
    `databases`: "*" to enumerate all, or a list that acts as an allowlist.
    Use a db_datareader-only login whenever possible.
    """
    server = Server(
        name=name, host=host, auth=auth, user=user,
        password_ref=f"env:{password_env}" if password_env else "",
        connection_string=connection_string, databases=databases,
        env=env, notes=notes, trust_server_certificate=trust_server_certificate,
        source="user",
    )
    path = save_user_server(server)
    cat = catalog()
    cat.reload_config()
    cat._enumerated.pop(name, None)
    try:
        result = check_server(server)
    except Exception as e:  # saved either way; report why the test failed
        result = {"server": name, "ok": False, "error": str(e)}
    return {"saved_to": str(path), "test": result}


@mcp.tool()
def remove_server(name: str) -> dict[str, Any]:
    """Remove a server from the user config. Discovered and project servers can't be removed here."""
    removed = remove_user_server(name)
    cat = catalog()
    cat.reload_config()
    cat._enumerated.pop(name, None)
    return {"removed": removed, "name": name}


@mcp.tool()
def test_connection(server: str, database: str = "") -> dict[str, Any]:
    """Connect to a server (name from list_servers), report login/version, warn if sysadmin."""
    srv = catalog().servers().get(server)
    if srv is None:
        raise CatalogError(f"unknown server '{server}'. Use list_servers.")
    return check_server(srv, database or None)


# ---- query tools ------------------------------------------------------------

@mcp.tool()
def list_tables(
    db: str, schema: str | None = None, name_like: str | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    """List tables/views in `db` (key from list_databases).

    `name_like` is a SQL LIKE pattern on the table name (% wildcard). `limit` max 10000.
    """
    _check_range("limit", limit, 10_000)
    where: list[str] = []
    params: list[Any] = [limit]
    if schema:
        where.append("TABLE_SCHEMA = ?")
        params.append(schema)
    if name_like:
        where.append("TABLE_NAME LIKE ?")
        params.append(name_like)
    sql = (
        "SELECT TOP (?) TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY TABLE_SCHEMA, TABLE_NAME"
    )
    server, database = catalog().resolve(db)
    cols, rows, _ = run_query(server, database, sql, params)
    return _records(cols, rows)


@mcp.tool()
def describe_table(db: str, table: str) -> list[dict[str, Any]]:
    """Column metadata for `table` ('schema.table' or bare name) in `db`."""
    schema, name = _split_table(table)
    sql = (
        "SELECT TABLE_SCHEMA, ORDINAL_POSITION, COLUMN_NAME, DATA_TYPE, "
        "CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE, COLUMN_DEFAULT "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ?"
        + (" AND TABLE_SCHEMA = ?" if schema else "")
        + " ORDER BY TABLE_SCHEMA, ORDINAL_POSITION"
    )
    server, database = catalog().resolve(db)
    cols, rows, _ = run_query(server, database, sql, [name] + ([schema] if schema else []))
    return _records(cols, rows)


@mcp.tool()
def head(db: str, table: str, n: int = 10) -> dict[str, Any]:
    """First `n` rows (max 10000, response capped at 1000 rows) of `table`."""
    _check_range("n", n, 10_000)
    server, database = catalog().resolve(db)
    cols, rows, truncated = run_query(server, database, f"SELECT TOP {n} * FROM {_quote(table)}")
    return {"columns": cols, "row_count": len(rows), "truncated": truncated,
            "rows": _records(cols, rows)}


@mcp.tool()
def count(db: str, table: str, where: str | None = None) -> dict[str, Any]:
    """Row count of `table`, optionally filtered by `where` (trusted text; guard re-checks it)."""
    sql = f"SELECT COUNT_BIG(*) AS n FROM {_quote(table)}" + (f" WHERE {where}" if where else "")
    server, database = catalog().resolve(db)
    _cols, rows, _ = run_query(server, database, sql)
    return {"db": db, "table": table, "where": where, "count": int(rows[0][0]) if rows else 0}


@mcp.tool()
def query(db: str, sql: str) -> dict[str, Any]:
    """Run one read-only SELECT/WITH statement against `db`.

    Anything else (DML, DDL, EXEC, SELECT INTO, multiple statements) is rejected.
    Rows are capped at 1000; use TOP/WHERE to narrow. 30 second timeout.
    """
    server, database = catalog().resolve(db)
    cols, rows, truncated = run_query(server, database, sql, max_rows=MAX_ROWS)
    return {"columns": cols, "row_count": len(rows), "truncated": truncated,
            "rows": _records(cols, rows)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
