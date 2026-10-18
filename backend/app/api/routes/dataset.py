"""Dataset lifecycle endpoints: fetch schema, preview rows, and delete/cleanup with ownership checks."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.db.duckdb_manager import duckdb_manager
from app.models.schemas import DatasetSchema
from app.services.schema_service import extract_all_schemas, extract_schema
from app.utils.auth import get_current_user
from app.utils.dataset_auth import authorize_dataset_access, delete_dataset_with_ownership
from app.validation.sql_validator import escape_sql_identifier

router = APIRouter(prefix="/api/dataset", tags=["dataset"])


@router.get("/{dataset_id}/schema", response_model=DatasetSchema)
async def get_schema(
    dataset_id: str,
    current_user: dict = Depends(get_current_user),
) -> DatasetSchema:
    # 1. Authorize dataset ownership
    authorize_dataset_access(dataset_id, current_user)

    try:
        conn = duckdb_manager.get_connection(dataset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Dataset not found or expired.") from exc
    return extract_schema(conn, dataset_id)


@router.get("/{dataset_id}/schemas", response_model=list[DatasetSchema])
async def get_all_schemas(
    dataset_id: str,
    current_user: dict = Depends(get_current_user),
) -> list[DatasetSchema]:
    # 1. Authorize dataset ownership
    authorize_dataset_access(dataset_id, current_user)

    try:
        conn = duckdb_manager.get_connection(dataset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Dataset not found or expired.") from exc
    return extract_all_schemas(conn, dataset_id)


@router.get("/{dataset_id}/preview")
async def get_preview(
    dataset_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, list[dict[str, Any]]]:
    """Return the first 100 rows for every table in this dataset."""
    # 1. Authorize dataset ownership
    authorize_dataset_access(dataset_id, current_user)

    try:
        conn = duckdb_manager.get_connection(dataset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Dataset not found or expired.") from exc

    table_names = duckdb_manager.get_table_names(dataset_id)
    result: dict[str, list[dict[str, Any]]] = {}
    for table_name in table_names:
        safe_table = escape_sql_identifier(table_name)
        rows = (
            conn.execute(f"SELECT * FROM {safe_table} LIMIT 100")
            .fetch_df()
            .to_dict(orient="records")
        )
        result[table_name] = rows
    return result


@router.delete("/{dataset_id}")
async def delete_dataset(
    dataset_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Verify ownership and delete dataset across memory, disk, and MongoDB."""
    return delete_dataset_with_ownership(dataset_id, current_user)
