"""Safe SQL execution pipeline with validation, bounded repair, and DuckDB result serialization."""
from __future__ import annotations

import datetime
import decimal
import json
import logging
import math
import re
from typing import Any

import duckdb
import pandas as pd

from app.db.duckdb_manager import duckdb_manager
from app.models.schemas import DatasetSchema
from app.services.llm_service import LLMServiceError, llm_service
from app.validation.sql_validator import SQLValidationError, validate_sql

logger = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = 2
QUERY_ROW_LIMIT = 100


def sanitize_value(val: Any) -> Any:
    """Recursively serialize DuckDB/pandas data types to standard JSON-safe primitives."""
    if val is None:
        return None
    if isinstance(val, (bool, str, int)):
        return val
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    if isinstance(val, (datetime.date, datetime.datetime, datetime.time)):
        return val.isoformat()
    if isinstance(val, decimal.Decimal):
        return float(val)
    if isinstance(val, (bytes, bytearray)):
        return val.decode("utf-8", errors="replace")
    if pd.isna(val):
        return None
    return str(val)


def sanitize_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize a list of row dicts for JSON serialization."""
    sanitized = []
    for r in records:
        sanitized.append({k: sanitize_value(v) for k, v in r.items()})
    return sanitized


class SQLPipelineResult:
    def __init__(
        self,
        success: bool,
        answer: str,
        sql: str | None = None,
        columns: list[str] | None = None,
        rows: list[dict[str, Any]] | None = None,
        error_info: dict[str, Any] | None = None,
        is_empty: bool = False,
    ) -> None:
        self.success = success
        self.answer = answer
        self.sql = sql
        self.columns = columns or []
        self.rows = rows or []
        self.error_info = error_info
        self.is_empty = is_empty


def _clean_sql_error_message(exc: Exception) -> str:
    """Format a user-friendly, safe error description without exposing internal paths or stack traces."""
    msg = str(exc)
    # Strip any OS file paths or internal pointers
    msg = re.sub(r"[A-Za-z]:\\[\w\\\.-]+", "[internal_path]", msg)
    msg = re.sub(r"/[\w/\.-]+", "[internal_path]", msg)
    # Common DuckDB error translations
    if "Binder Error: Referenced column" in msg:
        col_match = re.search(r'Referenced column "([^"]+)"', msg)
        col_name = col_match.group(1) if col_match else "specified"
        return f"The column '{col_name}' does not exist in the dataset table."
    if "Binder Error: Table with name" in msg:
        tbl_match = re.search(r'Table with name "([^"]+)"', msg)
        tbl_name = tbl_match.group(1) if tbl_match else "specified"
        return f"The table '{tbl_name}' does not exist in this dataset."
    if "Conversion Error" in msg or "Could not convert" in msg:
        return "An incompatible data type was used in an aggregation or calculation."
    if "No function matches" in msg:
        return "An unsupported function or aggregation was used on this column type."
    return msg.split("\n")[0].strip()


class SQLPipeline:
    """Executes safe SQL generation, strict validation, DuckDB execution, and bounded repair."""

    def run(
        self,
        conn: duckdb.DuckDBPyConnection,
        dataset_id: str,
        question: str,
        schemas: list[DatasetSchema],
        target_table: str | None = None,
        conversation_context: str = "",
    ) -> SQLPipelineResult:
        allowed_tables = duckdb_manager.get_table_names(dataset_id)

        # 1. Generate initial SQL
        try:
            raw_sql = llm_service.generate_sql(
                question=question,
                schema=schemas,
                target_table=target_table,
                conversation_context=conversation_context,
            )
        except LLMServiceError as exc:
            logger.error("LLM SQL generation error: %s", exc)
            return SQLPipelineResult(
                success=False,
                answer="I was unable to generate an analysis query for your question. Please rephrase or try again.",
                sql=None,
                error_info={"code": "llm_generation_error", "message": str(exc)},
            )
        except Exception as exc:
            logger.error("Unexpected error during SQL generation: %s", exc)
            return SQLPipelineResult(
                success=False,
                answer="An unexpected error occurred while preparing your query.",
                sql=None,
                error_info={"code": "sql_preparation_error", "message": str(exc)},
            )

        current_sql = raw_sql
        last_error_msg = ""
        repair_attempts = 0

        while True:
            # 2. Validate SQL
            try:
                safe_sql = validate_sql(current_sql, allowed_tables=allowed_tables)
            except SQLValidationError as exc:
                err_msg = str(exc)
                logger.warning("SQL validation failed for query [%s]: %s", current_sql, err_msg)
                # If prompt injection / security violation was caught, do not attempt repair
                if any(k in err_msg.lower() for k in ["forbidden", "system", "multiple", "injection", "privileges"]):
                    return SQLPipelineResult(
                        success=False,
                        answer="I can only help you analyze and query your uploaded data.",
                        sql=None,
                        error_info={"code": "security_validation_failed", "message": err_msg},
                    )
                # Attempt repair if bounded attempts remain
                if repair_attempts < MAX_REPAIR_ATTEMPTS:
                    repair_attempts += 1
                    logger.info("Attempting SQL repair #%d for validation error: %s", repair_attempts, err_msg)
                    current_sql = self._attempt_repair(
                        question=question,
                        failed_sql=current_sql,
                        error_msg=err_msg,
                        schemas=schemas,
                        target_table=target_table,
                    )
                    if current_sql:
                        continue
                return SQLPipelineResult(
                    success=False,
                    answer="The requested query violates database safety constraints and cannot be executed.",
                    sql=None,
                    error_info={"code": "validation_failed", "message": err_msg},
                )

            # 3. Execute SQL safely against DuckDB
            try:
                bounded_sql = f"SELECT * FROM ({safe_sql}) LIMIT {QUERY_ROW_LIMIT}"
                df = conn.execute(bounded_sql).fetch_df()
                columns = list(df.columns)
                rows = sanitize_records(df.to_dict(orient="records"))

                # Handle empty results
                if len(rows) == 0:
                    return SQLPipelineResult(
                        success=True,
                        answer="No records matching your criteria were found in the dataset.",
                        sql=safe_sql,
                        columns=columns,
                        rows=[],
                        is_empty=True,
                    )

                # Successful execution with data -> explain results
                try:
                    explanation = llm_service.explain_results(question, columns, rows)
                except Exception as exc:
                    logger.warning("Explanation generation failed: %s", exc)
                    explanation = f"Found {len(rows)} matching record{'s' if len(rows) != 1 else ''} for your question."

                return SQLPipelineResult(
                    success=True,
                    answer=explanation,
                    sql=safe_sql,
                    columns=columns,
                    rows=rows,
                )

            except Exception as exc:
                last_error_msg = _clean_sql_error_message(exc)
                logger.warning("DuckDB query execution failed [%s]: %s", safe_sql, exc)

                if repair_attempts < MAX_REPAIR_ATTEMPTS:
                    repair_attempts += 1
                    logger.info("Attempting SQL repair #%d for execution error: %s", repair_attempts, last_error_msg)
                    repaired = self._attempt_repair(
                        question=question,
                        failed_sql=safe_sql,
                        error_msg=last_error_msg,
                        schemas=schemas,
                        target_table=target_table,
                    )
                    if repaired and repaired != safe_sql:
                        current_sql = repaired
                        continue

                # All repair attempts exhausted
                user_msg = (
                    f"The query could not be executed on your data: {last_error_msg}. "
                    "Please check the column names or rephrase your question."
                )
                return SQLPipelineResult(
                    success=False,
                    answer=user_msg,
                    sql=None,
                    error_info={"code": "execution_failed", "message": last_error_msg},
                )

    def _attempt_repair(
        self,
        question: str,
        failed_sql: str,
        error_msg: str,
        schemas: list[DatasetSchema],
        target_table: str | None = None,
    ) -> str | None:
        """Ask LLM to repair SQL without altering the intended analysis."""
        from app.services.schema_service import all_schemas_to_llm_prompt, schema_to_llm_prompt

        schema_text = all_schemas_to_llm_prompt(schemas, target_table)
        prompt = (
            f"{schema_text}\n\n"
            f"User Question: \"{question}\"\n"
            f"Failed SQL: {failed_sql}\n"
            f"Database Error: {error_msg}\n\n"
            "Task: Fix the SQL query to resolve the database error and accurately answer the user's question.\n"
            "Rules:\n"
            "- Do NOT alter the user's intended analysis.\n"
            "- If a column name is wrong or misspelled, use the correct column name from the schema.\n"
            "- If an aggregate failed due to data types (e.g. SUM on VARCHAR), use TRY_CAST(col AS DOUBLE) or appropriate conversion.\n"
            "- Output ONLY the corrected DuckDB SQL without markdown fences or comments."
        )

        try:
            response = llm_service._create_completion(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a specialized SQL repair engine for DuckDB. Return only valid SQL.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=512,
            )
            repaired = (response.choices[0].message.content or "").strip()
            # Strip markdown fences
            repaired = re.sub(r"^```(sql)?", "", repaired, flags=re.IGNORECASE).strip()
            repaired = re.sub(r"```$", "", repaired).strip()
            return repaired
        except Exception as exc:
            logger.warning("LLM SQL repair invocation failed: %s", exc)
            return None


sql_pipeline = SQLPipeline()
