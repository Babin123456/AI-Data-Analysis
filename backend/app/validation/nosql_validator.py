"""NoSQL injection prevention and sanitization helpers for MongoDB queries."""
from __future__ import annotations

import re
from typing import Any


class NoSQLValidationError(Exception):
    """Raised when an untrusted input fails NoSQL safety validation."""
    pass


def sanitize_regex_query(search_term: str) -> str:
    """Escape regex special characters to prevent MongoDB regex injection and ReDoS."""
    if not search_term or not isinstance(search_term, str):
        return ""
    # Strip whitespace and escape special regex characters
    cleaned = search_term.strip()
    return re.escape(cleaned)


def check_nosql_injection(value: Any) -> None:
    """Recursively verify that untrusted input does not contain MongoDB operators ($)."""
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).startswith("$"):
                raise NoSQLValidationError(f"Forbidden NoSQL operator key detected: {k}")
            check_nosql_injection(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            check_nosql_injection(item)
