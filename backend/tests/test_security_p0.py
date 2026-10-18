"""Comprehensive regression test suite for P0 security vulnerabilities:
1. IDOR and Dataset Ownership Authorization.
2. Strict Table Access Whitelisting in Download and SQL Editor.
3. Bounded Result Materialization and Resource Limits in SQL Editor.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from app.db.duckdb_manager import duckdb_manager
from app.main import app
from app.utils.auth import get_current_user
from app.utils.dataset_auth import record_dataset_ownership
from app.validation.sql_validator import SQLValidationError, validate_sql

OWNER_USER = {"id": "owner_123", "username": "alice", "email": "alice@example.com", "role": "user"}
OTHER_USER = {"id": "attacker_456", "username": "bob", "email": "bob@example.com", "role": "user"}

DATASET_ID = "dataset_sec_test_001"
TABLE_NAME = "financial_records"


@pytest.fixture(autouse=True)
def setup_dataset():
    # Setup test dataset
    duckdb_manager.drop_connection(DATASET_ID)
    from pathlib import Path
    from app.config import settings
    upload_dir = Path(settings.upload_dir)
    (upload_dir / f"{DATASET_ID}.duckdb").unlink(missing_ok=True)
    (upload_dir / f"{DATASET_ID}.duckdb.wal").unlink(missing_ok=True)

    conn = duckdb_manager.create_connection(DATASET_ID, table_name=TABLE_NAME, read_only=False)
    conn.execute(f"""
        CREATE OR REPLACE TABLE {TABLE_NAME} AS
        SELECT range AS id, 'item_' || range AS name, range * 10.5 AS amount
        FROM range(1000)
    """)
    # Assign ownership to OWNER_USER
    record_dataset_ownership(DATASET_ID, OWNER_USER, filenames=["finance.csv"], table_names=[TABLE_NAME])
    yield
    duckdb_manager.drop_connection(DATASET_ID)
    (upload_dir / f"{DATASET_ID}.duckdb").unlink(missing_ok=True)
    (upload_dir / f"{DATASET_ID}.duckdb.wal").unlink(missing_ok=True)
    (upload_dir / f"{DATASET_ID}.meta.json").unlink(missing_ok=True)


# ─── P0: IDOR & Dataset Ownership Authorization Tests ────────────────────────

def test_owner_can_access_schema():
    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    res = client.get(f"/api/dataset/{DATASET_ID}/schema")
    assert res.status_code == 200
    data = res.json()
    assert data["table_name"] == TABLE_NAME


def test_non_owner_cannot_access_schema():
    app.dependency_overrides[get_current_user] = lambda: OTHER_USER
    client = TestClient(app)
    res = client.get(f"/api/dataset/{DATASET_ID}/schema")
    assert res.status_code == 404
    assert res.json()["detail"] == "Dataset not found or session expired."


def test_owner_can_access_preview():
    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    res = client.get(f"/api/dataset/{DATASET_ID}/preview")
    assert res.status_code == 200
    data = res.json()
    assert TABLE_NAME in data
    assert len(data[TABLE_NAME]) == 100


def test_non_owner_cannot_access_preview():
    app.dependency_overrides[get_current_user] = lambda: OTHER_USER
    client = TestClient(app)
    res = client.get(f"/api/dataset/{DATASET_ID}/preview")
    assert res.status_code == 404


def test_unauthenticated_request_rejected():
    app.dependency_overrides.pop(get_current_user, None)
    client = TestClient(app)
    res = client.get(f"/api/dataset/{DATASET_ID}/schema")
    assert res.status_code in (401, 403)


def test_legacy_unowned_dataset_fails_closed():
    unowned_id = "legacy_unowned_999"
    conn = duckdb_manager.create_connection(unowned_id, table_name="legacy_data", read_only=False)
    conn.execute("CREATE OR REPLACE TABLE legacy_data (col INT)")

    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    res = client.get(f"/api/dataset/{unowned_id}/schema")
    # Must fail closed with 404
    assert res.status_code == 404

    duckdb_manager.drop_connection(unowned_id)
    from pathlib import Path
    from app.config import settings
    upload_dir = Path(settings.upload_dir)
    (upload_dir / f"{unowned_id}.duckdb").unlink(missing_ok=True)
    (upload_dir / f"{unowned_id}.meta.json").unlink(missing_ok=True)


def test_non_owner_cannot_delete_dataset():
    app.dependency_overrides[get_current_user] = lambda: OTHER_USER
    client = TestClient(app)
    res = client.delete(f"/api/dataset/{DATASET_ID}")
    assert res.status_code == 404
    # Dataset must still exist
    assert duckdb_manager.exists(DATASET_ID)


def test_owner_can_delete_dataset():
    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    res = client.delete(f"/api/dataset/{DATASET_ID}")
    assert res.status_code == 200
    assert not duckdb_manager.exists(DATASET_ID)


def test_non_owner_cannot_append_files_to_foreign_dataset():
    app.dependency_overrides[get_current_user] = lambda: OTHER_USER
    client = TestClient(app)
    file_content = b"col1,col2\n1,2"
    res = client.post(
        "/api/upload",
        files=[("files", ("hacked.csv", file_content, "text/csv"))],
        data={"dataset_id": DATASET_ID},
    )
    # Must be rejected with 404
    assert res.status_code == 404


# ─── P0: Allowed Tables & Strict SQL Validation Tests ────────────────────────

def test_sql_editor_authorized_table_success():
    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    res = client.post(
        "/api/sql-editor",
        json={"dataset_id": DATASET_ID, "sql": f"SELECT * FROM {TABLE_NAME} WHERE id < 5"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["error"] is None
    assert len(data["rows"]) == 5


def test_sql_editor_unauthorized_table_rejected():
    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    res = client.post(
        "/api/sql-editor",
        json={"dataset_id": DATASET_ID, "sql": "SELECT * FROM secret_system_table"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["error"] is not None
    assert "unauthorized or unknown table" in data["error"].lower()


def test_sql_editor_cte_bypass_rejected():
    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    res = client.post(
        "/api/sql-editor",
        json={
            "dataset_id": DATASET_ID,
            "sql": "WITH cte AS (SELECT * FROM unauthorized_tbl) SELECT * FROM cte",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["error"] is not None
    assert "unauthorized or unknown table" in data["error"].lower()


def test_sql_editor_alias_bypass_rejected():
    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    res = client.post(
        "/api/sql-editor",
        json={
            "dataset_id": DATASET_ID,
            "sql": "WITH t AS (SELECT 1) SELECT * FROM unauthorized_tbl AS u",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["error"] is not None
    assert "unauthorized or unknown table" in data["error"].lower()


def test_sql_editor_non_owner_query_rejected():
    app.dependency_overrides[get_current_user] = lambda: OTHER_USER
    client = TestClient(app)
    res = client.post(
        "/api/sql-editor",
        json={"dataset_id": DATASET_ID, "sql": f"SELECT * FROM {TABLE_NAME}"},
    )
    assert res.status_code == 404


def test_download_authorized_table_success():
    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    res = client.post(
        "/api/download/csv",
        json={"dataset_id": DATASET_ID, "sql": f"SELECT * FROM {TABLE_NAME} LIMIT 10"},
    )
    assert res.status_code == 200
    assert "text/csv" in res.headers["content-type"]
    assert b"financial_records" not in res.content
    assert b"item_0" in res.content


def test_download_unauthorized_table_rejected():
    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    res = client.post(
        "/api/download/csv",
        json={"dataset_id": DATASET_ID, "sql": "SELECT * FROM forbidden_tbl"},
    )
    assert res.status_code == 400
    assert "unauthorized or unknown table" in res.json()["detail"].lower()


def test_download_non_owner_rejected():
    app.dependency_overrides[get_current_user] = lambda: OTHER_USER
    client = TestClient(app)
    res = client.post(
        "/api/download/csv",
        json={"dataset_id": DATASET_ID, "sql": f"SELECT * FROM {TABLE_NAME}"},
    )
    assert res.status_code == 404


# ─── P0: Bounded Result Limits & Resource Protection Tests ───────────────────

def test_sql_editor_bounds_results_to_500_max():
    # Dataset has 1000 rows. Query selects all rows without LIMIT.
    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    res = client.post(
        "/api/sql-editor",
        json={"dataset_id": DATASET_ID, "sql": f"SELECT * FROM {TABLE_NAME}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["error"] is None
    # Must be bounded to at most 500 rows inside DuckDB
    assert len(data["rows"]) == 500


def test_sql_editor_handles_complex_ctes_with_limit():
    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    sql = f"""
        WITH ranked AS (
            SELECT id, name, amount,
                   ROW_NUMBER() OVER (ORDER BY amount DESC) as rnk
            FROM {TABLE_NAME}
        )
        SELECT * FROM ranked WHERE rnk <= 20
    """
    res = client.post(
        "/api/sql-editor",
        json={"dataset_id": DATASET_ID, "sql": sql},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["error"] is None
    assert len(data["rows"]) == 20


def test_sql_editor_empty_result_handled_safely():
    app.dependency_overrides[get_current_user] = lambda: OWNER_USER
    client = TestClient(app)
    res = client.post(
        "/api/sql-editor",
        json={"dataset_id": DATASET_ID, "sql": f"SELECT * FROM {TABLE_NAME} WHERE id = -999"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["error"] is None
    assert data["rows"] == []
    assert "id" in data["columns"]
