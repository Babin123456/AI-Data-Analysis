"""Pydantic request/response models shared across the API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ChartType = Literal[
    "bar",
    "line",
    "pie",
    "scatter",
    "histogram",
    "box",
    "area",
    "kpi",
    "heatmap",
    "horizontal_bar",
    "table",
]


class ColumnSchema(BaseModel):
    name: str
    dtype: str
    nullable: bool = True
    missing_count: int = 0
    is_numeric: bool = False
    is_datetime: bool = False
    is_categorical: bool = False
    is_boolean: bool = False


class DatasetSchema(BaseModel):
    dataset_id: str
    table_name: str
    row_count: int
    column_count: int
    columns: list[ColumnSchema]
    duplicate_row_count: int = 0


class FileInfo(BaseModel):
    filename: str
    file_type: str
    table_name: str
    schema_: DatasetSchema = Field(alias="schema")
    preview_rows: list[dict[str, Any]]

    class Config:
        populate_by_name = True


class UploadResponse(BaseModel):
    dataset_id: str
    files: list[FileInfo]


class SqlQueryRequest(BaseModel):
    dataset_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    sql: str = Field(..., min_length=1, max_length=10000)


class SqlQueryResponse(BaseModel):
    sql: str
    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    error: str | None = None


class ExploreRequest(BaseModel):
    dataset_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    x_column: str = Field(..., min_length=1, max_length=256)
    y_column: str | None = Field(default=None, max_length=256)
    aggregation: Literal["sum", "avg", "count", "min", "max", "none"] = "none"
    chart_type: ChartType = "bar"
    table_name: str | None = Field(default=None, max_length=128)


class ChatRequest(BaseModel):
    dataset_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    question: str = Field(..., min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=64)
    table_name: str | None = Field(default=None, max_length=128)


class ChatResponse(BaseModel):
    answer: str
    sql: str | None = None
    columns: list[str] = []
    rows: list[dict[str, Any]] = []
    chart_type: ChartType | None = None
    chart_spec: dict[str, Any] | None = None
    warning: str | None = None
    off_topic: bool = False


class ErrorResponse(BaseModel):
    detail: str
