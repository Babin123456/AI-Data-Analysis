"""Whitelist-based SQL validation and SQL identifier escaping.

Only read-only analytical queries are allowed. Anything that could
mutate data or schema, access external files, read environment variables,
or call dangerous DuckDB functions is rejected before reaching the database.
"""
from __future__ import annotations

import re

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import DDL, DML

ALLOWED_STATEMENT_TYPES = {"SELECT"}
ALLOWED_LEADING_KEYWORDS = {"SELECT", "WITH"}

FORBIDDEN_KEYWORDS = {
    # DDL / DML mutations
    "DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "CREATE",
    "TRUNCATE", "ATTACH", "DETACH", "COPY", "PRAGMA", "EXPORT",
    "IMPORT", "INSTALL", "LOAD", "CALL", "GRANT", "REVOKE",
    "VACUUM", "REPLACE", "CHECKPOINT", "EXECUTE", "PREPARE", "DEALLOCATE",
    # DuckDB file & external scans (prevent LFI / arbitrary file read)
    "READ_CSV", "READ_CSV_AUTO", "READ_PARQUET", "READ_JSON", "READ_JSON_AUTO",
    "READ_TEXT", "READ_BLOB", "SCAN_CSV", "SCAN_PARQUET", "GLOB",
    "SQLITE_SCAN", "POSTGRES_SCAN", "MYSQL_SCAN", "ICEBERG_SCAN", "DELTA_SCAN",
    # Environment variables & system info (prevent secret leakage)
    "GETENV", "CURRENT_SETTING", "SET_VARIABLE", "SYSTEM",
    "QUERY", "QUERY_TABLE", "SQL",
}

# Extra defense against stacked/chained statements.
_MULTI_STATEMENT_PATTERN = re.compile(r";\s*\S")


class SQLValidationError(Exception):
    pass


def escape_sql_identifier(identifier: str) -> str:
    """Safely escape a SQL identifier (table or column name) with double quotes.

    Escapes existing double quotes by doubling them according to SQL standards.
    """
    if not identifier:
        return '""'
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def validate_sql(sql: str) -> str:
    """Validate a generated SQL string. Returns the cleaned SQL or raises SQLValidationError."""
    if not sql or not sql.strip():
        raise SQLValidationError("Generated SQL is empty.")

    cleaned = sql.strip().rstrip(";")

    if _MULTI_STATEMENT_PATTERN.search(sql):
        raise SQLValidationError("Multiple SQL statements are not allowed.")

    # Strip comments to prevent comment-based filter bypasses
    cleaned_no_comments = sqlparse.format(cleaned, strip_comments=True).strip()
    if not cleaned_no_comments:
        raise SQLValidationError("SQL statement is empty after removing comments.")

    parsed = sqlparse.parse(cleaned_no_comments)
    if len(parsed) != 1:
        raise SQLValidationError("Exactly one SQL statement is required.")

    statement: Statement = parsed[0]
    stmt_type = statement.get_type()

    first_keyword = _first_keyword(statement)
    if first_keyword not in ALLOWED_LEADING_KEYWORDS:
        raise SQLValidationError(
            f"Only SELECT / WITH statements are allowed (got: {first_keyword})."
        )

    if stmt_type not in ALLOWED_STATEMENT_TYPES and first_keyword != "WITH":
        raise SQLValidationError(f"Statement type '{stmt_type}' is not allowed.")

    upper_sql = cleaned_no_comments.upper()
    for forbidden in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{forbidden}\b", upper_sql):
            raise SQLValidationError(f"Forbidden keyword or function detected: {forbidden}")

    return cleaned


def _first_keyword(statement: Statement) -> str:
    for token in statement.tokens:
        if token.is_whitespace:
            continue
        if token.ttype in (DML, DDL) or token.ttype is not None:
            return token.value.strip().upper()
        return token.value.strip().upper()
    return ""
