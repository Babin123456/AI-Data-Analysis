"""SQL Editor endpoint: run user-written SQL queries against uploaded datasets safely.

Enforces:
1. Dataset ownership authorization.
2. Mandatory table whitelisting via validate_sql(allowed_tables=...).
3. Bounded row materialization inside DuckDB (LIMIT 500) before pandas fetching.
4. Execution deadline and cancellation to prevent event-loop blocking and runaway computation.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.db.duckdb_manager import duckdb_manager
from app.models.schemas import SqlQueryRequest, SqlQueryResponse
from app.services.sql_pipeline import sanitize_records
from app.utils.auth import get_current_user
from app.utils.dataset_auth import authorize_dataset_access
from app.validation.sql_validator import SQLValidationError, validate_sql

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sql-editor", tags=["sql-editor"])

MAX_SQL_EDITOR_ROWS = 500
QUERY_TIMEOUT_SECONDS = 15.0


def _clean_sql_error(exc: Exception) -> str:
    """Format safe error description without exposing internal file paths or server details."""
    msg = str(exc)
    msg = re.sub(r"[A-Za-z]:\\[\w\\\.-]+", "[internal_path]", msg)
    msg = re.sub(r"/[\w/\.-]+", "[internal_path]", msg)
    if "out of memory" in msg.lower():
        return "Query exceeded the allocated memory limit (512MB)."
    if "Binder Error: Referenced column" in msg:
        col_match = re.search(r'Referenced column "([^"]+)"', msg)
        col_name = col_match.group(1) if col_match else "specified"
        return f"The column '{col_name}' does not exist in the dataset."
    if "Binder Error: Table with name" in msg:
        tbl_match = re.search(r'Table with name "([^"]+)"', msg)
        tbl_name = tbl_match.group(1) if tbl_match else "specified"
        return f"The table '{tbl_name}' does not exist in this dataset."
    return msg.split("\n")[0].strip()


def _execute_bounded_query(conn: Any, sql: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Execute bounded SQL synchronously inside DuckDB and fetch at most MAX_SQL_EDITOR_ROWS."""
    df = conn.execute(sql).fetch_df()
    columns = list(df.columns)
    rows = sanitize_records(df.to_dict(orient="records"))
    return columns, rows


@router.post("", response_model=SqlQueryResponse)
async def run_sql_query(
    req: SqlQueryRequest,
    current_user: dict = Depends(get_current_user),
) -> SqlQueryResponse:
    # 1. Authorize dataset ownership (fails closed on cross-user or unowned dataset)
    authorize_dataset_access(req.dataset_id, current_user)

    try:
        conn = duckdb_manager.get_connection(req.dataset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Dataset not found or expired.") from exc

    # 2. Derive allowed table whitelist strictly from authorized dataset
    allowed_tables = duckdb_manager.get_table_names(req.dataset_id)
    if not allowed_tables:
        return SqlQueryResponse(
            sql=req.sql,
            error="No active tables found for this dataset.",
        )

    # 3. Validate SQL against authorized table whitelist and statement rules
    try:
        safe_sql = validate_sql(req.sql, allowed_tables=allowed_tables)
    except SQLValidationError as exc:
        return SqlQueryResponse(
            sql=req.sql,
            error=str(exc),
        )

    # 4. Construct bounded SQL wrapper to apply row limit inside DuckDB engine
    # (prevents fetching millions of rows into pandas memory)
    bounded_sql = f"SELECT * FROM ({safe_sql}) AS limited_result LIMIT {MAX_SQL_EDITOR_ROWS}"

    # 5. Execute in worker thread with timeout deadline and query interruption
    try:
        columns, rows = await asyncio.wait_for(
            asyncio.to_thread(_execute_bounded_query, conn, bounded_sql),
            timeout=QUERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        try:
            conn.interrupt()
        except Exception as int_exc:
            logger.warning("Failed to interrupt query on timeout: %s", int_exc)
        return SqlQueryResponse(
            sql=safe_sql,
            error=f"Query execution timed out after {int(QUERY_TIMEOUT_SECONDS)} seconds. The query is too computationally expensive.",
        )
    except Exception as exc:  # noqa: BLE001
        clean_err = _clean_sql_error(exc)
        return SqlQueryResponse(
            sql=safe_sql,
            error=f"Query execution failed: {clean_err}",
        )

    return SqlQueryResponse(
        sql=safe_sql,
        columns=columns,
        rows=rows,
    )


@router.get("/{dataset_id}/tables")
async def list_tables(
    dataset_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    # Authorize dataset ownership
    authorize_dataset_access(dataset_id, current_user)

    table_names = duckdb_manager.get_table_names(dataset_id)
    return {"dataset_id": dataset_id, "tables": table_names}
