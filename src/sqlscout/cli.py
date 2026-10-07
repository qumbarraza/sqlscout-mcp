"""`sqlscout` command line. With no arguments (or `serve`) it runs the MCP server."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any, Sequence

from . import __version__
from .config import (
    Server, remove_user_server, save_user_server, user_config_path,
)
from .connection import (
    KEYRING_SERVICE, check_server, installed_drivers, pick_driver, run_query,
)
from .errors import DriverNotFound, SqlscoutError


def _print_table(rows: list[dict[str, Any]], columns: Sequence[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(columns, widths)))


def _catalog():
    from .catalog import Catalog
    return Catalog()


def cmd_serve(_args: argparse.Namespace) -> int:
    from .server import main as serve
    serve()
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    print(f"sqlscout {__version__}")
    print(f"ODBC drivers: {installed_drivers() or 'none'}")
    try:
        print(f"selected driver: {pick_driver()}")
    except DriverNotFound as e:
        print(f"selected driver: NONE\n  {e}")
    print(f"user config: {user_config_path()} (exists={user_config_path().is_file()})")
    from .discovery import candidates
    found = candidates()
    print(f"local instance candidates: {found or 'none'}")
    return 0


def cmd_discover(_args: argparse.Namespace) -> int:
    cat = _catalog()
    _print_table(cat.list_servers(), ["name", "host", "auth", "source", "status", "databases", "error"])
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    result = _catalog().list_databases()
    _print_table(result["databases"], ["key", "source", "env", "notes"])
    for u in result["servers_unavailable"]:
        print(f"! {u['server']}: {u['status']} {u['error']}", file=sys.stderr)
    return 0


def cmd_add_server(args: argparse.Namespace) -> int:
    auth = "connection_string" if args.connection_string else ("sql" if args.user else "windows")
    password_ref = ""
    if auth == "sql":
        if args.password_env:
            password_ref = f"env:{args.password_env}"
        elif args.prompt_password:
            import keyring
            password = getpass.getpass(f"Password for {args.user}@{args.host}: ")
            keyring.set_password(KEYRING_SERVICE, f"{args.name}:{args.user}", password)
            password_ref = "keyring"
        else:
            print("auth=sql needs --password-env NAME or --prompt-password", file=sys.stderr)
            return 2
    dbs: list[str] | str = "*" if args.databases in ("", "*") else \
        [d.strip() for d in args.databases.split(",") if d.strip()]
    server = Server(
        name=args.name, host=args.host or "", auth=auth, user=args.user or "",
        password_ref=password_ref, connection_string=args.connection_string or "",
        databases=dbs, env=args.env, notes=args.notes,
        trust_server_certificate=not args.verify_certificate, source="user",
    )
    path = save_user_server(server)
    print(f"saved '{args.name}' to {path}")
    if args.no_test:
        return 0
    try:
        result = check_server(server)
    except SqlscoutError as e:
        print(f"saved, but connection test failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_remove_server(args: argparse.Namespace) -> int:
    if remove_user_server(args.name):
        print(f"removed '{args.name}'")
        return 0
    print(f"'{args.name}' not found in user config", file=sys.stderr)
    return 1


def cmd_test(args: argparse.Namespace) -> int:
    server = _catalog().servers().get(args.name)
    if server is None:
        print(f"unknown server '{args.name}'", file=sys.stderr)
        return 1
    print(json.dumps(check_server(server, args.database), indent=2))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    server, database = _catalog().resolve(args.db)
    cols, rows, truncated = run_query(server, database, args.sql)
    _print_table([dict(zip(cols, r)) for r in rows], cols)
    if truncated:
        print("(truncated)", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sqlscout", description="Read-only SQL Server MCP server.")
    p.add_argument("--version", action="version", version=f"sqlscout {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("serve", help="run the MCP server over stdio (default)").set_defaults(fn=cmd_serve)
    sub.add_parser("doctor", help="check drivers, config and local instances").set_defaults(fn=cmd_doctor)
    sub.add_parser("discover", help="scan this machine and list servers").set_defaults(fn=cmd_discover)
    sub.add_parser("list", help="list database keys").set_defaults(fn=cmd_list)

    a = sub.add_parser("add-server", help="add a server to your user config")
    a.add_argument("name")
    a.add_argument("--host", help="e.g. sql01, sql01\\INST, 10.0.0.5,1433")
    a.add_argument("--user", help="SQL login (omit for Windows auth)")
    a.add_argument("--password-env", help="name of env var holding the password")
    a.add_argument("--prompt-password", action="store_true",
                   help="prompt and store the password in Windows Credential Manager")
    a.add_argument("--connection-string", help="full ODBC string; use ${ENV_VAR} for secrets")
    a.add_argument("--databases", default="*", help='"*" or comma-separated allowlist')
    a.add_argument("--env", default="", help="label, e.g. dev/test/prod")
    a.add_argument("--notes", default="")
    a.add_argument("--verify-certificate", action="store_true",
                   help="do not trust the server certificate (default trusts it)")
    a.add_argument("--no-test", action="store_true", help="skip the connection test")
    a.set_defaults(fn=cmd_add_server)

    r = sub.add_parser("remove-server", help="remove a server from your user config")
    r.add_argument("name")
    r.set_defaults(fn=cmd_remove_server)

    t = sub.add_parser("test", help="test a server connection")
    t.add_argument("name")
    t.add_argument("--database")
    t.set_defaults(fn=cmd_test)

    q = sub.add_parser("query", help="run a read-only SELECT")
    q.add_argument("db", help="key from `sqlscout list`")
    q.add_argument("sql")
    q.set_defaults(fn=cmd_query)
    return p


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    fn = getattr(args, "fn", cmd_serve)
    try:
        code = fn(args)
    except SqlscoutError as e:
        print(f"error: {e}", file=sys.stderr)
        code = 1
    sys.exit(code)
