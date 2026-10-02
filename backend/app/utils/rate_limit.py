"""MongoDB-backed sliding window rate limiter.

Uses the existing MongoDB connection (no new dependencies) so rate-limit state
is:
  • Persistent across server restarts
  • Shared across all Uvicorn/Gunicorn worker processes
  • Consistent in multi-instance / cloud deployments (Render, Vercel, etc.)

The `rate_limits` collection stores one document per (key, window_start) pair
and uses a TTL index to auto-expire old records — no manual cleanup needed.

Collection schema per document:
  {
    "key":          "ip:1.2.3.4" | "user:email@example.com",
    "window_start": <datetime — truncated to current window>,
    "count":        <int — number of attempts in this window>,
    "expires_at":   <datetime — window_start + window_seconds, used by TTL index>
  }
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

# pyrefly: ignore [missing-import]
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


class MongoRateLimiter:
    """Sliding-window rate limiter backed by MongoDB.

    Parameters
    ----------
    max_requests : int
        Maximum number of requests allowed per window.
    window_seconds : int
        Duration of the sliding window in seconds.
    """

    def __init__(self, max_requests: int = 5, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._col = None   # lazy-loaded to avoid import-time circular issues

    def _get_col(self):
        """Lazy-load the MongoDB collection and ensure the TTL index exists."""
        if self._col is not None:
            return self._col
        # Deferred import — mongodb.py may not be fully initialized at import time
        from app.db.mongodb import db  # noqa: PLC0415
        col = db["rate_limits"]
        # TTL index: MongoDB automatically removes expired documents.
        # expireAfterSeconds=0 means "expire at the datetime stored in expires_at".
        try:
            col.create_index("expires_at", expireAfterSeconds=0, background=True)
            col.create_index([("key", 1), ("window_start", 1)], background=True)
        except Exception as exc:
            logger.warning("Rate limiter: could not create indexes: %s", exc)
        self._col = col
        return col

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def check_rate_limit(self, ip: str, username: str | None = None) -> None:
        """Check sliding-window rate limits for both IP and username.

        Raises HTTP 429 if the limit is exceeded for either key.
        Records a new attempt if the limit is not exceeded.
        """
        self._check_and_record(f"ip:{ip}")
        if username:
            self._check_and_record(f"user:{username.strip().lower()}")

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _check_and_record(self, key: str) -> None:
        """Atomically increment the counter for *key* and raise 429 if over limit."""
        from pymongo import ReturnDocument

        # Integer-based epoch calculation guarantees 0 microsecond drift across requests
        now_ts = int(datetime.now(timezone.utc).timestamp())
        window_start_ts = now_ts - (now_ts % self.window_seconds)
        window_start = datetime.fromtimestamp(window_start_ts, tz=timezone.utc)
        expires_at = window_start + timedelta(seconds=self.window_seconds * 2)

        col = self._get_col()
        try:
            result = col.find_one_and_update(
                {"key": key, "window_start": window_start},
                {
                    "$inc": {"count": 1},
                    "$setOnInsert": {
                        "key": key,
                        "window_start": window_start,
                        "expires_at": expires_at,
                    },
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            count = result.get("count", 1) if result else 1
        except Exception as exc:
            # If MongoDB is temporarily unavailable, fail open (allow the request)
            # rather than locking out all users.
            logger.error("Rate limiter MongoDB error (failing open): %s", exc)
            return

        if count > self.max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Too many login attempts. "
                    f"Please wait {self.window_seconds} seconds before trying again."
                ),
            )


# ── Singleton: 5 login attempts per 60 s — shared across all workers ──────────
login_limiter = MongoRateLimiter(max_requests=5, window_seconds=60)
