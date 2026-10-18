"""AI Chat endpoint: natural language question -> validated intent routing ->
safe SQL execution with bounded repair -> plain-English explanation + structured results.
"""
from __future__ import annotations

import logging
import time
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request

from app.db.duckdb_manager import duckdb_manager
from app.models.schemas import ChatRequest, ChatResponse
from app.services.chat_session_service import chat_session_service
from app.services.dataset_explainer import explain_dataset
from app.services.intent_service import intent_service
from app.services.schema_service import extract_all_schemas
from app.services.sql_pipeline import sql_pipeline
from app.utils.auth import get_current_user
from app.utils.dataset_auth import authorize_dataset_access
from app.utils.rate_limit import chat_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> ChatResponse:
    start_time = time.time()
    request_id = str(uuid.uuid4())

    # 1. Rate limiting (max 15 chat queries per minute per IP)
    client_ip = request.client.host if request.client else "unknown"
    chat_limiter.check_rate_limit(ip=client_ip)

    logger.info(
        "Chat request received [request_id=%s] dataset_id=%s session_id=%s q_len=%d",
        request_id,
        req.dataset_id,
        req.conversation_id,
        len(req.question),
    )

    # 2. Dataset Ownership Authorization & Retrieval
    authorize_dataset_access(req.dataset_id, current_user)

    try:
        conn = duckdb_manager.get_connection(req.dataset_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Dataset not found or expired.") from exc

    schemas = extract_all_schemas(conn, req.dataset_id)
    if not schemas:
        raise HTTPException(status_code=400, detail="No readable tables found in the active dataset.")

    # 3. Load Session Context
    session_id = req.conversation_id or ""
    context_summary = chat_session_service.get_context_summary(req.dataset_id, session_id)
    has_prior_context = bool(context_summary)

    # 4. Intent Classification
    intent, direct_reply = intent_service.classify(
        question=req.question,
        schemas=schemas,
        has_prior_context=has_prior_context,
    )

    logger.info(
        "Intent classified [request_id=%s] intent=%s has_prior=%s",
        request_id,
        intent,
        has_prior_context,
    )

    # 5. Route to appropriate handler based on classified intent

    # Handler A: Greeting / Pleasantries
    if intent == "greeting":
        answer = direct_reply or "Hello! 👋 How can I help you analyze your data today?"
        chat_session_service.record_turn(
            dataset_id=req.dataset_id,
            session_id=session_id,
            question=req.question,
            intent=intent,
            answer=answer,
        )
        return ChatResponse(
            answer=answer,
            intent="greeting",
            response_type="text",
            sql=None,
            columns=[],
            rows=[],
            table=[],
            session_id=session_id,
            dataset_id=req.dataset_id,
            request_id=request_id,
            off_topic=False,
        )

    # Handler B: Off-topic Request
    if intent == "off_topic":
        answer = direct_reply or "I am an AI Data Analyst specialized in querying your uploaded data. Please ask a question related to your dataset."
        chat_session_service.record_turn(
            dataset_id=req.dataset_id,
            session_id=session_id,
            question=req.question,
            intent=intent,
            answer=answer,
        )
        return ChatResponse(
            answer=answer,
            intent="off_topic",
            response_type="text",
            sql=None,
            columns=[],
            rows=[],
            table=[],
            session_id=session_id,
            dataset_id=req.dataset_id,
            request_id=request_id,
            off_topic=True,
        )

    # Handler C: Dataset Explanation / Schema Questions
    if intent == "dataset_explanation":
        answer = explain_dataset(schemas=schemas, question=req.question)
        chat_session_service.record_turn(
            dataset_id=req.dataset_id,
            session_id=session_id,
            question=req.question,
            intent=intent,
            answer=answer,
        )
        return ChatResponse(
            answer=answer,
            intent="dataset_explanation",
            response_type="text",
            sql=None,
            columns=[],
            rows=[],
            table=[],
            session_id=session_id,
            dataset_id=req.dataset_id,
            request_id=request_id,
            off_topic=False,
        )

    # Handler D: Clarification Needed
    if intent == "clarification":
        answer = direct_reply or "Could you please specify which columns or criteria you would like to analyze?"
        chat_session_service.record_turn(
            dataset_id=req.dataset_id,
            session_id=session_id,
            question=req.question,
            intent=intent,
            answer=answer,
        )
        return ChatResponse(
            answer=answer,
            intent="clarification",
            response_type="clarification",
            sql=None,
            columns=[],
            rows=[],
            table=[],
            session_id=session_id,
            dataset_id=req.dataset_id,
            request_id=request_id,
            off_topic=False,
        )

    # Handler E: SQL Query Execution & Repair Pipeline
    pipeline_result = sql_pipeline.run(
        conn=conn,
        dataset_id=req.dataset_id,
        question=req.question,
        schemas=schemas,
        target_table=req.table_name,
        conversation_context=context_summary,
    )

    elapsed_ms = (time.time() - start_time) * 1000
    logger.info(
        "SQL pipeline completed [request_id=%s] success=%s empty=%s duration_ms=%.1f",
        request_id,
        pipeline_result.success,
        pipeline_result.is_empty,
        elapsed_ms,
    )

    if pipeline_result.success:
        chat_session_service.record_turn(
            dataset_id=req.dataset_id,
            session_id=session_id,
            question=req.question,
            intent="sql_query",
            sql=pipeline_result.sql,
            answer=pipeline_result.answer,
        )
        return ChatResponse(
            answer=pipeline_result.answer,
            intent="sql_query",
            response_type="data",
            sql=pipeline_result.sql,
            columns=pipeline_result.columns,
            rows=pipeline_result.rows,
            table=pipeline_result.rows,
            session_id=session_id,
            dataset_id=req.dataset_id,
            request_id=request_id,
            off_topic=False,
        )

    # Execution / Validation failure: strictly separated from intent errors
    chat_session_service.record_turn(
        dataset_id=req.dataset_id,
        session_id=session_id,
        question=req.question,
        intent="sql_query",
        sql=None,
        answer=pipeline_result.answer,
    )
    return ChatResponse(
        answer=pipeline_result.answer,
        intent="sql_query",
        response_type="error",
        sql=None,
        columns=[],
        rows=[],
        table=[],
        error=pipeline_result.error_info,
        warning=pipeline_result.error_info.get("message") if pipeline_result.error_info else None,
        session_id=session_id,
        dataset_id=req.dataset_id,
        request_id=request_id,
        off_topic=False,
    )
