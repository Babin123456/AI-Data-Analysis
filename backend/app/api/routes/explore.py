"""Manual "Explore" mode endpoint: user picks columns/aggregation/chart type
directly, we build and run a safe, validated aggregate or distribution query.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from app.db.duckdb_manager import duckdb_manager
from app.models.schemas import ExploreRequest
from app.services.chart_service import build_chart_spec
from app.validation.sql_validator import escape_sql_identifier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/explore", tags=["explore"])

_AGG_SQL = {
    "sum": "SUM",
    "avg": "AVG",
    "count": "COUNT",
    "min": "MIN",
    "max": "MAX",
}

_NUMERIC_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "INT", "INT1", "INT2", "INT4", "INT8",
    "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
    "FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "DOUBLE PRECISION", "DECIMAL", "NUMERIC", "REAL",
}


def _is_numeric_type(col_type: str) -> bool:
    """Check if a DuckDB type string represents a numeric column."""
    base = col_type.split("(")[0].strip().upper()
    return base in _NUMERIC_TYPES


@router.post("")
async def explore(req: ExploreRequest) -> dict[str, Any]:
    try:
        conn = duckdb_manager.get_connection(req.dataset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Dataset not found or session expired. Please re-upload your file.") from exc

    # 1. Validate target table
    if req.table_name:
        available_tables = duckdb_manager.get_table_names(req.dataset_id)
        if req.table_name not in available_tables:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown table '{req.table_name}'. Available tables: {available_tables}",
            )
        target_table = req.table_name
    else:
        target_table = duckdb_manager.get_table_name(req.dataset_id)

    safe_table = escape_sql_identifier(target_table)

    # 2. Introspect table columns and data types
    try:
        describe_rows = conn.execute(f"DESCRIBE {safe_table}").fetchall()
    except Exception as exc:
        logger.exception("Failed to describe table %s: %s", target_table, exc)
        raise HTTPException(status_code=400, detail=f"Failed to inspect table '{target_table}'.") from exc

    column_types: dict[str, str] = {row[0]: row[1] for row in describe_rows}

    # 3. Validate column existence
    if req.x_column not in column_types:
        raise HTTPException(
            status_code=400,
            detail=f"Column '{req.x_column}' does not exist in table '{target_table}'. Available columns: {list(column_types.keys())}",
        )
    if req.y_column and req.y_column not in column_types:
        raise HTTPException(
            status_code=400,
            detail=f"Column '{req.y_column}' does not exist in table '{target_table}'. Available columns: {list(column_types.keys())}",
        )

    x_type = column_types[req.x_column]
    y_type = column_types[req.y_column] if req.y_column else None

    # 4. Enforce chart compatibility and type safety
    if req.chart_type == "histogram":
        if not _is_numeric_type(x_type):
            raise HTTPException(
                status_code=400,
                detail=f"Histogram requires a numeric X-axis column, but '{req.x_column}' is '{x_type}'. Please choose a numeric column.",
            )

    elif req.chart_type == "scatter":
        if not req.y_column:
            raise HTTPException(
                status_code=400,
                detail="Scatter plots require both X-axis and Y-axis columns to be selected.",
            )
        if not _is_numeric_type(x_type):
            raise HTTPException(
                status_code=400,
                detail=f"Scatter plot requires a numeric X-axis column, but '{req.x_column}' is '{x_type}'.",
            )
        if y_type and not _is_numeric_type(y_type):
            raise HTTPException(
                status_code=400,
                detail=f"Scatter plot requires a numeric Y-axis column, but '{req.y_column}' is '{y_type}'.",
            )

    elif req.chart_type == "box":
        metric_col = req.y_column or req.x_column
        metric_type = y_type or x_type
        if not _is_numeric_type(metric_type):
            raise HTTPException(
                status_code=400,
                detail=f"Box plot requires a numeric metric column for distribution, but '{metric_col}' is '{metric_type}'.",
            )

    # 5. Enforce aggregation compatibility
    if req.aggregation in ("sum", "avg", "min", "max"):
        if not req.y_column:
            raise HTTPException(
                status_code=400,
                detail=f"Aggregation '{req.aggregation.upper()}' requires a Y-axis column. Please select a numeric Y-axis column or choose COUNT.",
            )
        if y_type and not _is_numeric_type(y_type):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot calculate {req.aggregation.upper()} on non-numeric column '{req.y_column}' ({y_type}). Please select a numeric column for Y-axis or choose COUNT aggregation.",
            )

    safe_x = escape_sql_identifier(req.x_column)
    safe_y = escape_sql_identifier(req.y_column) if req.y_column else None

    # 6. Build deterministic, secure query
    if req.chart_type == "histogram":
        sql = f"SELECT {safe_x} FROM {safe_table} WHERE {safe_x} IS NOT NULL LIMIT 1000"
    elif req.chart_type == "scatter":
        sql = f"SELECT {safe_x}, {safe_y} FROM {safe_table} WHERE {safe_x} IS NOT NULL AND {safe_y} IS NOT NULL LIMIT 1000"
    elif req.chart_type == "box":
        if safe_y and safe_x != safe_y:
            sql = f"SELECT {safe_x}, {safe_y} FROM {safe_table} WHERE {safe_y} IS NOT NULL LIMIT 1000"
        else:
            metric = safe_y or safe_x
            sql = f"SELECT {metric} FROM {safe_table} WHERE {metric} IS NOT NULL LIMIT 1000"
    elif req.aggregation != "none" and safe_y:
        agg_fn = _AGG_SQL[req.aggregation]
        if req.aggregation == "count":
            sql = (
                f"SELECT {safe_x}, COUNT({safe_y}) AS count "
                f"FROM {safe_table} WHERE {safe_x} IS NOT NULL GROUP BY {safe_x} ORDER BY count DESC LIMIT 500"
            )
        else:
            sql = (
                f"SELECT {safe_x}, {agg_fn}({safe_y}) AS {safe_y} "
                f"FROM {safe_table} WHERE {safe_x} IS NOT NULL GROUP BY {safe_x} ORDER BY 2 DESC LIMIT 500"
            )
    elif safe_y and req.aggregation == "none":
        sql = f"SELECT {safe_x}, {safe_y} FROM {safe_table} WHERE {safe_x} IS NOT NULL LIMIT 500"
    else:
        # Default grouping count by X
        sql = f"SELECT {safe_x}, COUNT(*) AS count FROM {safe_table} WHERE {safe_x} IS NOT NULL GROUP BY {safe_x} ORDER BY count DESC LIMIT 500"

    try:
        df = conn.execute(sql).fetch_df()
    except Exception as exc:  # noqa: BLE001
        logger.exception("DuckDB query execution failed for sql: %s: %s", sql, exc)
        raise HTTPException(status_code=400, detail=f"Query failed: {exc}") from exc

    rows = df.to_dict(orient="records")
    y_col_spec = req.y_column if req.y_column else ("count" if "count" in df.columns else None)
    chart_spec = build_chart_spec(
        req.chart_type,
        list(df.columns),
        rows,
        x_col=req.x_column,
        y_col=y_col_spec,
    )

    return {
        "sql": sql,
        "columns": list(df.columns),
        "rows": rows,
        "chart_type": req.chart_type,
        "chart_spec": chart_spec,
    }
