"""Client-side read-only guard.

Accepts exactly one SELECT / WITH ... SELECT statement. Rejects DML, DDL,
EXEC, admin statements, SELECT ... INTO, and a few SELECT-reachable escape
hatches (OPENROWSET/OPENQUERY/OPENDATASOURCE, NEXT VALUE FOR).

This is a soft guard: it protects against mistakes, not a determined attacker.
For a hard guarantee, connect with a login that only has db_datareader.
"""

from __future__ import annotations

import re

import sqlparse
from sqlparse.tokens import DDL, DML, Comment, Keyword, String

from .errors import ReadOnlyViolation

_FORBIDDEN = {
    # DML
    "INSERT", "UPDATE", "DELETE", "MERGE",
    # DDL
    "CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME",
    # permissions / execution / admin
    "GRANT", "REVOKE", "DENY", "EXEC", "EXECUTE",
    "BACKUP", "RESTORE", "BULK", "SHUTDOWN", "KILL",
    "CHECKPOINT", "RECONFIGURE", "DBCC", "USE",
    "WRITETEXT", "UPDATETEXT",
}

# Escape hatches that are valid inside a SELECT. Matched on text with string
# literals and comments removed.
_BLOCKED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bOPEN(ROWSET|QUERY|DATASOURCE)\b", re.I),
     "OPENROWSET/OPENQUERY/OPENDATASOURCE are not allowed (remote or ad-hoc access)"),
    (re.compile(r"\bNEXT\s+VALUE\s+FOR\b", re.I),
     "NEXT VALUE FOR is not allowed (advances a sequence)"),
]


def assert_read_only(sql: str) -> None:
    """Raise ReadOnlyViolation unless `sql` is a single SELECT/WITH statement."""
    if not sql or not sql.strip():
        raise ReadOnlyViolation("empty query")

    statements = [s for s in sqlparse.parse(sql) if str(s).strip()]
    if len(statements) != 1:
        raise ReadOnlyViolation(f"only one statement allowed; got {len(statements)}")

    stmt = statements[0]
    stype = (stmt.get_type() or "").upper()
    if stype not in {"SELECT", "UNKNOWN"}:
        raise ReadOnlyViolation(f"statement type '{stype}' not allowed")

    tokens = [
        t for t in stmt.flatten()
        if not t.is_whitespace and t.ttype not in Comment
    ]
    first = tokens[0] if tokens else None
    if first is None or first.normalized.upper() not in {"SELECT", "WITH", "("}:
        raise ReadOnlyViolation(f"query must start with SELECT or WITH (got '{first}')")

    saw_select = False
    scrubbed: list[str] = []
    for tok in tokens:
        if tok.ttype in String:
            continue
        scrubbed.append(tok.value)
        if tok.ttype in Keyword or tok.ttype in DML or tok.ttype in DDL:
            upper = tok.normalized.upper()
            if upper in _FORBIDDEN:
                raise ReadOnlyViolation(f"forbidden keyword: {upper}")
            if upper == "SELECT":
                saw_select = True
            elif upper == "INTO" and saw_select:
                raise ReadOnlyViolation("SELECT ... INTO is not allowed (creates a new table)")

    text = " ".join(scrubbed)
    for pattern, message in _BLOCKED_PATTERNS:
        if pattern.search(text):
            raise ReadOnlyViolation(message)
