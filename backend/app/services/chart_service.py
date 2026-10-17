"""Deterministic chart-type selection.

The AI is not trusted to pick the chart type on its own; instead we
inspect the shape of the result set (column count, dtypes, cardinality)
and apply simple rules, per the project spec.
"""
from __future__ import annotations

from typing import Any

# pyrefly: ignore [missing-import]
from app.models.schemas import ChartType


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_datelike(value: Any) -> bool:
    return isinstance(value, str) and bool(
        __import__("re").match(r"^\d{4}-\d{2}-\d{2}", value)
    )


def select_chart_type(columns: list[str], rows: list[dict[str, Any]]) -> ChartType:
    """Pick a chart type based on the shape of the query result."""
    if not rows:
        return "table"

    if len(columns) == 1 and len(rows) == 1:
        return "kpi"

    sample = rows[0]
    col_types = {col: type(sample.get(col)) for col in columns}
    numeric_cols = [c for c in columns if _is_numeric(sample.get(c))]
    date_cols = [c for c in columns if _is_datelike(sample.get(c))]
    non_numeric_cols = [c for c in columns if c not in numeric_cols]

    if len(numeric_cols) == 1 and len(rows) == 1 and len(columns) == 1:
        return "kpi"

    if date_cols and numeric_cols:
        return "line"

    if len(numeric_cols) == 2 and len(non_numeric_cols) == 0:
        return "scatter"

    if len(numeric_cols) >= 1 and len(non_numeric_cols) == 1:
        if len(rows) > 12:
            return "horizontal_bar"
        return "bar"

    if len(numeric_cols) >= 3:
        return "heatmap"

    return "table"


def build_chart_spec(
    chart_type: ChartType,
    columns: list[str],
    rows: list[dict[str, Any]],
    x_col: str | None = None,
    y_col: str | None = None,
) -> dict[str, Any]:
    """Build a minimal Plotly-friendly spec the frontend can render directly."""
    if chart_type == "table" or not rows:
        return {"type": "table", "columns": columns, "rows": rows}

    # Resolve actual X column
    if x_col and x_col in columns:
        actual_x = x_col
    else:
        non_numeric = [c for c in columns if not _is_numeric(rows[0].get(c))]
        actual_x = non_numeric[0] if non_numeric else columns[0]

    # Resolve actual Y column
    if y_col and y_col in columns:
        actual_y = y_col
    else:
        remaining = [c for c in columns if c != actual_x]
        numeric = [c for c in remaining if _is_numeric(rows[0].get(c))]
        actual_y = numeric[0] if numeric else (remaining[0] if remaining else actual_x)

    # Histogram: Plotly automatically calculates bins from raw numeric X values
    if chart_type == "histogram":
        return {
            "type": "histogram",
            "x": [row.get(actual_x) for row in rows],
            "x_label": actual_x,
            "y_label": "Frequency",
        }

    # Box plot: handles grouped box plot or single-distribution box plot
    if chart_type == "box":
        if x_col and y_col and x_col in columns and y_col in columns and x_col != y_col:
            return {
                "type": "box",
                "x": [row.get(actual_x) for row in rows],
                "y": [row.get(actual_y) for row in rows],
                "x_label": actual_x,
                "y_label": actual_y,
            }
        metric_col = actual_y if (y_col and y_col in columns) else actual_x
        return {
            "type": "box",
            "y": [row.get(metric_col) for row in rows],
            "y_label": metric_col,
        }

    # Standard charts: bar, line, pie, scatter, area, horizontal_bar
    return {
        "type": chart_type,
        "x": [row.get(actual_x) for row in rows],
        "y": [row.get(actual_y) for row in rows],
        "x_label": actual_x,
        "y_label": actual_y,
    }
