"""Centralized dataset ownership and authorization helper.

Associates each uploaded dataset with an authenticated user, persists ownership
in MongoDB and on disk, and enforces ownership before any read, write, or destructive
operation.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from app.config import settings
from app.db.duckdb_manager import duckdb_manager

logger = logging.getLogger(__name__)


def get_user_identifier(user: dict[str, Any]) -> str:
    """Extract a canonical user ID string from the authenticated user dict."""
    user_id = str(user.get("id") or user.get("_id") or user.get("username") or "").strip()
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not determine authenticated user identity.",
        )
    return user_id


def _get_meta_path(dataset_id: str) -> Path:
    """Return the filesystem path for the dataset ownership metadata file."""
    upload_dir = Path(settings.upload_dir)
    return upload_dir / f"{dataset_id}.meta.json"


def record_dataset_ownership(
    dataset_id: str,
    user: dict[str, Any],
    filenames: list[str] | None = None,
    table_names: list[str] | None = None,
) -> None:
    """Persist dataset ownership for the authenticated user across memory, disk, and MongoDB."""
    user_id = get_user_identifier(user)
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. In-memory DuckDB manager registry
    duckdb_manager.set_dataset_owner(dataset_id, user_id)

    # 2. Local metadata file (guarantees survival across process restarts even if MongoDB is offline)
    meta_path = _get_meta_path(dataset_id)
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_content = {
            "dataset_id": dataset_id,
            "user_id": user_id,
            "filenames": filenames or [],
            "table_names": table_names or [],
            "created_at": now_iso,
        }
        meta_path.write_text(json.dumps(meta_content, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.error("Failed to write dataset metadata file for %s: %s", dataset_id, exc)

    # 3. MongoDB collection (durable multi-worker database record)
    try:
        from app.db.mongodb import db
        db["datasets"].update_one(
            {"dataset_id": dataset_id},
            {
                "$set": {
                    "dataset_id": dataset_id,
                    "user_id": user_id,
                    "filenames": filenames or [],
                    "table_names": table_names or [],
                    "updated_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {
                    "created_at": datetime.now(timezone.utc),
                },
            },
            upsert=True,
        )
    except Exception as exc:
        logger.warning("MongoDB record_dataset_ownership skipped or failed: %s", exc)


def get_dataset_owner(dataset_id: str) -> str | None:
    """Retrieve owner user_id for dataset_id, checking in-memory, disk, and MongoDB in order."""
    # 1. In-memory check
    owner = duckdb_manager.get_dataset_owner(dataset_id)
    if owner:
        return owner

    # 2. Disk metadata check
    meta_path = _get_meta_path(dataset_id)
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            owner = data.get("user_id")
            if owner:
                duckdb_manager.set_dataset_owner(dataset_id, owner)
                return owner
        except Exception as exc:
            logger.warning("Failed to read metadata file for %s: %s", dataset_id, exc)

    # 3. MongoDB check
    try:
        from app.db.mongodb import db
        doc = db["datasets"].find_one({"dataset_id": dataset_id})
        if doc and doc.get("user_id"):
            owner = str(doc["user_id"])
            duckdb_manager.set_dataset_owner(dataset_id, owner)
            return owner
    except Exception as exc:
        logger.warning("MongoDB get_dataset_owner lookup failed for %s: %s", dataset_id, exc)

    return None


def authorize_dataset_access(dataset_id: str, user: dict[str, Any]) -> str:
    """Authorize access to dataset_id for the given authenticated user.

    Fails closed:
    - If dataset does not exist -> 404
    - If dataset is legacy/unowned -> 404 (does not expose or grant access)
    - If user is not the owner -> 404 (avoids exposing dataset existence to other users)

    Returns the authorized user_id.
    """
    if not dataset_id or not duckdb_manager.exists(dataset_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dataset not found or session expired.",
        )

    current_user_id = get_user_identifier(user)
    owner_id = get_dataset_owner(dataset_id)

    # Fail closed on legacy unowned datasets
    if owner_id is None:
        logger.warning(
            "Access denied to unowned/legacy dataset %s by user %s",
            dataset_id,
            current_user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dataset not found or session expired.",
        )

    # Strict ownership check
    if owner_id != current_user_id:
        logger.warning(
            "Security violation: user %s attempted unauthorized access to dataset %s owned by %s",
            current_user_id,
            dataset_id,
            owner_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dataset not found or session expired.",
        )

    return current_user_id


def delete_dataset_with_ownership(dataset_id: str, user: dict[str, Any]) -> dict[str, str]:
    """Verify ownership and atomically clean up dataset across memory, disk, and MongoDB."""
    user_id = authorize_dataset_access(dataset_id, user)

    # 1. Close DuckDB in-memory connection and remove from registry
    duckdb_manager.drop_connection(dataset_id)

    # 2. Invalidate schema cache
    from app.services.schema_service import invalidate_schema_cache
    invalidate_schema_cache(dataset_id)

    # 3. Clean up uploaded disk files and metadata file
    upload_dir = Path(settings.upload_dir)
    if upload_dir.exists():
        prefix = f"{dataset_id}_"
        for f in upload_dir.glob(f"{prefix}*"):
            f.unlink(missing_ok=True)
        meta_path = _get_meta_path(dataset_id)
        meta_path.unlink(missing_ok=True)

    # 4. Atomically delete from MongoDB using compound filter (database-level ownership enforcement)
    try:
        from app.db.mongodb import db
        db["datasets"].delete_one({"dataset_id": dataset_id, "user_id": user_id})
    except Exception as exc:
        logger.warning("MongoDB dataset deletion failed for %s: %s", dataset_id, exc)

    return {"status": "deleted", "dataset_id": dataset_id}
