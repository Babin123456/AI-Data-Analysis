"""MongoDB database client initialization with resilient connection handling."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from pymongo import MongoClient

from app.config import settings

logger = logging.getLogger(__name__)

# Import the Bloom Filter singleton.
from app.utils.bloom_filter import username_bloom_filter  # noqa: E402


class _DummyResult:
    def __init__(self, inserted_id: Any = None, modified_count: int = 0, deleted_count: int = 0):
        self.inserted_id = inserted_id
        self.modified_count = modified_count
        self.deleted_count = deleted_count
        self.upserted_id = None


class _DummyCollection:
    def __init__(self, name: str):
        self.name = name

    def find(self, *args, **kwargs):
        return []

    def find_one(self, *args, **kwargs):
        return None

    def insert_one(self, *args, **kwargs):
        return _DummyResult()

    def update_one(self, *args, **kwargs):
        return _DummyResult()

    def update_many(self, *args, **kwargs):
        return _DummyResult()

    def delete_one(self, *args, **kwargs):
        return _DummyResult()

    def delete_many(self, *args, **kwargs):
        return _DummyResult()

    def create_index(self, *args, **kwargs):
        pass


class MongoClientProxy:
    """Lazy proxy for MongoClient that avoids crashing on import when offline or testing."""

    def __init__(self) -> None:
        self._client: MongoClient | None = None

    def _get_client(self) -> MongoClient | None:
        if self._client is None:
            try:
                self._client = MongoClient(
                    settings.mongodb_uri,
                    serverSelectionTimeoutMS=2000,
                    connectTimeoutMS=2000,
                )
            except Exception as exc:
                logger.warning("MongoDB connection could not be established: %s", exc)
                return None
        return self._client

    def __getattr__(self, name: str) -> Any:
        c = self._get_client()
        if c is not None:
            return getattr(c, name)
        raise RuntimeError("MongoDB client is not available.")

    def __getitem__(self, name: str) -> Any:
        c = self._get_client()
        if c is not None:
            return c[name]
        return MongoDatabaseProxy(self)


class MongoDatabaseProxy:
    """Lazy proxy for Mongo Database that falls back gracefully when offline."""

    def __init__(self, client_proxy: MongoClientProxy) -> None:
        self._client_proxy = client_proxy

    def __getitem__(self, collection_name: str) -> Any:
        c = self._client_proxy._get_client()
        if c is not None:
            try:
                return c[settings.database_name][collection_name]
            except Exception as exc:
                logger.warning("Error accessing collection %s: %s", collection_name, exc)
        return _DummyCollection(collection_name)

    def __getattr__(self, name: str) -> Any:
        c = self._client_proxy._get_client()
        if c is not None:
            return getattr(c[settings.database_name], name)
        return _DummyCollection(name)


# Resilient singleton proxies
client: Any = MongoClientProxy()
db: Any = MongoDatabaseProxy(client)


def init_db() -> None:
    """Initialize database indexes and verify connection."""
    try:
        raw_client = client._get_client()
        if raw_client is None:
            logger.warning("MongoDB client is not connected; skipping index initialization.")
            return

        # Test connection
        raw_client.admin.command("ping")
        logger.info("Successfully connected to MongoDB.")

        real_db = raw_client[settings.database_name]

        users = real_db["users"]
        # Unique indexes
        users.create_index("username", unique=True)
        users.create_index("email", unique=True)
        users.create_index("firebase_uid", unique=True, sparse=True)

        # Performance indexes for Admin listing, search, and sorting
        users.create_index([("created_at", -1)])
        users.create_index([("role", 1), ("created_at", -1)])

        # Email verifications: lookup by user_id + used + created_at
        email_verifications = real_db["email_verifications"]
        email_verifications.create_index("user_id")
        email_verifications.create_index([("user_id", 1), ("used", 1), ("created_at", -1)])
        email_verifications.create_index("expires_at", expireAfterSeconds=600)

        # Password resets: lookup by user_id + used + created_at
        password_resets = real_db["password_resets"]
        password_resets.create_index("user_id")
        password_resets.create_index([("user_id", 1), ("used", 1), ("created_at", -1)])
        password_resets.create_index("expires_at", expireAfterSeconds=300)

        # Groq API Usage collection
        groq_usage = real_db["groq_usage"]
        groq_usage.create_index("date", unique=True)

        # Rate limiter collection (MongoDB-backed, multi-worker safe)
        rate_limits = real_db["rate_limits"]
        rate_limits.create_index("expires_at", expireAfterSeconds=0)
        rate_limits.create_index([("key", 1), ("window_start", 1)])

        # OTP failure lockout collection
        otp_lockouts = real_db["otp_lockouts"]
        otp_lockouts.create_index("expires_at", expireAfterSeconds=0)
        otp_lockouts.create_index("email", unique=True)

        # Datasets ownership collection (persistent multi-worker dataset registry)
        datasets = real_db["datasets"]
        datasets.create_index("dataset_id", unique=True)
        datasets.create_index("user_id")

        logger.info("MongoDB unique, performance, dataset, and TTL indexes initialized successfully.")

        # Seed the Bloom Filter
        username_bloom_filter.load_from_db()

        # Seed demo admin account into DB (only if it doesn't exist yet)
        _seed_demo_admin()

    except Exception as e:
        logger.warning(f"Failed to initialize MongoDB connection or indexes (continuing safely): {e}")


def _seed_demo_admin() -> None:
    """Create the demo admin account in MongoDB on first startup."""
    from app.utils.auth import hash_password

    demo_email = settings.demo_admin_email.strip().lower()
    demo_password = settings.demo_admin_password or "admin123"

    try:
        result = db["users"].update_one(
            {"email": demo_email},
            {
                "$setOnInsert": {
                    "username": "admin_demo",
                    "email": demo_email,
                    "hashed_password": hash_password(demo_password),
                    "role": "admin",
                    "is_admin": True,
                    "is_active": True,
                    "is_verified": True,
                    "created_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        if result and getattr(result, "upserted_id", None):
            logger.info("Demo admin account seeded into MongoDB (first-time setup).")
    except Exception as exc:
        logger.warning("Failed to seed demo admin account: %s", exc)


def track_groq_usage(call_type: str = "chat", tokens_estimated: int = 150) -> None:
    """Record daily Groq API usage in MongoDB groq_usage collection."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db.groq_usage.update_one(
            {"date": today},
            {
                "$inc": {
                    "total_calls": 1,
                    f"calls.{call_type}": 1,
                    "tokens_estimated": tokens_estimated,
                },
                "$set": {
                    "updated_at": datetime.now(timezone.utc),
                },
            },
            upsert=True,
        )
    except Exception as exc:
        logger.warning(f"Failed to track Groq usage: {exc}")


# Trigger DB initialization safely without crashing on startup
try:
    init_db()
except Exception as exc:
    logger.warning("Initial init_db invocation skipped: %s", exc)
