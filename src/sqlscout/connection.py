"""Connection strings, driver selection, secrets, and guarded query execution."""

from __future__ import annotations

import os
import re
from typing import Any, Sequence

import pyodbc

from .config import Server
from .errors import ConfigError, ConnectionFailed, DriverNotFound
from .guard import assert_read_only

KEYRING_SERVICE = "sqlscout"
MAX_ROWS = 1000

_DRIVER_PREFERENCE = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "ODBC Driver 11 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server Native Client 10.0",
    "SQL Server",
]
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DB_KEY = re.compile(r"(?i)(?:^|;)\s*(?:database|initial catalog)\s*=\s*([^;]*)")


# ---- driver ----------------------------------------------------------------

def installed_drivers() -> list[str]:
    return list(pyodbc.drivers())


def pick_driver(available: Sequence[str] | None = None) -> str:
    override = os.environ.get("SQLSCOUT_ODBC_DRIVER")
    if override:
        return override
    available = list(available if available is not None else installed_drivers())
    for name in _DRIVER_PREFERENCE:
        if name in available:
            return name
    for name in available:
        if "SQL Server" in name:
            return name
    raise DriverNotFound(
        "No SQL Server ODBC driver found. Install 'ODBC Driver 18 for SQL Server' from "
        "https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server"
    )


def _modern(driver: str) -> bool:
    return driver.startswith("ODBC Driver")


# ---- secrets ---------------------------------------------------------------

def expand_env(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        value = os.environ.get(m.group(1))
        if value is None:
            raise ConfigError(f"environment variable {m.group(1)} is not set")
        return value
    return _ENV_PLACEHOLDER.sub(repl, text)


def resolve_password(server: Server) -> str:
    ref = server.password_ref
    if ref.startswith("env:"):
        value = os.environ.get(ref[4:])
        if not value:
            raise ConfigError(f"server '{server.name}': environment variable {ref[4:]} is not set")
        return value
    if ref == "keyring":
        import keyring  # lazy: only needed for keyring-backed servers
        value = keyring.get_password(KEYRING_SERVICE, f"{server.name}:{server.user}")
        if not value:
            raise ConfigError(
                f"server '{server.name}': no password in Windows Credential Manager. "
                f"Run: sqlscout add-server {server.name} --prompt-password ..."
            )
        return value
    raise ConfigError(f"server '{server.name}': no password_ref configured")


def redact(text: str) -> str:
    return re.sub(r"(?i)\b(pwd|password)\s*=\s*(\{[^}]*\}|[^;]*)", r"\1=***", text)


# ---- connection string -----------------------------------------------------

def _val(value: str) -> str:
    """Escape an ODBC attribute value."""
    if any(c in value for c in ";{}") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def default_database(server: Server) -> str:
    """Database to connect to when enumerating or testing a server."""
    if server.auth == "connection_string":
        m = _DB_KEY.search(server.connection_string)
        if m and "${" not in m.group(1):
            return m.group(1).strip()
    return "master"


def build_connection_string(
    server: Server, database: str, *, driver: str, password: str | None = None
) -> str:
    modern = _modern(driver)
    if server.auth == "connection_string":
        cs = expand_env(server.connection_string).strip().rstrip(";")
        lower = cs.lower()
        if database:
            cs = _DB_KEY.sub("", cs).lstrip(";") + f";DATABASE={_val(database)}"
        if "driver=" not in lower:
            cs = f"DRIVER={{{driver}}};{cs}"
        if modern:
            if "trustservercertificate" not in lower and "encrypt" not in lower \
                    and server.trust_server_certificate:
                cs += ";TrustServerCertificate=yes"
            if "applicationintent" not in lower:
                cs += ";ApplicationIntent=ReadOnly"
        if "app=" not in lower and "application name" not in lower:
            cs += ";APP=sqlscout"
        return cs + ";"

    parts = [f"DRIVER={{{driver}}}", f"SERVER={_val(server.host)}", f"DATABASE={_val(database)}"]
    if server.auth == "windows":
        parts.append("Trusted_Connection=yes")
    else:
        parts += [f"UID={_val(server.user)}", f"PWD={_val(password or resolve_password(server))}"]
    if modern:
        if server.trust_server_certificate:
            parts.append("TrustServerCertificate=yes")
        parts.append("ApplicationIntent=ReadOnly")
    parts.append("APP=sqlscout")
    return ";".join(parts) + ";"


def open_connection(
    server: Server,
    database: str | None = None,
    *,
    login_timeout: int = 15,
    query_timeout: int = 30,
) -> pyodbc.Connection:
    db = database or default_database(server)
    cs = build_connection_string(server, db, driver=pick_driver())
    try:
        conn = pyodbc.connect(cs, autocommit=True, readonly=True, timeout=login_timeout)
    except pyodbc.Error as e:
        sqlstate = str(e.args[0]) if e.args else ""
        raise ConnectionFailed(
            f"cannot connect to '{server.name}' ({db}): {redact(str(e))}", sqlstate
        ) from None
    conn.timeout = query_timeout
    return conn


# ---- guarded execution -----------------------------------------------------

def run_query(
    server: Server,
    database: str | None,
    sql: str,
    params: Sequence[Any] | None = None,
    *,
    max_rows: int = MAX_ROWS,
    login_timeout: int = 15,
    query_timeout: int = 30,
) -> tuple[list[str], list[tuple[Any, ...]], bool]:
    """Guard-check and run `sql`. Returns (columns, rows, truncated)."""
    assert_read_only(sql)
    conn = open_connection(
        server, database, login_timeout=login_timeout, query_timeout=query_timeout
    )
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql, list(params or []))
            cols = [c[0] for c in cur.description] if cur.description else []
            fetched = cur.fetchmany(max_rows + 1) if cols else []
        except pyodbc.Error as e:
            raise ConnectionFailed(f"query failed on '{server.name}': {redact(str(e))}") from None
        truncated = len(fetched) > max_rows
        return cols, [tuple(r) for r in fetched[:max_rows]], truncated
    finally:
        conn.close()


def check_server(server: Server, database: str | None = None) -> dict[str, Any]:
    """Connect, report identity, and warn if the login could write."""
    cols, rows, _ = run_query(
        server,
        database,
        "SELECT @@VERSION AS version, SUSER_SNAME() AS login_name, "
        "IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin",
        login_timeout=8,
    )
    row = dict(zip(cols, rows[0])) if rows else {}
    warnings: list[str] = []
    if row.get("is_sysadmin") == 1:
        warnings.append(
            "login is sysadmin: the read-only guard is the only thing preventing writes. "
            "Prefer a db_datareader-only login."
        )
    return {
        "server": server.name,
        "ok": True,
        "login": row.get("login_name"),
        "version": str(row.get("version", "")).splitlines()[0] if row else "",
        "warnings": warnings,
    }
