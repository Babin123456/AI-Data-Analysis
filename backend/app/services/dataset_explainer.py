"""Generates comprehensive explanations of dataset schemas, columns, and structure."""
from __future__ import annotations

import logging
from app.services.schema_service import DatasetSchema

logger = logging.getLogger(__name__)


def explain_dataset(schemas: list[DatasetSchema], question: str = "") -> str:
    """Generate a clean, structured explanation of the dataset schema without running SQL."""
    if not schemas:
        return "No schema information is currently available for this dataset."

    if len(schemas) == 1:
        s = schemas[0]
        numeric_cols = [c.name for c in s.columns if c.is_numeric]
        cat_cols = [c.name for c in s.columns if c.is_categorical]
        date_cols = [c.name for c in s.columns if c.is_datetime]
        missing_cols = [c.name for c in s.columns if c.missing_count > 0]

        lines = [
            f"### Dataset Overview for **{s.table_name}**",
            f"- **Rows:** {s.row_count:,}",
            f"- **Columns:** {s.column_count}",
        ]
        if s.duplicate_row_count > 0:
            lines.append(f"- **Duplicate Rows:** {s.duplicate_row_count:,}")

        lines.append("\n**Column Breakdown:**")
        if numeric_cols:
            lines.append(f"- **Numeric ({len(numeric_cols)}):** {', '.join(numeric_cols)}")
        if cat_cols:
            lines.append(f"- **Categorical / Text ({len(cat_cols)}):** {', '.join(cat_cols)}")
        if date_cols:
            lines.append(f"- **Date & Time ({len(date_cols)}):** {', '.join(date_cols)}")

        if missing_cols:
            lines.append(f"\n⚠️ **Columns with Missing Values:** {', '.join(missing_cols)}")
        else:
            lines.append("\n✅ **Data Completeness:** No missing values detected across columns.")

        lines.append("\n*You can ask me to compute totals, find trends, filter records, or create visual charts for any of these columns!*")
        return "\n".join(lines)

    # Multi-table dataset
    lines = [f"### Multi-Table Dataset Overview ({len(schemas)} tables)"]
    for s in schemas:
        col_names = [c.name for c in s.columns]
        lines.append(
            f"- **{s.table_name}**: {s.row_count:,} rows, {s.column_count} columns ({', '.join(col_names[:6])}{', ...' if len(col_names) > 6 else ''})"
        )
    lines.append("\n*Feel free to ask questions about specific tables or cross-table comparisons!*")
    return "\n".join(lines)
