"""Download endpoints for exporting query results.

The client sends back the SQL it wants exported (already validated once
by /api/chat or /api/explore) plus the target format; we re-validate,
re-run, and stream the result back as a file.
"""
from __future__ import annotations

import io

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.db.duckdb_manager import duckdb_manager
from app.utils.auth import get_current_user
from app.utils.dataset_auth import authorize_dataset_access
from app.validation.sql_validator import SQLValidationError, validate_sql

router = APIRouter(prefix="/api/download", tags=["download"])


class DownloadRequest(BaseModel):
    dataset_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    sql: str = Field(..., min_length=1, max_length=10000)


@router.post("/csv")
async def download_csv(
    req: DownloadRequest,
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    df = _run_query(req, current_user)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=result.csv"},
    )


@router.post("/excel")
async def download_excel(
    req: DownloadRequest,
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    df = _run_query(req, current_user)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Result")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=result.xlsx"},
    )


def _run_query(req: DownloadRequest, current_user: dict) -> pd.DataFrame:
    # 1. Authorize dataset ownership (fails closed on unowned or cross-user datasets)
    authorize_dataset_access(req.dataset_id, current_user)

    try:
        conn = duckdb_manager.get_connection(req.dataset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Dataset not found or expired.") from exc

    # 2. Derive allowed tables strictly from authorized dataset
    allowed_tables = duckdb_manager.get_table_names(req.dataset_id)
    if not allowed_tables:
        raise HTTPException(status_code=400, detail="No readable tables found for this dataset.")

    # 3. Validate SQL against authorized table whitelist
    try:
        safe_sql = validate_sql(req.sql, allowed_tables=allowed_tables)
    except SQLValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 4. Execute query
    try:
        return conn.execute(safe_sql).fetch_df()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Query failed: {exc}") from exc
