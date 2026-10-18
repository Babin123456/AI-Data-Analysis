"""Comprehensive automated tests for the refactored AI chat pipeline.

Covers all 15 required scenarios:
1. Greeting returns text without SQL.
2. Valid SQL query returns answer and structured results.
3. Dataset explanation is handled without unnecessary SQL execution.
4. Off-topic request returns guidance.
5. Ambiguous request asks for clarification.
6. Invalid column is handled safely.
7. Incompatible data type / invalid aggregation produces useful response.
8. SQL injection and prohibited SQL operations are rejected.
9. SQL repair stops after configured retry limit (2).
10. Empty results do not cause errors.
11. DuckDB exceptions do not leak internal details.
12. Null SQL does not produce an empty SQL block.
13. Follow-up queries retain conversation context.
14. Unauthorized / non-existent dataset access is rejected.
15. LLM timeouts and service errors are handled gracefully.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from app.db.duckdb_manager import duckdb_manager
from app.main import app
from app.models.schemas import DatasetSchema
from app.services.chat_session_service import chat_session_service
from app.services.dataset_explainer import explain_dataset
from app.services.intent_service import intent_service
from app.services.llm_service import LLMServiceError, llm_service
from app.services.schema_service import extract_all_schemas
from app.services.sql_pipeline import sql_pipeline
from app.utils.auth import get_current_user
from app.validation.sql_validator import SQLValidationError, validate_sql

TEST_DATASET_ID = "test_chat_dataset_123"
TEST_TABLE_NAME = "sales_records"


async def mock_auth_user():
    return {"username": "analyst_test", "email": "analyst@test.com", "role": "user"}


@pytest.fixture(scope="module", autouse=True)
def setup_test_dataset():
    """Create in-memory DuckDB dataset for test assertions."""
    duckdb_manager.set_dataset_owner(TEST_DATASET_ID, "analyst_test")
    conn = duckdb_manager.create_connection(TEST_DATASET_ID, table_name=TEST_TABLE_NAME)
    conn.execute(f"""
        CREATE TABLE {TEST_TABLE_NAME} (
            id INTEGER,
            category VARCHAR,
            region VARCHAR,
            amount DOUBLE,
            quantity INTEGER
        )
    """)
    conn.execute(f"""
        INSERT INTO {TEST_TABLE_NAME} VALUES
        (1, 'Electronics', 'North', 250.0, 2),
        (2, 'Furniture', 'South', 150.0, 1),
        (3, 'Electronics', 'East', 400.0, 4),
        (4, 'Clothing', 'West', 75.0, 3),
        (5, 'Furniture', 'North', 300.0, 2)
    """)
    yield
    duckdb_manager.drop_connection(TEST_DATASET_ID)


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = mock_auth_user
    with patch("app.api.routes.chat.chat_limiter.check_rate_limit"):
        with TestClient(app) as test_client:
            yield test_client
    app.dependency_overrides.pop(get_current_user, None)


# 1. Greeting returns text without SQL
def test_greeting_returns_text_without_sql(client):
    res = client.post("/api/chat", json={
        "dataset_id": TEST_DATASET_ID,
        "question": "Hello there!",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "greeting"
    assert data["response_type"] == "text"
    assert data["sql"] is None
    assert data["rows"] == []
    assert "Hello" in data["answer"] or "help" in data["answer"].lower()


# 2. Valid SQL query returns answer and structured results
def test_valid_sql_query_returns_answer_and_results(client):
    valid_sql = f"SELECT category, SUM(amount) AS total FROM {TEST_TABLE_NAME} GROUP BY category"
    with patch.object(llm_service, "generate_sql", return_value=valid_sql), \
         patch.object(llm_service, "explain_results", return_value="Here is the total sales grouped by category."):
        res = client.post("/api/chat", json={
            "dataset_id": TEST_DATASET_ID,
            "question": "What is the total sales for each category?",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["intent"] == "sql_query"
        assert data["response_type"] == "data"
        assert data["sql"] is not None
        assert "category" in data["columns"]
        assert "total" in data["columns"]
        assert len(data["rows"]) == 3  # Electronics, Furniture, Clothing


# 3. Dataset explanation is handled without unnecessary SQL execution
def test_dataset_explanation_handled_without_sql(client):
    with patch.object(sql_pipeline, "run") as mock_sql_run:
        res = client.post("/api/chat", json={
            "dataset_id": TEST_DATASET_ID,
            "question": "What columns are in this dataset?",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["intent"] == "dataset_explanation"
        assert data["response_type"] == "text"
        assert data["sql"] is None
        assert data["rows"] == []
        assert "category" in data["answer"].lower()
        # Ensure SQL pipeline was not called
        mock_sql_run.assert_not_called()


# 4. Off-topic request returns guidance
def test_off_topic_returns_guidance(client):
    res = client.post("/api/chat", json={
        "dataset_id": TEST_DATASET_ID,
        "question": "tell me a joke about cats",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "off_topic"
    assert data["response_type"] == "text"
    assert data["sql"] is None
    assert data["off_topic"] is True
    assert "data" in data["answer"].lower()


# 5. Ambiguous request asks for clarification
def test_ambiguous_request_asks_for_clarification(client):
    res = client.post("/api/chat", json={
        "dataset_id": TEST_DATASET_ID,
        "question": "why?",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["intent"] == "clarification"
    assert data["response_type"] == "clarification"
    assert data["sql"] is None
    assert "specify" in data["answer"].lower() or "which" in data["answer"].lower()


# 6. Invalid column is handled safely
def test_invalid_column_handled_safely(client):
    bad_sql = f"SELECT non_existent_column_xyz FROM {TEST_TABLE_NAME}"
    with patch.object(llm_service, "generate_sql", return_value=bad_sql), \
         patch.object(sql_pipeline, "_attempt_repair", return_value=None):
        res = client.post("/api/chat", json={
            "dataset_id": TEST_DATASET_ID,
            "question": "Show the non_existent_column_xyz",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["intent"] == "sql_query"
        assert data["response_type"] == "error"
        assert data["sql"] is None
        assert "non_existent_column_xyz" in data["answer"] or "column" in data["answer"].lower()
        # Verify no internal stack traces leaked
        assert "Traceback" not in data["answer"]
        assert "duckdb" not in data["answer"].lower() or "table" in data["answer"].lower()


# 7. Incompatible data type produces useful response
def test_incompatible_data_type_aggregation(client):
    bad_type_sql = f"SELECT ln(category) FROM {TEST_TABLE_NAME}"
    with patch.object(llm_service, "generate_sql", return_value=bad_type_sql), \
         patch.object(sql_pipeline, "_attempt_repair", return_value=None):
        res = client.post("/api/chat", json={
            "dataset_id": TEST_DATASET_ID,
            "question": "Calculate the natural log of category",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["intent"] == "sql_query"
        assert data["response_type"] == "error"
        assert data["sql"] is None
        assert "incompatible" in data["answer"].lower() or "unsupported" in data["answer"].lower() or "error" in data["answer"].lower()


# 8. SQL injection and prohibited SQL operations are rejected
def test_sql_injection_and_prohibited_operations_rejected(client):
    malicious_attempts = [
        f"DROP TABLE {TEST_TABLE_NAME}",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM information_schema.tables",
        "ATTACH 'hack.db' AS hack",
        f"SELECT * FROM {TEST_TABLE_NAME}; DROP TABLE {TEST_TABLE_NAME}",
    ]
    for mal_query in malicious_attempts:
        with pytest.raises(SQLValidationError):
            validate_sql(mal_query, allowed_tables=[TEST_TABLE_NAME])

        with patch.object(llm_service, "generate_sql", return_value=mal_query):
            res = client.post("/api/chat", json={
                "dataset_id": TEST_DATASET_ID,
                "question": "hack the database",
            })
            assert res.status_code == 200
            data = res.json()
            assert data["sql"] is None
            assert data["response_type"] in ("error", "text")
            assert "only help you analyze" in data["answer"] or "safety constraints" in data["answer"]


# 9. SQL repair stops after configured retry limit (2)
def test_sql_repair_stops_after_retry_limit(client):
    bad_sql = f"SELECT invalid_one FROM {TEST_TABLE_NAME}"
    repair_count = 0

    def mock_repair(**kwargs):
        nonlocal repair_count
        repair_count += 1
        return f"SELECT still_invalid_{repair_count} FROM {TEST_TABLE_NAME}"

    with patch.object(llm_service, "generate_sql", return_value=bad_sql), \
         patch.object(sql_pipeline, "_attempt_repair", side_effect=mock_repair):
        res = client.post("/api/chat", json={
            "dataset_id": TEST_DATASET_ID,
            "question": "Show invalid columns",
        })
        assert res.status_code == 200
        data = res.json()
        # Exactly 2 repairs attempted before termination
        assert repair_count == 2
        assert data["response_type"] == "error"
        assert data["sql"] is None


# 10. Empty results do not cause errors
def test_empty_results_do_not_cause_errors(client):
    empty_sql = f"SELECT * FROM {TEST_TABLE_NAME} WHERE amount > 1000000"
    with patch.object(llm_service, "generate_sql", return_value=empty_sql):
        res = client.post("/api/chat", json={
            "dataset_id": TEST_DATASET_ID,
            "question": "Show sales above 1 million",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["response_type"] == "data"
        assert data["sql"] == empty_sql
        assert data["rows"] == []
        assert "no records" in data["answer"].lower() or "found" in data["answer"].lower()


# 11. DuckDB exceptions do not leak internal details
def test_duckdb_exceptions_do_not_leak_internal_details(client):
    error_sql = f"SELECT * FROM {TEST_TABLE_NAME} WHERE amount = 'non_numeric'"
    with patch.object(llm_service, "generate_sql", return_value=error_sql), \
         patch.object(sql_pipeline, "_attempt_repair", return_value=None):
        res = client.post("/api/chat", json={
            "dataset_id": TEST_DATASET_ID,
            "question": "Filter amount by string",
        })
        assert res.status_code == 200
        data = res.json()
        answer = data["answer"]
        assert "C:\\" not in answer
        assert "/home/" not in answer
        assert "/tmp/" not in answer
        assert "Traceback" not in answer


# 12. Null SQL does not produce empty SQL block
def test_null_sql_does_not_produce_empty_sql_block(client):
    res = client.post("/api/chat", json={
        "dataset_id": TEST_DATASET_ID,
        "question": "Hello!",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["sql"] is None
    assert data["rows"] == []


# 13. Follow-up queries retain conversation context
def test_follow_up_retains_conversation_context(client):
    session_id = "test_session_followup_99"
    chat_session_service.clear_session(TEST_DATASET_ID, session_id)

    # First turn
    chat_session_service.record_turn(
        dataset_id=TEST_DATASET_ID,
        session_id=session_id,
        question="Show sales by category",
        intent="sql_query",
        sql=f"SELECT category, sum(amount) FROM {TEST_TABLE_NAME} GROUP BY category",
        answer="Here are the sales by category.",
    )

    # Context summary should retain previous question and SQL
    context = chat_session_service.get_context_summary(TEST_DATASET_ID, session_id)
    assert "Show sales by category" in context
    assert "SELECT category, sum(amount)" in context


# 14. Unauthorized dataset access is rejected
def test_unauthorized_dataset_access_rejected(client):
    # Non-existent or expired dataset returns 404
    res = client.post("/api/chat", json={
        "dataset_id": "non_existent_dataset_999",
        "question": "What is the total sales?",
    })
    assert res.status_code == 404
    data = res.json()
    assert "not found" in data["detail"].lower() or "expired" in data["detail"].lower()

    # Unauthenticated client (no token) returns 401 when dependency override is removed
    saved_override = app.dependency_overrides.pop(get_current_user, None)
    try:
        raw_res = client.post("/api/chat", json={
            "dataset_id": TEST_DATASET_ID,
            "question": "What is the total sales?",
        })
        assert raw_res.status_code == 401
    finally:
        if saved_override:
            app.dependency_overrides[get_current_user] = saved_override



# 15. LLM timeouts and service errors are handled gracefully
def test_llm_timeouts_handled_gracefully(client):
    with patch.object(intent_service, "classify", return_value=("sql_query", "")), \
         patch.object(llm_service, "generate_sql", side_effect=LLMServiceError("Groq API connection timed out")):
        res = client.post("/api/chat", json={
            "dataset_id": TEST_DATASET_ID,
            "question": "Calculate complex metrics",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["response_type"] == "error"
        assert data["sql"] is None
        assert "unable to generate" in data["answer"].lower() or "rephrase" in data["answer"].lower()
