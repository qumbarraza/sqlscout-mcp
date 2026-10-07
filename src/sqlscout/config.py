"""Server catalog configuration.

Two optional JSON files, merged (project wins over user):

  user     %USERPROFILE%\\.sqlscout\\config.json      (override dir with SQLSCOUT_HOME)
  project  .sqlscout.json in the working dir or any parent

Shape:

  {
    "servers": {
      "prod-ro": {
        "host": "sql01.example.com,1433",
        "auth": "sql",
        "user": "readonly",
        "password_ref": "env:PROD_RO_PASSWORD",
        "databases": ["Sales", "Inventory"],
        "env": "prod",
        "notes": "reporting replica"
      },
      "other": {
        "auth": "connection_string",
        "connection_string": "Server=h;UID=u;PWD=${OTHER_PWD};Database=d"
      }
    },
    "aliases": {
      "sales": {"server": "prod-ro", "database": "Sales", "notes": "short name"}
    }
  }

Secrets are never stored in these files: use `password_ref` ("env:NAME" or
"keyring") or `${NAME}` placeholders inside a connection string.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

AUTH_MODES = ("windows", "sql", "connection_string")
SOURCES = ("user", "project", "discovered")

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_LITERAL_SECRET_RE = re.compile(r"(?i)\b(pwd|password)\s*=\s*(?!\$\{)")


def home_dir() -> Path:
    override = os.environ.get("SQLSCOUT_HOME")
    return Path(override) if override else Path.home() / ".sqlscout"


def user_config_path() -> Path:
    return home_dir() / "config.json"


def project_config_path(start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        candidate = d / ".sqlscout.json"
        if candidate.is_file():
            return candidate
    return None


@dataclass
class Server:
    name: str
    host: str = ""
    auth: str = "windows"            # windows | sql | connection_string
    user: str = ""
    password_ref: str = ""           # "env:VAR" | "keyring"
    connection_string: str = ""      # may contain ${ENV_VAR} placeholders
    databases: list[str] | str = "*"  # "*" = enumerate; list = allowlist
    env: str = ""
    notes: str = ""
    trust_server_certificate: bool = True
    source: str = "user"

    def validate(self) -> None:
        if self.source != "discovered" and not _NAME_RE.fullmatch(self.name):
            raise ConfigError(
                f"invalid server name '{self.name}': use letters, digits, '_', '-', '.'"
            )
        if self.auth not in AUTH_MODES:
            raise ConfigError(f"server '{self.name}': auth must be one of {AUTH_MODES}")
        if self.auth in ("windows", "sql") and not self.host:
            raise ConfigError(f"server '{self.name}': 'host' is required for auth={self.auth}")
        if self.auth == "sql":
            if not self.user:
                raise ConfigError(f"server '{self.name}': 'user' is required for auth=sql")
            if not (self.password_ref == "keyring" or re.fullmatch(r"env:[A-Za-z_]\w*", self.password_ref)):
                raise ConfigError(
                    f"server '{self.name}': password_ref must be 'env:VAR_NAME' or 'keyring' "
                    "(passwords are never stored in config)"
                )
        if self.auth == "connection_string":
            if not self.connection_string:
                raise ConfigError(f"server '{self.name}': 'connection_string' is required")
            if _LITERAL_SECRET_RE.search(self.connection_string):
                raise ConfigError(
                    f"server '{self.name}': connection string contains a literal password. "
                    "Use a placeholder such as PWD=${MY_ENV_VAR} and set that environment variable."
                )
        if self.databases != "*" and not (
            isinstance(self.databases, list) and all(isinstance(d, str) and d for d in self.databases)
        ):
            raise ConfigError(f"server '{self.name}': 'databases' must be \"*\" or a list of names")

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"auth": self.auth}
        for key in ("host", "user", "password_ref", "connection_string", "env", "notes"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.databases != "*":
            out["databases"] = self.databases
        if not self.trust_server_certificate:
            out["trust_server_certificate"] = False
        return out

    @classmethod
    def from_json(cls, name: str, data: dict[str, Any], source: str) -> "Server":
        if not isinstance(data, dict):
            raise ConfigError(f"server '{name}' must be an object")
        known = {"host", "auth", "user", "password_ref", "connection_string",
                 "databases", "env", "notes", "trust_server_certificate"}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"server '{name}': unknown field(s) {sorted(unknown)}")
        server = cls(
            name=name,
            host=data.get("host", ""),
            auth=data.get("auth", "windows"),
            user=data.get("user", ""),
            password_ref=data.get("password_ref", ""),
            connection_string=data.get("connection_string", ""),
            databases=data.get("databases", "*"),
            env=data.get("env", ""),
            notes=data.get("notes", ""),
            trust_server_certificate=bool(data.get("trust_server_certificate", True)),
            source=source,
        )
        server.validate()
        return server


@dataclass
class Alias:
    key: str
    server: str
    database: str
    notes: str = ""
    source: str = "user"


@dataclass
class Config:
    servers: dict[str, Server] = field(default_factory=dict)
    aliases: dict[str, Alias] = field(default_factory=dict)
    loaded_from: list[Path] = field(default_factory=list)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON ({e})") from None
    except OSError as e:
        raise ConfigError(f"{path}: cannot read ({e})") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be an object")
    return data


def _merge_file(cfg: Config, path: Path, source: str) -> None:
    data = _read_json(path)
    for name, raw in (data.get("servers") or {}).items():
        cfg.servers[name] = Server.from_json(name, raw, source)
    for key, raw in (data.get("aliases") or {}).items():
        if not isinstance(raw, dict) or "server" not in raw or "database" not in raw:
            raise ConfigError(f"{path}: alias '{key}' needs 'server' and 'database'")
        cfg.aliases[key] = Alias(key, raw["server"], raw["database"], raw.get("notes", ""), source)
    cfg.loaded_from.append(path)


def load_config(cwd: Path | None = None) -> Config:
    cfg = Config()
    upath = user_config_path()
    if upath.is_file():
        _merge_file(cfg, upath, "user")
    ppath = project_config_path(cwd)
    if ppath is not None and ppath != upath:
        _merge_file(cfg, ppath, "project")
    return cfg


# ---- user-config mutation (only the user file is ever written) ---------------

def _write_user_json(data: dict[str, Any]) -> None:
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_user_json() -> dict[str, Any]:
    path = user_config_path()
    return _read_json(path) if path.is_file() else {}


def save_user_server(server: Server) -> Path:
    server.validate()
    data = _read_user_json()
    data.setdefault("servers", {})[server.name] = server.to_json()
    _write_user_json(data)
    return user_config_path()


def remove_user_server(name: str) -> bool:
    data = _read_user_json()
    servers = data.get("servers") or {}
    if name not in servers:
        return False
    del servers[name]
    _write_user_json(data)
    return True
