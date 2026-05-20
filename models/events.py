"""
models/events.py
----------------
Pydantic models for every WebSocket event that flows between
the frontend and the backend.

Direction:
  IN  → messages sent by the browser to the server
  OUT → messages sent by the server to the browser
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional, Union
from pydantic import BaseModel, Field


# ─── Shared enums ─────────────────────────────────────────────────────────────

class FridayState(str, Enum):
    IDLE      = "idle"
    LISTENING = "listening"
    THINKING  = "thinking"
    SPEAKING  = "speaking"
    WORKING   = "working"
    ERROR     = "error"


class MessageStatus(str, Enum):
    SOLVED      = "solved"
    NEEDS_HELP  = "needs_help"
    PENDING     = "pending"


class DifficultyLevel(str, Enum):
    EASY   = "easy"
    MEDIUM = "medium"
    HARD   = "hard"


class TaskStatus(str, Enum):
    WORKING   = "working"
    DONE      = "done"
    FAILED    = "failed"


# ─── Inbound events (browser → server) ───────────────────────────────────────

class UserMessageEvent(BaseModel):
    """Text message sent by Simge."""
    type: Literal["user_message"] = "user_message"
    content: str
    userId: str = "simge"
    userName: str = "Simge"
    project: str = "FRIDAY"
    sessionId: Optional[str] = None


class AudioChunkEvent(BaseModel):
    """Raw PCM audio chunk from the microphone (Phase 2)."""
    type: Literal["audio_chunk"] = "audio_chunk"
    # base64-encoded audio bytes
    data: str
    userId: str = "simge"
    sessionId: Optional[str] = None


class PingEvent(BaseModel):
    type: Literal["ping"] = "ping"


# Union of all inbound event types
InboundEvent = Union[UserMessageEvent, AudioChunkEvent, PingEvent]


# ─── Outbound events (server → browser) ──────────────────────────────────────

class StateEvent(BaseModel):
    """Notifies the UI to transition FRIDAY's state machine."""
    type: Literal["state"] = "state"
    state: FridayState


class AssistantMessageEvent(BaseModel):
    """Full text response from Gemini / FRIDAY."""
    type: Literal["assistant_message"] = "assistant_message"
    content: str
    topic: Optional[str] = None
    status: MessageStatus = MessageStatus.SOLVED
    difficultyLevel: DifficultyLevel = DifficultyLevel.EASY
    messageId: Optional[str] = None


class AssistantTokenEvent(BaseModel):
    """Streaming token (Phase 2 — for incremental UI rendering)."""
    type: Literal["token"] = "token"
    delta: str


class TaskEvent(BaseModel):
    """Shows a step inside TaskProcessWindow."""
    type: Literal["task"] = "task"
    title: str
    description: Optional[str] = None
    status: TaskStatus = TaskStatus.WORKING


class SavedEvent(BaseModel):
    """Confirmation that the message was persisted to Firestore."""
    type: Literal["saved"] = "saved"
    status: Literal["success", "failed"] = "success"
    messageId: Optional[str] = None


class ErrorEvent(BaseModel):
    """Generic error notification."""
    type: Literal["error"] = "error"
    message: str
    code: Optional[str] = None


class PongEvent(BaseModel):
    type: Literal["pong"] = "pong"


class ConnectedEvent(BaseModel):
    """Sent immediately after a WebSocket connection is established."""
    type: Literal["connected"] = "connected"
    sessionId: str
    message: str = "FRIDAY bağlantısı kuruldu."


# Union of all outbound event types (for type hints)
OutboundEvent = Union[
    StateEvent,
    AssistantMessageEvent,
    AssistantTokenEvent,
    TaskEvent,
    SavedEvent,
    ErrorEvent,
    PongEvent,
    ConnectedEvent,
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def parse_inbound(raw: dict[str, Any]) -> InboundEvent:
    """
    Deserialise a raw dict (from JSON) into the correct inbound event model.
    Raises ValueError for unknown event types.
    """
    event_type = raw.get("type")
    mapping: dict[str, type[InboundEvent]] = {
        "user_message": UserMessageEvent,
        "audio_chunk":  AudioChunkEvent,
        "ping":         PingEvent,
    }
    cls = mapping.get(event_type)  # type: ignore[arg-type]
    if cls is None:
        raise ValueError(f"Unknown inbound event type: {event_type!r}")
    return cls(**raw)


def to_dict(event: BaseModel) -> dict[str, Any]:
    """Serialise an outbound event to a plain dict (for json.dumps)."""
    return event.model_dump(exclude_none=True)
