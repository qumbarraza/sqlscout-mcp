"""Exception types. All carry messages that are safe to show to the model/user."""


class SqlscoutError(Exception):
    """Base class for expected, user-facing errors."""


class ReadOnlyViolation(SqlscoutError, ValueError):
    """A query failed the read-only guard."""


class ConfigError(SqlscoutError):
    """Config file unreadable or invalid."""


class CatalogError(SqlscoutError):
    """Unknown server/database key."""


class DriverNotFound(SqlscoutError):
    """No usable SQL Server ODBC driver is installed."""


class ConnectionFailed(SqlscoutError):
    """Could not connect to, or query, a server. Message is credential-redacted."""

    def __init__(self, message: str, sqlstate: str = "") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate

    @property
    def is_auth_failure(self) -> bool:
        return self.sqlstate in {"28000", "28P01"} or "login failed" in str(self).lower()
