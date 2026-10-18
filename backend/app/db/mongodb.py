"""MongoDB database client initialization and lifecycle management.

Provides explicit, reusable connect and disconnect routines managed by the FastAPI lifespan.
Zero network operations or index creations occur at module import time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from pymongo import MongoClient

from app.config import settings

logger = logging.getLogger(__name__)

# Import the Bloom Filter singleton.
from app.utils.bloom_filter import username_bloom_filter  # noqa: E402

_active_client: MongoClient | None = None


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
    """Proxy for MongoClient delegating to the lifespan-managed client connection."""

    def _get_client(self) -> MongoClient | None:
        global _active_client
        return _active_client

    def __getattr__(self, name: str) -> Any:
        c = self._get_client()
        if c is not None:
            return getattr(c, name)
        if settings.app_env == "testing":
            return getattr(MongoDatabaseProxy(self), name)
        raise RuntimeError("MongoDB client is not connected. Ensure FastAPI lifespan has started.")

    def __getitem__(self, name: str) -> Any:
        c = self._get_client()
        if c is not None:
            return c[name]
        return MongoDatabaseProxy(self)


class MongoDatabaseProxy:
    """Proxy for Mongo Database delegating to the lifespan-managed database."""

    def __init__(self, client_proxy: MongoClientProxy) -> None:
        self._client_proxy = client_proxy

    def __getitem__(self, collection_name: str) -> Any:
        c = self._client_proxy._get_client()
        if c is not None:
            try:
                return c[settings.database_name][collection_name]
            except Exception as exc:
                logger.warning("Error accessing MongoDB collection %s: %s", collection_name, exc)
        if settings.app_env == "testing":
            return _DummyCollection(collection_name)
        raise RuntimeError(
            f"MongoDB is not connected. Cannot access collection '{collection_name}'. "
            "Ensure the database client was initialized during application startup."
        )

    def __getattr__(self, name: str) -> Any:
        c = self._client_proxy._get_client()
        if c is not None:
            return getattr(c[settings.database_name], name)
        if settings.app_env == "testing":
            return _DummyCollection(name)
        raise RuntimeError(f"MongoDB is not connected. Cannot access attribute '{name}'.")


# Global proxy instances consumed across routes and services
client: Any = MongoClientProxy()
db: Any = MongoDatabaseProxy(client)


def connect_to_mongo() -> MongoClient | None:
    """Explicitly connect to MongoDB, verify the connection, and initialize indexes.

    Called during FastAPI lifespan startup for each worker process.
    Idempotent: returns existing client if already connected.
    """
    global _active_client
    if _active_client is not None:
        return _active_client

    try:
        raw_client = MongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=2000,
            connectTimeoutMS=2000,
        )
        # Verify connection
        raw_client.admin.command("ping")
        _active_client = raw_client
        logger.info("Successfully connected to MongoDB (%s).", settings.database_name)

        # Initialize required indexes and seeds
        init_db(raw_client)
        return _active_client
    except Exception as exc:
        _active_client = None
        if settings.app_env == "testing" and not settings.mongodb_required:
            logger.warning("MongoDB connection skipped in testing environment: %s", exc)
            return None
        logger.error("Failed to connect to MongoDB at startup (%s): %s", settings.mongodb_uri, exc)
        raise RuntimeError(f"Application startup failed: unable to connect to MongoDB: {exc}") from exc


def close_mongo_connection() -> None:
    """Explicitly close the MongoDB client connection during application shutdown.

    Called during FastAPI lifespan shutdown for each worker process.
    Idempotent.
    """
    global _active_client
    if _active_client is not None:
        try:
            _active_client.close()
            logger.info("MongoDB client connection closed cleanly.")
        except Exception as exc:
            logger.warning("Error closing MongoDB connection: %s", exc)
        finally:
            _active_client = None


def init_db(raw_client: MongoClient | None = None) -> None:
    """Initialize database indexes, seed Bloom Filter, and seed demo admin."""
    target_client = raw_client or _active_client
    if target_client is None:
        if settings.app_env == "testing":
            logger.debug("init_db skipped because no MongoDB client is active.")
            return
        raise RuntimeError("Cannot initialize MongoDB indexes: database client is not connected.")

    try:
        real_db = target_client[settings.database_name]

        # 1. Users collection
        users = real_db["users"]
        users.create_index("username", unique=True)
        users.create_index("email", unique=True)
        users.create_index("firebase_uid", unique=True, sparse=True)
        users.create_index([("created_at", -1)])
        users.create_index([("role", 1), ("created_at", -1)])

        # 2. Email verifications collection
        email_verifications = real_db["email_verifications"]
        email_verifications.create_index("user_id")
        email_verifications.create_index([("user_id", 1), ("used", 1), ("created_at", -1)])
        email_verifications.create_index("expires_at", expireAfterSeconds=600)

        # 3. Password resets collection
        password_resets = real_db["password_resets"]
        password_resets.create_index("user_id")
        password_resets.create_index([("user_id", 1), ("used", 1), ("created_at", -1)])
        password_resets.create_index("expires_at", expireAfterSeconds=300)

        # 4. Groq API Usage collection
        groq_usage = real_db["groq_usage"]
        groq_usage.create_index("date", unique=True)

        # 5. Rate limits collection
        rate_limits = real_db["rate_limits"]
        rate_limits.create_index("expires_at", expireAfterSeconds=0)
        rate_limits.create_index([("key", 1), ("window_start", 1)])

        # 6. OTP lockouts collection
        otp_lockouts = real_db["otp_lockouts"]
        otp_lockouts.create_index("expires_at", expireAfterSeconds=0)
        otp_lockouts.create_index("email", unique=True)

        # 7. Datasets collection
        datasets = real_db["datasets"]
        datasets.create_index("dataset_id", unique=True)
        datasets.create_index("user_id")

        logger.info("MongoDB unique, performance, dataset, and TTL indexes verified successfully.")

        # Seed the Bloom Filter
        username_bloom_filter.load_from_db()

        # Seed demo admin account into DB
        _seed_demo_admin(real_db)

    except Exception as exc:
        logger.error("Failed to initialize MongoDB indexes: %s", exc)
        raise


def _seed_demo_admin(real_db: Any) -> None:
    """Create the demo admin account in MongoDB on first startup."""
    from app.utils.auth import hash_password

    demo_email = settings.demo_admin_email.strip().lower()
    demo_password = settings.demo_admin_password or "admin123"

    try:
        result = real_db["users"].update_one(
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
        logger.warning("Failed to track Groq usage: %s", exc)
