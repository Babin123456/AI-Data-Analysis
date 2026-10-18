"""Automated tests for MongoDB Lifespan, DuckDB Idle Eviction, and Multi-Worker Persistence."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db.duckdb_manager import DuckDBManager, duckdb_manager
from app.db.mongodb import (
    _active_client,
    client as mongo_client_proxy,
    close_mongo_connection,
    connect_to_mongo,
    db as mongo_db_proxy,
)
from app.main import app, lifespan
from app.utils.auth import get_current_user
from app.utils.dataset_auth import record_dataset_ownership

TEST_USER = {"id": "lifecycle_user_1", "username": "lifecycle_user", "email": "user@test.com", "role": "user"}


# ─── 1. MongoDB Lifecycle Management Tests ───────────────────────────────────

def test_import_has_no_mongodb_side_effects():
    """Verify that importing mongodb module does not establish active client or network connections."""
    import app.db.mongodb as mongo_mod
    # Module-level client must not be pre-connected
    assert mongo_mod._active_client is None


def test_mongodb_lifespan_connect_and_disconnect():
    """Verify explicit connect_to_mongo initializes client and close_mongo_connection closes it cleanly."""
    mock_raw_client = MagicMock()
    mock_raw_client.admin.command.return_value = {"ok": 1}
    mock_raw_client[settings.database_name] = MagicMock()

    with patch("app.db.mongodb.MongoClient", return_value=mock_raw_client):
        # 1. Connect
        connected_client = connect_to_mongo()
        assert connected_client is mock_raw_client
        mock_raw_client.admin.command.assert_called_with("ping")

        # 2. Idempotent repeated connect
        assert connect_to_mongo() is mock_raw_client

        # 3. Disconnect
        close_mongo_connection()
        mock_raw_client.close.assert_called_once()

        import app.db.mongodb as mongo_mod
        assert mongo_mod._active_client is None


def test_mongodb_startup_failure_when_required():
    """When MongoDB is required and fails to connect, startup must fail with a clear, actionable error."""
    old_req = settings.mongodb_required
    old_env = settings.app_env
    try:
        settings.mongodb_required = True
        settings.app_env = "production"

        with patch("app.db.mongodb.MongoClient", side_effect=Exception("Connection refused")):
            with pytest.raises(RuntimeError) as exc_info:
                connect_to_mongo()
            assert "unable to connect to MongoDB" in str(exc_info.value)
    finally:
        settings.mongodb_required = old_req
        settings.app_env = old_env
        close_mongo_connection()


# ─── 2. DuckDB Connection Lifecycle & Eviction Tests ─────────────────────────

def test_duckdb_idle_expiration():
    """Verify that idle connections exceeding the timeout are closed and evicted."""
    manager = DuckDBManager()
    ds_id = "test_idle_ds_01"

    # Create connection
    conn = manager.create_connection(ds_id, table_name="sample", use_memory=True)
    assert manager.exists(ds_id)

    # Artificially age the activity timestamp past timeout (e.g. 100 seconds ago with 50s timeout)
    manager._last_active[ds_id] = time.monotonic() - 100.0

    # Evict with 50s timeout threshold
    evicted_count = manager.cleanup_idle_connections(max_idle_seconds=50.0)
    assert evicted_count == 1
    assert ds_id not in manager._connections


def test_duckdb_active_queries_protected_from_eviction():
    """Verify that active queries (in_use > 0) are never evicted even if idle time exceeds threshold."""
    manager = DuckDBManager()
    ds_id = "test_active_query_ds_02"

    manager.create_connection(ds_id, table_name="sample", use_memory=True)

    # Use acquire_connection context manager
    with manager.acquire_connection(ds_id):
        # Artificially age timestamp
        manager._last_active[ds_id] = time.monotonic() - 100.0

        # Attempt eviction while in use
        evicted = manager.cleanup_idle_connections(max_idle_seconds=50.0)
        assert evicted == 0
        assert ds_id in manager._connections

    # Once context manager exits, in_use drops to 0; age timestamp and test eviction
    manager._last_active[ds_id] = time.monotonic() - 100.0
    evicted = manager.cleanup_idle_connections(max_idle_seconds=50.0)
    assert evicted == 1
    assert ds_id not in manager._connections


def test_duckdb_lru_cache_eviction():
    """Verify that when cached connections exceed max_connections, oldest idle connections are evicted."""
    manager = DuckDBManager()
    manager._max_cached = 3  # low limit for test

    for i in range(5):
        manager.create_connection(f"ds_{i}", table_name=f"tbl_{i}", use_memory=True)
        # Give each successive connection a slightly newer timestamp
        manager._last_active[f"ds_{i}"] = time.monotonic() + i

    # Cache should be bounded to at most max_cached (3)
    assert len(manager._connections) <= 3


def test_duckdb_repeated_cleanup_is_safe():
    """Repeated cleanup calls on empty or active cache must be safe and idempotent."""
    manager = DuckDBManager()
    assert manager.cleanup_idle_connections() == 0
    assert manager.cleanup_idle_connections() == 0
    assert manager.close_all_connections() == 0


def test_frontend_cleanup_endpoint():
    """Verify that navigator.sendBeacon fire-and-forget cleanup endpoint returns 204 and closes connection."""
    ds_id = "test_beacon_ds_03"
    duckdb_manager.create_connection(ds_id, table_name="beacon_tbl", use_memory=True)
    assert ds_id in duckdb_manager._connections

    client = TestClient(app)
    res = client.post(f"/api/dataset/{ds_id}/cleanup")
    assert res.status_code == 204
    assert ds_id not in duckdb_manager._connections


# ─── 3. Multi-Worker Compatibility & Persistence Tests ───────────────────────

def test_duckdb_multi_worker_persistence():
    """Verify that a dataset created by Worker 1 is stored in '{dataset_id}.duckdb'
    and can be opened and queried by Worker 2 without reloading raw files.
    """
    ds_id = "test_multiworker_ds_04"
    tbl_name = "products"
    upload_dir = Path(settings.upload_dir)
    db_file = upload_dir / f"{ds_id}.duckdb"
    db_file.unlink(missing_ok=True)

    worker_1 = DuckDBManager()
    worker_2 = DuckDBManager()

    try:
        # Worker 1: creates and writes dataset
        conn_w1 = worker_1.create_connection(ds_id, table_name=tbl_name, read_only=False)
        conn_w1.execute(f"CREATE TABLE {tbl_name} (id INT, product VARCHAR, price DOUBLE)")
        conn_w1.execute(f"INSERT INTO {tbl_name} VALUES (1, 'Laptop', 1200.0), (2, 'Mouse', 25.0)")
        worker_1.transition_to_read_only(ds_id)

        assert db_file.exists(), "Persistent .duckdb file was not created on disk"

        # Worker 2: brand-new manager instance (simulates separate worker process)
        assert ds_id not in worker_2._connections

        # Worker 2 opens connection directly from persistent file
        conn_w2 = worker_2.get_connection(ds_id, read_only=True)
        rows = conn_w2.execute(f"SELECT * FROM {tbl_name} ORDER BY id").fetchall()
        assert rows == [(1, "Laptop", 1200.0), (2, "Mouse", 25.0)]

        # Tables list is introspected without reloading raw files
        tables = worker_2.get_table_names(ds_id)
        assert tbl_name in tables

    finally:
        worker_1.drop_connection(ds_id)
        worker_2.drop_connection(ds_id)
        duckdb_manager.drop_connection(ds_id)
        db_file.unlink(missing_ok=True)
        (upload_dir / f"{ds_id}.duckdb.wal").unlink(missing_ok=True)


def test_duckdb_concurrent_readers():
    """Verify that multiple readers can query the same persistent .duckdb database concurrently."""
    ds_id = "test_concurrent_readers_ds_05"
    tbl_name = "metrics"
    upload_dir = Path(settings.upload_dir)
    db_file = upload_dir / f"{ds_id}.duckdb"
    db_file.unlink(missing_ok=True)

    w = DuckDBManager()

    try:
        # Create dataset
        c = w.create_connection(ds_id, table_name=tbl_name, read_only=False)
        c.execute(f"CREATE TABLE {tbl_name} AS SELECT range AS n FROM range(100)")
        w.transition_to_read_only(ds_id)

        # Worker A and Worker B connect concurrently with read_only=True
        conn_a = duckdb.connect(str(db_file), read_only=True)
        conn_b = duckdb.connect(str(db_file), read_only=True)

        res_a = conn_a.execute(f"SELECT COUNT(*) FROM {tbl_name}").fetchone()[0]
        res_b = conn_b.execute(f"SELECT SUM(n) FROM {tbl_name}").fetchone()[0]

        assert res_a == 100
        assert res_b == 4950

        conn_a.close()
        conn_b.close()
    finally:
        w.drop_connection(ds_id)
        duckdb_manager.drop_connection(ds_id)
        db_file.unlink(missing_ok=True)
        (upload_dir / f"{ds_id}.duckdb.wal").unlink(missing_ok=True)
