# sqlscout-mcp

A **read-only SQL Server** [MCP](https://modelcontextprotocol.io) server for Claude Code (and any MCP client). Windows only.

- **Zero-config start:** finds SQL Server instances on your machine (installed, LocalDB, Docker/TCP) and lists their databases using Windows authentication.
- **Add any other server** by host or ODBC connection string, with secrets kept out of config files.
- **Read-only by design:** every query passes a guard that allows a single `SELECT`/`WITH` and nothing else.

## Install

Prerequisites: Windows, Python 3.10+, and the [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server) (driver 17 also works).

With [uv](https://docs.astral.sh/uv/) (recommended, nothing to clone or activate):

```bash
claude mcp add sqlscout --scope user -- uvx --from git+https://github.com/qumbarraza/sqlscout-mcp sqlscout serve
```

Or with pip:

```bash
pip install git+https://github.com/qumbarraza/sqlscout-mcp
claude mcp add sqlscout --scope user -- sqlscout serve
```

Then in Claude Code ask: *"list my databases"*. Run `sqlscout doctor` first if anything looks off.

## How discovery works

On the first `list_databases` call sqlscout looks for instances via:

1. the Windows registry (installed instances, default and named)
2. LocalDB (`sqllocaldb i`)
3. the local SQL Browser service (named instances and their TCP ports)
4. `localhost,1433` when no default instance exists (typical for Docker)

Each one is probed with your Windows login. User databases appear as `local/<instance>/<database>` (for example `local/default/AdventureWorks`). Instances that are found but not usable are reported with a reason (`needs_auth` or `unreachable`) rather than failing the whole listing. Re-scan any time with `refresh_discovery`.

## Add other servers

Three equivalent ways. All write to `%USERPROFILE%\.sqlscout\config.json` (override the folder with `SQLSCOUT_HOME`).

**CLI**

```bash
# Windows authentication
sqlscout add-server staging --host sql01.corp.local --env staging

# SQL login, password prompted and stored in Windows Credential Manager
sqlscout add-server reports --host sql02,1433 --user report_ro --prompt-password --databases Sales,Inventory

# SQL login, password from an environment variable
sqlscout add-server reports --host sql02 --user report_ro --password-env REPORTS_PWD

# Full ODBC connection string; use ${VAR} for secrets
sqlscout add-server legacy --connection-string "Server=old01;UID=ro;PWD=${LEGACY_PWD};Database=Main"
```

The server is tested right after it is saved. `sqlscout list` shows all database keys; `sqlscout remove-server NAME` removes one.

**From Claude** — ask it to add a server and it will call `add_server`. This tool never accepts a literal password (tool arguments end up in the chat transcript). Use `password_env`, or add the server with the CLI and `--prompt-password`.

**Edit the file** (`config.json`):

```json
{
  "servers": {
    "reports": {
      "host": "sql02,1433",
      "auth": "sql",
      "user": "report_ro",
      "password_ref": "keyring",
      "databases": ["Sales", "Inventory"],
      "env": "prod"
    }
  },
  "aliases": {
    "sales": { "server": "reports", "database": "Sales" }
  }
}
```

`databases` is `"*"` (enumerate everything the login can see, the default) or a list, which then acts as an **allowlist**: other databases on that server cannot be queried.

### Share a catalog with your team

Put a `.sqlscout.json` (same format, **no secrets**) in a repo or project folder. It is picked up from the working directory or any parent, and overrides user config on name clashes. Team members supply their own credentials through env vars or Credential Manager; `password_ref` only names where to find them.

### Secrets

| Where | How |
|---|---|
| `password_ref: "env:NAME"` | read from environment variable `NAME` at connect time |
| `password_ref: "keyring"` | read from Windows Credential Manager (service `sqlscout`) |
| `${NAME}` in a connection string | expanded from the environment at connect time |

Literal passwords in config or connection strings are rejected.

## Tools

| Tool | Purpose |
|---|---|
| `ping` | health check: driver, config paths, counts (touches no database) |
| `list_servers` | discovered and configured servers with status |
| `list_databases` | every usable `db` key, plus servers that could not be queried |
| `refresh_discovery` | re-scan this machine and re-read config |
| `add_server` / `remove_server` | manage user config |
| `test_connection` | connect, show login and version, warn if the login is `sysadmin` |
| `list_tables` | tables and views, with schema / `LIKE` filter |
| `describe_table` | column metadata |
| `head` | first N rows |
| `count` | row count with optional `WHERE` |
| `query` | one read-only `SELECT`/`WITH`, max 1000 rows, 30 s timeout |

## Safety model

Layers, from soft to hard:

1. **Guard** (sqlparse): exactly one statement; must start with `SELECT`/`WITH`; rejects `INSERT/UPDATE/DELETE/MERGE`, `CREATE/ALTER/DROP/TRUNCATE`, `EXEC`, `GRANT/REVOKE/DENY`, `BACKUP/RESTORE`, `USE`, `DBCC`, `SELECT ... INTO`, `OPENROWSET/OPENQUERY/OPENDATASOURCE`, and `NEXT VALUE FOR`. Text inside string literals and comments is ignored.
2. **Connection:** opened read-only with `ApplicationIntent=ReadOnly`, a query timeout, and a row cap.
3. **Your login** — the only hard guarantee. The guard is client-side and protects against mistakes, not a determined attacker. Connect with a login that has just `db_datareader`. `test_connection` warns when the login is `sysadmin`.

Database contents returned by queries are visible to the model. Don't point this at data you would not paste into the chat.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No SQL Server ODBC driver found` | install ODBC Driver 18, or set `SQLSCOUT_ODBC_DRIVER` to an installed driver name |
| Instance shows `unreachable` | service stopped, or a named instance with SQL Browser off: add it with its port, `--host "localhost,50123"` |
| Instance shows `needs_auth` | your Windows account has no login there; add it with SQL auth via `add_server` |
| Certificate errors on a server with a real cert | `add-server ... --verify-certificate` |
| Config problem | `sqlscout doctor` prints drivers, config paths and local candidates |

## Development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

Tests need no database. MIT licensed.
