"""Find SQL Server instances on this machine (Windows) and list their databases.

Sources, merged and de-duplicated by label:
  1. Installed instances      registry  HKLM\\SOFTWARE\\Microsoft\\Microsoft SQL Server\\Instance Names\\SQL
  2. LocalDB instances        `sqllocaldb i`
  3. SQL Browser (UDP 1434)  named instances and their TCP ports (covers Docker/dev installs)
  4. Default TCP port        localhost,1433 when no default instance was found

Each candidate is probed with Windows authentication. Probes run in parallel
with short timeouts.
"""

from __future__ import annotations

import glob
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .config import Server
from .connection import run_query
from .errors import ConnectionFailed, SqlscoutError

_USER_DATABASES_SQL = (
    "SELECT name FROM sys.databases "
    "WHERE database_id > 4 AND state = 0 AND HAS_DBACCESS(name) = 1 "
    "ORDER BY name"
)


@dataclass
class DiscoveryResult:
    server: Server
    status: str                       # ok | needs_auth | unreachable
    databases: list[str] = field(default_factory=list)
    error: str = ""


# ---- candidate sources -----------------------------------------------------

def _registry_instances() -> dict[str, str]:
    if sys.platform != "win32":
        return {}
    import winreg

    found: dict[str, str] = {}
    path = r"SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL"
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name, _value, _type = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if name.upper() == "MSSQLSERVER":
                    found.setdefault("default", "localhost")
                else:
                    found.setdefault(name, f"localhost\\{name}")
    return found


def _sqllocaldb_exe() -> str | None:
    for exe in ("sqllocaldb", *glob.glob(
        r"C:\Program Files\Microsoft SQL Server\*\Tools\Binn\SqlLocalDB.exe"
    )):
        try:
            subprocess.run([exe, "v"], capture_output=True, timeout=5, check=True)
            return exe
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _localdb_instances() -> dict[str, str]:
    exe = _sqllocaldb_exe()
    if exe is None:
        return {}
    try:
        out = subprocess.run([exe, "i"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return {
        f"localdb-{n.strip()}": f"(localdb)\\{n.strip()}"
        for n in out.splitlines() if n.strip()
    }


def _browser_instances(timeout: float = 0.6) -> dict[str, str]:
    """Ask the local SQL Browser service for instances and their TCP ports."""
    found: dict[str, str] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(b"\x03", ("127.0.0.1", 1434))
        data = sock.recv(65535)
    except OSError:
        return found
    finally:
        sock.close()
    text = data[3:].decode("ascii", errors="ignore")
    for record in filter(None, text.split(";;")):
        fields = record.split(";")
        info = dict(zip(fields[0::2], fields[1::2]))
        name = info.get("InstanceName")
        if not name:
            continue
        port = info.get("tcp")
        label = "default" if name.upper() == "MSSQLSERVER" else name
        found[label] = f"localhost,{port}" if port else f"localhost\\{name}"
    return found


def _tcp_1433_open() -> bool:
    try:
        socket.create_connection(("127.0.0.1", 1433), timeout=0.4).close()
        return True
    except OSError:
        return False


def candidates() -> dict[str, str]:
    """Return {label: host} for every locally reachable-looking instance."""
    found: dict[str, str] = {}
    found.update(_registry_instances())
    for label, host in {**_localdb_instances(), **_browser_instances()}.items():
        found.setdefault(label, host)
    if "default" not in found and _tcp_1433_open():
        found["tcp-1433"] = "localhost,1433"
    return found


# ---- probing ---------------------------------------------------------------

def _probe(label: str, host: str) -> DiscoveryResult:
    server = Server(
        name=f"local/{label}",
        host=host,
        auth="windows",
        env="local",
        notes=f"auto-discovered ({host})",
        source="discovered",
    )
    # LocalDB instances are started on demand by the first connection, which is slow.
    login_timeout = 25 if host.lower().startswith("(localdb)") else 4
    try:
        _cols, rows, _ = run_query(
            server, "master", _USER_DATABASES_SQL, login_timeout=login_timeout, query_timeout=10
        )
    except ConnectionFailed as e:
        status = "needs_auth" if e.is_auth_failure else "unreachable"
        return DiscoveryResult(server, status, error=str(e))
    except SqlscoutError as e:
        return DiscoveryResult(server, "unreachable", error=str(e))
    server.databases = [r[0] for r in rows]
    return DiscoveryResult(server, "ok", databases=list(server.databases))


def discover() -> list[DiscoveryResult]:
    found = candidates()
    if not found:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(found))) as pool:
        results = list(pool.map(lambda kv: _probe(*kv), sorted(found.items())))
    return results


def enumerate_databases(server: Server) -> list[str]:
    """List user databases visible to this server's login."""
    _cols, rows, _ = run_query(
        server, None, _USER_DATABASES_SQL, login_timeout=6, query_timeout=15
    )
    return [r[0] for r in rows]
