import pytest

from sqlscout.errors import ReadOnlyViolation
from sqlscout.guard import assert_read_only

ACCEPT = [
    "SELECT 1",
    "SELECT 1;",
    "SELECT TOP 10 * FROM dbo.Users",
    "SELECT u.Id, p.Name FROM dbo.Users u INNER JOIN dbo.Profiles p ON p.UserId = u.Id ORDER BY u.Id DESC",
    "WITH active AS (SELECT Id FROM dbo.Users WHERE IsActive = 1) SELECT * FROM active",
    "SELECT 1 UNION ALL SELECT 2",
    "(SELECT 1)",
    "SELECT * FROM (SELECT TOP 5 Id FROM dbo.Users) x",
    "-- inspect\nSELECT TOP 1 * FROM dbo.Users",
    "/* inspect */ SELECT TOP 1 * FROM dbo.Users",
    "SELECT insert_date FROM dbo.AuditLog",
    "SELECT 1 WHERE EXISTS (SELECT 1 FROM dbo.Users WHERE Id = 1)",
    "SELECT REPLACE (Name, 'a', 'b') FROM dbo.Users",
    "SELECT * FROM dbo.Users WITH (NOLOCK)",
    "SELECT 'DROP TABLE dbo.Users; DELETE FROM x' AS note",
    "SELECT 'OPENROWSET' AS word",
    "SELECT name FROM sys.databases WHERE database_id > 4 AND state = 0 "
    "AND HAS_DBACCESS(name) = 1 ORDER BY name",
    "SELECT @@VERSION AS version, SUSER_SNAME() AS login_name, "
    "IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin",
]

REJECT = [
    ("", "empty"),
    ("   \n\t ", "empty"),
    ("INSERT INTO dbo.Users (Id) VALUES (1)", "INSERT"),
    ("UPDATE dbo.Users SET IsActive = 0", "UPDATE"),
    ("DELETE FROM dbo.Users WHERE Id = 1", "DELETE"),
    ("MERGE INTO dbo.Users AS t USING (SELECT 1 AS Id) AS s ON t.Id = s.Id "
     "WHEN MATCHED THEN UPDATE SET t.IsActive = 0;", "MERGE"),
    ("DROP TABLE dbo.Users", "DROP"),
    ("CREATE TABLE dbo.X (Id INT)", "CREATE"),
    ("ALTER TABLE dbo.Users ADD Col2 INT", "ALTER"),
    ("TRUNCATE TABLE dbo.Users", "TRUNCATE"),
    ("EXEC dbo.MyProc", "EXEC"),
    ("EXECUTE dbo.MyProc", "EXECUTE"),
    ("GRANT SELECT ON dbo.Users TO some_user", "GRANT"),
    ("REVOKE SELECT ON dbo.Users FROM some_user", "REVOKE"),
    ("USE master", "must start with"),
    ("DBCC CHECKDB", "must start with"),
    ("BACKUP DATABASE x TO DISK = 'x'", "must start with"),
    ("KILL 53", "must start with"),
    ("SELECT 1; SELECT 2", "one statement"),
    ("SELECT 1; DROP TABLE dbo.Users", "one statement"),
    ("SELECT * INTO dbo.NewTable FROM dbo.Users", "INTO"),
    ("SELECT * INTO #tmp FROM dbo.Users", "INTO"),
    ("WITH x AS (SELECT 1 AS a) DELETE FROM dbo.Users", "DELETE"),
    ("SELECT * FROM OPENROWSET('SQLNCLI', 'Server=x;', 'SELECT 1')", "OPENROWSET"),
    ("SELECT * FROM OPENQUERY(linked, 'SELECT 1')", "OPENQUERY"),
    ("SELECT * FROM OPENDATASOURCE('SQLNCLI','Data Source=x').db.dbo.t", "OPENDATASOURCE"),
    ("SELECT NEXT VALUE FOR dbo.MySeq", "NEXT VALUE FOR"),
]


@pytest.mark.parametrize("sql", ACCEPT)
def test_accepts(sql):
    assert_read_only(sql)


@pytest.mark.parametrize("sql,expected", REJECT)
def test_rejects(sql, expected):
    with pytest.raises(ReadOnlyViolation) as exc:
        assert_read_only(sql)
    assert expected.lower() in str(exc.value).lower()
