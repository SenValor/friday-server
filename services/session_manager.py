"""
services/session_manager.py
----------------------------
Manages in-memory WebSocket sessions.

Each browser tab that connects gets a `FridaySession` object which:
  - Holds conversation history (for multi-turn Gemini context)
  - Tracks the user's identity
  - Provides a helper to send events back over the WebSocket
  - Stores a GeminiLiveSession stub for Phase 2

The SessionManager singleton owns all active sessions and cleans up on
disconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket

from models.events import OutboundEvent, to_dict
from services.gemini_live import GeminiLiveSession

log = logging.getLogger(__name__)


# ─── Per-connection session ───────────────────────────────────────────────────

@dataclass
class FridaySession:
    id: str
    websocket: WebSocket
    user_id: str
    user_name: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Gemini conversation history (list of {"role": ..., "parts": [...]})
    history: list[dict] = field(default_factory=list)

    # Phase-2 live audio session (kept as None until activated)
    live: Optional[GeminiLiveSession] = None

    # Counts for the current WebSocket connection
    message_count: int = 0

    async def send(self, event: OutboundEvent) -> None:
        """Serialise and send an outbound event over the WebSocket."""
        try:
            await self.websocket.send_text(json.dumps(to_dict(event)))
        except Exception as exc:
            log.warning("send() failed on session %s: %s", self.id, exc)

    def add_turn(self, role: str, text: str) -> None:
        """Append a turn to the Gemini history buffer."""
        self.history.append({"role": role, "parts": [{"text": text}]})
        # Keep last 20 turns (10 exchanges) to stay within token budget
        if len(self.history) > 20:
            self.history = self.history[-20:]

    def bump(self) -> None:
        self.message_count += 1


# ─── Singleton manager ────────────────────────────────────────────────────────

class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, FridaySession] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        websocket: WebSocket,
        user_id: str = "simge",
        user_name: str = "Simge",
    ) -> FridaySession:
        async with self._lock:
            session_id = str(uuid.uuid4())
            session = FridaySession(
                id=session_id,
                websocket=websocket,
                user_id=user_id,
                user_name=user_name,
            )
            self._sessions[session_id] = session
            log.info("Session created: %s (user=%s)", session_id, user_name)
            return session

    async def remove(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session and session.live:
                try:
                    await session.live.close()
                except Exception:
                    pass
            if session:
                log.info("Session removed: %s", session_id)

    def get(self, session_id: str) -> Optional[FridaySession]:
        return self._sessions.get(session_id)

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    def all_sessions(self) -> list[FridaySession]:
        return list(self._sessions.values())


# Global singleton — imported by server.py
session_manager = SessionManager()
