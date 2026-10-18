"""Thread-safe conversation session and context management for AI chat."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ChatTurn:
    question: str
    intent: str
    sql: str | None = None
    answer: str = ""
    timestamp: float = field(default_factory=time.time)


class ChatSessionService:
    """Stores recent conversation turns per session to support contextual follow-ups."""

    def __init__(self, max_turns_per_session: int = 10, ttl_seconds: int = 3600) -> None:
        self.max_turns_per_session = max_turns_per_session
        self.ttl_seconds = ttl_seconds
        # session_key -> list of ChatTurn
        self._history: dict[str, list[ChatTurn]] = {}
        # session_key -> last_accessed timestamp
        self._last_accessed: dict[str, float] = {}
        self._lock = Lock()

    def _make_key(self, dataset_id: str, session_id: str) -> str:
        return f"{dataset_id}:{session_id}"

    def record_turn(
        self,
        dataset_id: str,
        session_id: str | None,
        question: str,
        intent: str,
        sql: str | None = None,
        answer: str = "",
    ) -> None:
        if not session_id or not dataset_id:
            return

        key = self._make_key(dataset_id, session_id)
        turn = ChatTurn(
            question=question.strip(),
            intent=intent,
            sql=sql,
            answer=answer[:500],  # keep preview compact
        )

        with self._lock:
            self._cleanup_expired()
            if key not in self._history:
                self._history[key] = []
            self._history[key].append(turn)
            if len(self._history[key]) > self.max_turns_per_session:
                self._history[key].pop(0)
            self._last_accessed[key] = time.time()

    def get_context_summary(
        self,
        dataset_id: str,
        session_id: str | None,
        max_turns: int = 2,
    ) -> str:
        """Return a compact, token-efficient summary of recent conversation turns."""
        if not session_id or not dataset_id:
            return ""

        key = self._make_key(dataset_id, session_id)
        with self._lock:
            turns = self._history.get(key, [])
            if not turns:
                return ""
            self._last_accessed[key] = time.time()
            recent = turns[-max_turns:]

        lines: list[str] = ["Recent Conversation Context:"]
        for t in recent:
            lines.append(f"- User asked: {t.question}")
            if t.sql:
                lines.append(f"  SQL executed: {t.sql}")
            elif t.answer:
                lines.append(f"  Summary: {t.answer}")

        return "\n".join(lines)

    def get_last_sql(self, dataset_id: str, session_id: str | None) -> str | None:
        """Get the most recently executed SQL query in this session, if any."""
        if not session_id or not dataset_id:
            return None

        key = self._make_key(dataset_id, session_id)
        with self._lock:
            turns = self._history.get(key, [])
            for t in reversed(turns):
                if t.sql:
                    return t.sql
        return None

    def clear_session(self, dataset_id: str, session_id: str) -> None:
        key = self._make_key(dataset_id, session_id)
        with self._lock:
            self._history.pop(key, None)
            self._last_accessed.pop(key, None)

    def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [
            k for k, ts in self._last_accessed.items()
            if now - ts > self.ttl_seconds
        ]
        for k in expired:
            self._history.pop(k, None)
            self._last_accessed.pop(k, None)


chat_session_service = ChatSessionService()
