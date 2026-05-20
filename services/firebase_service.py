"""
services/firebase_service.py
-----------------------------
Firebase Admin SDK wrapper.

- Lazy-initialises the app on first use so the import never crashes if
  credentials are missing (unit tests, CI, etc.).
- Provides typed helpers for every Firestore collection FRIDAY needs.
- All public methods are async-friendly: heavy Firestore I/O runs in
  asyncio's default thread-pool executor via asyncio.to_thread().
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore

from core.config import settings

log = logging.getLogger(__name__)

# ─── Firestore collection names ───────────────────────────────────────────────

USERS              = "users"
FRIDAY_SESSIONS    = "friday_sessions"
FRIDAY_MESSAGES    = "friday_messages"
FRIDAY_TASKS       = "friday_tasks"
INTERN_DAILY_LOGS  = "intern_daily_logs"


# ─── Lazy init ────────────────────────────────────────────────────────────────

_db: Optional[firestore.Client] = None  # type: ignore[type-arg]


def _get_db() -> firestore.Client:  # type: ignore[type-arg]
    global _db
    if _db is not None:
        return _db

    cred_path = Path(settings.FIREBASE_CREDENTIALS_PATH)
    try:
        if not firebase_admin._apps:  # avoid duplicate init
            if settings.FIREBASE_SERVICE_ACCOUNT_JSON:
                # Cloud deploy (Railway etc.): credentials passed as JSON string env var
                cred = credentials.Certificate(json.loads(settings.FIREBASE_SERVICE_ACCOUNT_JSON))
                firebase_admin.initialize_app(cred, {"projectId": settings.FIREBASE_PROJECT_ID})
                log.info("Firebase initialised from FIREBASE_SERVICE_ACCOUNT_JSON env var.")
            elif cred_path.exists():
                cred = credentials.Certificate(str(cred_path))
                firebase_admin.initialize_app(cred, {"projectId": settings.FIREBASE_PROJECT_ID})
            else:
                # Fallback: application default credentials (e.g. on GCP)
                firebase_admin.initialize_app(options={"projectId": settings.FIREBASE_PROJECT_ID})
                log.warning(
                    "firebase-service-account.json not found — using application default credentials."
                )
        _db = firestore.client()
        log.info("Firestore client initialised (project=%s)", settings.FIREBASE_PROJECT_ID)
    except Exception as exc:
        log.error("Firestore init failed: %s", exc)
        raise

    return _db


# ─── Public helpers ───────────────────────────────────────────────────────────

async def save_message(
    *,
    session_id: str,
    user_id: str,
    user_name: str,
    message: str,
    response: str,
    topic: str,
    project: str,
    difficulty_level: str = "easy",
    status: str = "solved",
) -> str:
    """
    Persist a completed Q&A pair to `friday_messages`.
    Returns the Firestore document ID.
    """
    doc_id = str(uuid.uuid4())
    payload = {
        "sessionId":       session_id,
        "userId":          user_id,
        "userName":        user_name,
        "role":            "intern",
        "message":         message,
        "response":        response,
        "topic":           topic,
        "project":         project,
        "difficultyLevel": difficulty_level,
        "status":          status,
        "createdAt":       datetime.now(timezone.utc),
    }

    def _write():
        _get_db().collection(FRIDAY_MESSAGES).document(doc_id).set(payload)

    try:
        await asyncio.to_thread(_write)
        log.debug("friday_messages/%s saved", doc_id)
    except Exception as exc:
        log.error("save_message failed: %s", exc)
        raise

    return doc_id


async def ensure_session(
    *,
    session_id: str,
    user_id: str,
    user_name: str,
) -> None:
    """
    Create or touch a `friday_sessions` document.
    Uses Firestore merge so repeated calls are idempotent.
    """
    def _write():
        _get_db().collection(FRIDAY_SESSIONS).document(session_id).set(
            {
                "userId":    user_id,
                "userName":  user_name,
                "startedAt": datetime.now(timezone.utc),
                "active":    True,
            },
            merge=True,
        )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        log.error("ensure_session failed: %s", exc)
        raise


async def close_session(session_id: str) -> None:
    """Mark a session as inactive when the WebSocket disconnects."""
    def _write():
        _get_db().collection(FRIDAY_SESSIONS).document(session_id).update(
            {"active": False, "endedAt": datetime.now(timezone.utc)}
        )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        # Non-critical — log and swallow
        log.warning("close_session failed (session_id=%s): %s", session_id, exc)


async def ensure_user(
    *,
    user_id: str,
    user_name: str,
    role: str = "intern",
) -> None:
    """Upsert a user document (only writes fields that don't already exist)."""
    def _write():
        doc_ref = _get_db().collection(USERS).document(user_id)
        doc_ref.set(
            {
                "userId":    user_id,
                "userName":  user_name,
                "role":      role,
                "updatedAt": datetime.now(timezone.utc),
            },
            merge=True,
        )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        log.warning("ensure_user failed: %s", exc)


async def append_daily_log_topic(
    *,
    user_id: str,
    user_name: str,
    date_str: str,       # "YYYY-MM-DD"
    topic: str,
    project: str,
    status: str,
) -> None:
    """
    Upsert today's intern_daily_logs document, incrementing counters.
    Uses Firestore ArrayUnion / Increment so concurrent writes stay safe.
    """
    from google.cloud.firestore_v1 import ArrayUnion, Increment  # type: ignore

    doc_id = f"{user_id}_{date_str}"

    def _write():
        doc_ref = _get_db().collection(INTERN_DAILY_LOGS).document(doc_id)
        update: dict = {
            "userId":         user_id,
            "userName":       user_name,
            "date":           date_str,
            "topics":         ArrayUnion([topic]),
            "projects":       ArrayUnion([project]),
            "totalQuestions": Increment(1),
            "updatedAt":      datetime.now(timezone.utc),
        }
        if status == "solved":
            update["solvedQuestions"] = Increment(1)
        if status == "needs_help":
            update["needsMasterReview"] = True
        doc_ref.set(update, merge=True)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        log.warning("append_daily_log_topic failed: %s", exc)


# ─── Health check ─────────────────────────────────────────────────────────────

async def ping() -> bool:
    """Returns True if Firestore is reachable."""
    try:
        def _check():
            _get_db().collection(FRIDAY_SESSIONS).limit(1).get()
        await asyncio.to_thread(_check)
        return True
    except Exception:
        return False
