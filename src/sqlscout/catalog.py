"""Unified view of configured, project-level and auto-discovered servers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config, Server, load_config
from .discovery import DiscoveryResult, discover, enumerate_databases
from .errors import CatalogError, SqlscoutError


class Catalog:
    def __init__(self, cwd: Path | None = None) -> None:
        self._cwd = cwd
        self.config: Config = load_config(cwd)
        self._discovered: list[DiscoveryResult] | None = None
        self._enumerated: dict[str, tuple[list[str], str]] = {}  # name -> (dbs, error)

    # ---- state ---------------------------------------------------------

    def reload_config(self) -> None:
        """Re-read config files (cheap); keeps discovery cache."""
        self.config = load_config(self._cwd)

    def refresh(self) -> None:
        self.reload_config()
        self._discovered = None
        self._enumerated.clear()

    def discovered(self) -> list[DiscoveryResult]:
        if self._discovered is None:
            self._discovered = discover()
        return self._discovered

    def servers(self, *, include_discovered: bool = True) -> dict[str, Server]:
        out: dict[str, Server] = {}
        if include_discovered:
            out.update({r.server.name: r.server for r in self.discovered()})
        out.update(self.config.servers)
        return out

    # ---- listings ------------------------------------------------------

    def _databases_of(self, server: Server) -> tuple[list[str], str]:
        if isinstance(server.databases, list):
            return server.databases, ""
        if server.name not in self._enumerated:
            try:
                self._enumerated[server.name] = (enumerate_databases(server), "")
            except SqlscoutError as e:
                self._enumerated[server.name] = ([], str(e))
        return self._enumerated[server.name]

    def list_servers(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for res in self.discovered():
            rows.append({
                "name": res.server.name, "host": res.server.host, "auth": "windows",
                "source": "discovered", "status": res.status,
                "databases": len(res.databases), "error": res.error,
            })
        for s in self.config.servers.values():
            rows.append({
                "name": s.name, "host": s.host or "(connection string)", "auth": s.auth,
                "source": s.source, "env": s.env, "notes": s.notes,
                "databases": s.databases if isinstance(s.databases, list) else "*",
            })
        return rows

    def list_databases(self) -> dict[str, Any]:
        databases: list[dict[str, Any]] = []
        unavailable: list[dict[str, str]] = []

        for res in self.discovered():
            if res.status != "ok":
                unavailable.append(
                    {"server": res.server.name, "status": res.status, "error": res.error}
                )
                continue
            for db in res.databases:
                databases.append({
                    "key": f"{res.server.name}/{db}", "server": res.server.name,
                    "database": db, "source": "discovered", "env": "local",
                    "notes": res.server.notes,
                })

        for s in self.config.servers.values():
            dbs, error = self._databases_of(s)
            if error:
                unavailable.append({"server": s.name, "status": "error", "error": error})
            for db in dbs:
                databases.append({
                    "key": f"{s.name}/{db}", "server": s.name, "database": db,
                    "source": s.source, "env": s.env, "notes": s.notes,
                })

        for a in self.config.aliases.values():
            databases.append({
                "key": a.key, "server": a.server, "database": a.database,
                "source": f"{a.source}-alias", "env": "", "notes": a.notes,
            })
        return {"databases": databases, "servers_unavailable": unavailable}

    # ---- resolution ----------------------------------------------------

    def resolve(self, key: str) -> tuple[Server, str]:
        """Map a database key (`server/db` or an alias) to (server, database)."""
        self.reload_config()
        alias = self.config.aliases.get(key)
        if alias is not None:
            server = self._server(alias.server)
            return server, alias.database

        server_name, sep, database = key.rpartition("/")
        if not sep or not server_name or not database:
            raise CatalogError(
                f"unknown database key '{key}'. Use list_databases to see valid keys "
                "(format: <server>/<database> or an alias)."
            )
        server = self._server(server_name)
        if server.source != "discovered" and isinstance(server.databases, list) \
                and database not in server.databases:
            raise CatalogError(
                f"database '{database}' is not in the allowlist for server '{server_name}': "
                f"{server.databases}"
            )
        return server, database

    def _server(self, name: str) -> Server:
        if name in self.config.servers:
            return self.config.servers[name]
        if name.startswith("local/"):
            for res in self.discovered():
                if res.server.name == name:
                    return res.server
        raise CatalogError(
            f"unknown server '{name}'. Use list_servers to see configured and discovered servers."
        )
