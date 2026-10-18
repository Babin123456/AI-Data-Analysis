"""Upload endpoint: accepts one or more files (max 10), loads them into
DuckDB as separate tables, and returns schema + preview for each.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

logger = logging.getLogger(__name__)

from app.config import settings
from app.db.duckdb_manager import duckdb_manager, filename_to_table_name
from app.models.schemas import DatasetSchema, FileInfo, UploadResponse
from app.services.file_loader import UnsupportedFileError, file_loader
from app.services.schema_service import extract_table_schema
from app.utils.auth import get_current_user
from app.utils.dataset_auth import authorize_dataset_access, record_dataset_ownership
from app.utils.file_utils import (
    is_allowed_extension,
    new_dataset_id,
    sanitize_filename,
    upload_path_for,
)
from app.validation.sql_validator import escape_sql_identifier

MAX_FILES = 10

router = APIRouter(prefix="/api/upload", tags=["upload"])


@router.post("", response_model=UploadResponse)
async def upload_dataset(
    files: List[UploadFile] = File(...),
    dataset_id: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user),
) -> UploadResponse:
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")

    if len(files) > MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum {MAX_FILES} files allowed per upload.",
        )

    if dataset_id:
        if duckdb_manager.exists(dataset_id):
            # Verify the authenticated user owns this dataset before allowing additions
            authorize_dataset_access(dataset_id, current_user)
        else:
            dataset_id = new_dataset_id()
    else:
        dataset_id = new_dataset_id()

    file_infos: list[FileInfo] = []
    conn = None

    for idx, file in enumerate(files):
        if not file.filename:
            raise HTTPException(status_code=400, detail=f"File #{idx + 1} has no filename.")

        if len(file.filename) > 255:
            raise HTTPException(
                status_code=400,
                detail=f"Filename '{file.filename[:30]}...' is too long (maximum 255 characters allowed).",
            )

        if not is_allowed_extension(file.filename):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported file type for '{file.filename}'. Allowed: "
                    f"{', '.join(settings.allowed_extensions)}"
                ),
            )

        contents = await file.read()
        if len(contents) == 0:
            raise HTTPException(
                status_code=400,
                detail=f"File '{file.filename}' is empty (0 bytes). Please upload a valid dataset.",
            )
        size_mb = len(contents) / (1024 * 1024)
        if size_mb > settings.max_upload_mb:
            raise HTTPException(
                status_code=413,
                detail=f"File '{file.filename}' too large ({size_mb:.1f}MB). Limit is {settings.max_upload_mb}MB.",
            )

        safe_name = sanitize_filename(file.filename)
        dest_path = upload_path_for(dataset_id, safe_name)
        base_table_name = filename_to_table_name(safe_name)
        table_name = base_table_name

        existing_tables = duckdb_manager.get_table_names(dataset_id)
        if table_name in existing_tables:
            counter = 2
            while f"{base_table_name}_{counter}" in existing_tables:
                counter += 1
            table_name = f"{base_table_name}_{counter}"

        try:
            dest_path.write_bytes(contents)

            conn = duckdb_manager.create_connection(dataset_id, table_name=table_name, read_only=False)
            is_multi = len(files) > 1 or len(existing_tables) > 0
            file_loader.load(conn, dest_path, safe_name, create_alias=not is_multi)

            schema: DatasetSchema = extract_table_schema(conn, dataset_id, table_name)

            safe_table = escape_sql_identifier(table_name)
            preview_rows = conn.execute(
                f"SELECT * FROM {safe_table} LIMIT 100"
            ).fetch_df().to_dict(orient="records")

            file_infos.append(
                FileInfo(
                    filename=safe_name,
                    file_type=safe_name.split(".")[-1],
                    table_name=table_name,
                    **{"schema": schema},
                    preview_rows=preview_rows,
                )
            )

        except UnsupportedFileError as exc:
            logger.warning("Unsupported file: %s", exc)
            if len(files) == 1:
                if len(duckdb_manager.get_table_names(dataset_id)) == 0:
                    duckdb_manager.drop_connection(dataset_id)
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surfaced as a clean 400 to the client
            logger.exception("File processing failed for %s: %s", safe_name, exc)
            if dest_path.exists():
                dest_path.unlink(missing_ok=True)
            if len(files) == 1:
                if len(duckdb_manager.get_table_names(dataset_id)) == 0:
                    duckdb_manager.drop_connection(dataset_id)
                raise HTTPException(status_code=400, detail=f"Failed to process file '{safe_name}': {exc}") from exc

    if not file_infos:
        if len(duckdb_manager.get_table_names(dataset_id)) == 0:
            duckdb_manager.drop_connection(dataset_id)
        raise HTTPException(status_code=400, detail="Failed to process uploaded files. Please verify file formats.")

    # Record persistent ownership for the authenticated user
    record_dataset_ownership(
        dataset_id=dataset_id,
        user=current_user,
        filenames=[f.filename for f in file_infos],
        table_names=[f.table_name for f in file_infos],
    )

    # Transition persistent DuckDB database to read-only mode so multiple workers can query concurrently
    duckdb_manager.transition_to_read_only(dataset_id)

    return UploadResponse(
        dataset_id=dataset_id,
        files=file_infos,
    )

