import sys
from pathlib import Path

# Add backend directory dynamically to sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from app.validation.sql_validator import (
    SQLValidationError,
    validate_sql,
    escape_sql_identifier,
)
from app.validation.nosql_validator import (
    NoSQLValidationError,
    sanitize_regex_query,
    check_nosql_injection,
)


def assert_raises(exc_type, func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except exc_type:
        return
    except Exception as e:
        raise AssertionError(f"Expected {exc_type.__name__}, got {type(e).__name__}: {e}")
    raise AssertionError(f"Expected {exc_type.__name__}, but no exception was raised.")


def test_valid_sql_queries():
    assert validate_sql("SELECT * FROM uploaded_data") == "SELECT * FROM uploaded_data"
    assert validate_sql("WITH cte AS (SELECT id FROM t) SELECT * FROM cte") == "WITH cte AS (SELECT id FROM t) SELECT * FROM cte"
    assert validate_sql("SELECT count(*), avg(salary) FROM emp GROUP BY dept") == "SELECT count(*), avg(salary) FROM emp GROUP BY dept"


def test_forbidden_sql_mutations():
    forbidden_queries = [
        "DROP TABLE uploaded_data",
        "DELETE FROM uploaded_data WHERE 1=1",
        "INSERT INTO uploaded_data VALUES (1, 2)",
        "UPDATE uploaded_data SET col = 1",
        "ALTER TABLE uploaded_data ADD COLUMN hack INT",
        "CREATE TABLE hack AS SELECT 1",
        "TRUNCATE uploaded_data",
        "ATTACH 'evil.db' AS evil",
        "DETACH evil",
        "COPY uploaded_data TO 'data.csv'",
        "PRAGMA database_list",
        "INSTALL httpfs",
        "LOAD httpfs",
        "GRANT ALL PRIVILEGES ON uploaded_data TO public",
    ]
    for q in forbidden_queries:
        assert_raises(SQLValidationError, validate_sql, q)


def test_forbidden_duckdb_file_and_secret_functions():
    lfi_and_secret_queries = [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_csv_auto('C:/Windows/win.ini')",
        "SELECT * FROM read_parquet('s3://secret/data.parquet')",
        "SELECT * FROM read_json('secrets.json')",
        "SELECT * FROM read_text('/root/.ssh/id_rsa')",
        "SELECT * FROM glob('../*')",
        "SELECT getenv('GROQ_API_KEY')",
        "SELECT getenv('MONGODB_URI')",
        "SELECT current_setting('access_mode')",
        "SELECT * FROM sqlite_scan('db.sqlite', 'table')",
    ]
    for q in lfi_and_secret_queries:
        assert_raises(SQLValidationError, validate_sql, q)


def test_stacked_statements_and_comments():
    stacked_queries = [
        "SELECT 1; DROP TABLE uploaded_data",
        "SELECT 1;\nDELETE FROM uploaded_data",
        "SELECT 1; -- comment\n DROP TABLE uploaded_data",
        "SELECT getenv/*comment*/('KEY')",
    ]
    for q in stacked_queries:
        assert_raises(SQLValidationError, validate_sql, q)


def test_escape_sql_identifier():
    assert escape_sql_identifier("users") == '"users"'
    assert escape_sql_identifier('col"name') == '"col""name"'
    assert escape_sql_identifier('table" --') == '"table"" --"'
    assert escape_sql_identifier("") == '""'


def test_nosql_regex_sanitization():
    raw_pattern = "user.*+?^${}()|[]\\"
    safe = sanitize_regex_query(raw_pattern)
    assert "\\." in safe
    assert "\\*" in safe
    assert "\\+" in safe
    assert "\\?" in safe
    assert "\\^" in safe
    assert "\\$" in safe


def test_nosql_operator_check():
    # Clean dict should pass
    check_nosql_injection({"username": "alice", "email": "alice@example.com"})

    # Dict with operator key should raise NoSQLValidationError
    assert_raises(NoSQLValidationError, check_nosql_injection, {"username": {"$gt": ""}})
    assert_raises(NoSQLValidationError, check_nosql_injection, {"$where": "this.password == 'secret'"})


if __name__ == "__main__":
    test_valid_sql_queries()
    test_forbidden_sql_mutations()
    test_forbidden_duckdb_file_and_secret_functions()
    test_stacked_statements_and_comments()
    test_escape_sql_identifier()
    test_nosql_regex_sanitization()
    test_nosql_operator_check()
    print("ALL INJECTION PREVENTION TESTS PASSED SUCCESSFULLY!")
