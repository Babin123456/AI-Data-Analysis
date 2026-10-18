import asyncio
from contextlib import asynccontextmanager
from fastapi import APIRouter, FastAPI, Depends, Response
from fastapi.middleware.cors import CORSMiddleware

import logging
from app.api.routes import chat, dataset, download, explore, upload, auth, sql_editor, admin
from app.config import settings
from app.db.duckdb_manager import duckdb_manager
from app.db.mongodb import close_mongo_connection, connect_to_mongo
from app.services.schema_service import invalidate_schema_cache
from app.utils.auth import get_current_user
from app.utils.logger_streamer import memory_log_handler

# Attach memory log handler to root logger to capture system logs
root_logger = logging.getLogger()
if memory_log_handler not in root_logger.handlers:
    root_logger.addHandler(memory_log_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ─────────────────────────────────────────────────────────────
    try:
        connect_to_mongo()
    except Exception as exc:
        if settings.app_env == "testing" and not settings.mongodb_required:
            root_logger.warning("MongoDB connection skipped in testing environment: %s", exc)
        else:
            root_logger.error("Failed to connect to MongoDB during startup: %s", exc)
            raise

    cleanup_task = asyncio.create_task(duckdb_manager.start_periodic_cleanup())
    root_logger.info("Application startup complete: DuckDB cleanup worker started.")

    yield

    # ── Shutdown ────────────────────────────────────────────────────────────
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    duckdb_manager.close_all_connections()
    close_mongo_connection()
    root_logger.info("Application shutdown complete: database connections closed.")


app = FastAPI(
    title="AI Data Analysis API",
    description="Upload structured data and analyze it with natural language.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.rstrip("/") for origin in settings.cors_origins if origin],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Public (no-auth) routes ──────────────────────────────────────────────────
# navigator.sendBeacon() cannot attach Authorization headers, so the cleanup
# endpoint must be reachable without a JWT token.
_public_dataset_router = APIRouter(prefix="/api/dataset", tags=["dataset"])


@_public_dataset_router.post("/{dataset_id}/cleanup", status_code=204)
async def cleanup_dataset(dataset_id: str) -> Response:
    """Fire-and-forget cleanup called via navigator.sendBeacon() on tab close.

    Returns 204 No Content so the browser does not need to read the response body.
    Silently ignores unknown dataset_ids (beacon may arrive after a server restart).
    """
    if duckdb_manager.exists(dataset_id):
        duckdb_manager.drop_connection(dataset_id)
        invalidate_schema_cache(dataset_id)
    return Response(status_code=204)


app.include_router(_public_dataset_router)

# ── Authenticated routes ─────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(upload.router, dependencies=[Depends(get_current_user)])
app.include_router(explore.router, dependencies=[Depends(get_current_user)])
app.include_router(chat.router, dependencies=[Depends(get_current_user)])
app.include_router(dataset.router, dependencies=[Depends(get_current_user)])
app.include_router(download.router, dependencies=[Depends(get_current_user)])
app.include_router(sql_editor.router, dependencies=[Depends(get_current_user)])


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "env": settings.app_env}
