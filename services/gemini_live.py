"""
services/gemini_live.py
-----------------------
All Gemini interaction lives here.

Phase 1  — generate_text_response()
    Sends a single user turn, streams the response back token-by-token,
    and returns the full text + parsed metadata.

Phase 2 (stub)  — GeminiLiveSession
    Skeleton for the native audio/multimodal live session.
    Swap the stub methods for real google-genai live calls when ready.

Meta extraction
    FRIDAY's system prompt instructs Gemini to append a JSON `_meta` line.
    We strip it before sending to the frontend and use it for Firestore.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from google import genai
from google.genai import types as genai_types

from core.config import settings
from core.persona import (
    FALLBACK_RESPONSES as _FALLBACK_RESPONSES,
    FALLBACK_DEFAULT as _FALLBACK_DEFAULT,
)

log = logging.getLogger(__name__)


# ─── Gemini hata sınıflandırması ─────────────────────────────────────────────

class GeminiQuotaError(Exception):
    """429 RESOURCE_EXHAUSTED — kota doldu."""

class GeminiPermissionError(Exception):
    """403 PERMISSION_DENIED — proje erişimi yok."""

class GeminiModelError(Exception):
    """404 NOT_FOUND — model bulunamadı."""


def _classify_gemini_error(exc: Exception) -> Exception:
    """
    google-genai kütüphanesi tüm API hatalarını tek bir exception tipiyle fırlatır.
    HTTP kodunu string içinde arayarak anlamlı alt sınıflara dönüştürüyoruz.
    """
    msg = str(exc)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        return GeminiQuotaError(
            "Gemini kotası doldu veya bu model için kota yok. "
            f"(model: {settings.GEMINI_MODEL})"
        )
    if "403" in msg or "PERMISSION_DENIED" in msg:
        return GeminiPermissionError(
            "Gemini projesinin bu modele erişim izni yok. "
            f"(model: {settings.GEMINI_MODEL})"
        )
    if "404" in msg or "NOT_FOUND" in msg:
        return GeminiModelError(
            f"Model bulunamadı: {settings.GEMINI_MODEL}. "
            ".env içindeki GEMINI_MODEL değerini kontrol edin."
        )
    return exc


# ─── Shared client (lazy) ─────────────────────────────────────────────────────

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not settings.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set in .env")
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


# ─── Meta extraction ─────────────────────────────────────────────────────────

@dataclass
class ResponseMeta:
    topic: str = "Genel"
    difficulty: str = "easy"
    status: str = "solved"


# Nested JSON'u da yakalamak için recursive-ish pattern
_META_PATTERN = re.compile(
    r'\{[^{}]*"_meta"\s*:\s*\{[^{}]*\}[^{}]*\}',
    re.DOTALL,
)


def _extract_meta(text: str) -> tuple[str, ResponseMeta]:
    """
    Strip the hidden `{"_meta": {...}}` line Gemini appends per the system
    prompt.  Returns (clean_text, ResponseMeta).
    """
    meta = ResponseMeta()
    match = _META_PATTERN.search(text)
    if match:
        try:
            raw = json.loads(match.group())
            inner = raw.get("_meta", {})
            meta.topic      = inner.get("topic", meta.topic)
            meta.difficulty = inner.get("difficulty", meta.difficulty)
            meta.status     = inner.get("status", meta.status)
        except json.JSONDecodeError:
            pass
        text = text[: match.start()] + text[match.end() :]

    return text.strip(), meta


# ─── Phase 1: text generation ────────────────────────────────────────────────

@dataclass
class TextResponse:
    content: str
    meta: ResponseMeta
    raw: str  # full unstripped text (for debugging)


async def generate_text_response(
    user_message: str,
    history: Optional[list[dict]] = None,
    *,
    on_token: Optional[callable] = None,  # type: ignore[type-arg]
) -> TextResponse:
    """
    Call Gemini with the FRIDAY system prompt and return the full response.

    Parameters
    ----------
    user_message:
        The text typed / spoken by Simge.
    history:
        Optional list of previous turns as
        [{"role": "user"|"model", "parts": [{"text": "..."}]}]
    on_token:
        Optional async callback(delta: str) called for each streamed chunk.
        Use this to push `token` events to the WebSocket in real-time.
    """
    client = _get_client()

    # Build conversation turns
    contents: list[genai_types.Content] = []
    for turn in (history or []):
        role = turn.get("role", "user")
        text = turn.get("parts", [{}])[0].get("text", "")
        contents.append(
            genai_types.Content(
                role=role,
                parts=[genai_types.Part(text=text)],
            )
        )
    contents.append(
        genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=user_message)],
        )
    )

    config = genai_types.GenerateContentConfig(
        system_instruction=settings.FRIDAY_SYSTEM_PROMPT,
        temperature=0.4,   # Daha tutarlı, deterministik yanıtlar
        max_output_tokens=1024,  # Kısa ve güçlü yanıt formatına uygun
    )

    model = settings.GEMINI_MODEL
    log.info("Gemini request → model=%s", model)

    full_text = ""
    try:
        # Streaming call — model adı .env'den geliyor, hardcoded değil
        async for chunk in await client.aio.models.generate_content_stream(
            model=model,
            contents=contents,
            config=config,
        ):
            delta = chunk.text or ""
            full_text += delta
            if on_token and delta:
                await on_token(delta)
    except Exception as exc:
        classified = _classify_gemini_error(exc)
        log.error("Gemini call failed [%s]: %s", type(classified).__name__, classified)
        # Kota veya izin hatası → fallback demo yanıt dön, istisna fırlatma
        if isinstance(classified, (GeminiQuotaError, GeminiPermissionError, GeminiModelError)):
            log.warning("Falling back to demo response (Gemini unavailable)")
            return _fallback_response(user_message)
        raise classified from exc

    clean, meta = _extract_meta(full_text)
    return TextResponse(content=clean, meta=meta, raw=full_text)


# ─── Fallback demo yanıt (Gemini erişilemediğinde) ───────────────────────────
# İçerik core/persona.py'dan gelir.


def _fallback_response(user_message: str) -> "TextResponse":
    q = user_message.lower()
    content = next(
        (v for k, v in _FALLBACK_RESPONSES.items() if k in q),
        _FALLBACK_DEFAULT,
    )
    meta = ResponseMeta(topic="Demo", difficulty="easy", status="pending")
    return TextResponse(content=content, meta=meta, raw=content)


# ─── Phase 2: Live audio session (stub) ──────────────────────────────────────

@dataclass
class GeminiLiveSession:
    """
    Placeholder for the Gemini Live multimodal session.

    When Phase 2 begins:
    1. Replace `_session` with a real `client.aio.live.connect(...)` context.
    2. Implement `send_audio()` to forward PCM chunks from the browser.
    3. Implement `receive_audio()` to stream audio back (or text if TTS is
       handled by ElevenLabs on the frontend).
    4. Handle interruption by calling `interrupt()`.

    The interface is intentionally narrow so `server.py` doesn't need to
    change — just swap this class.
    """

    session_id: str
    _session: object = field(default=None, repr=False)
    _active: bool = field(default=False, repr=False)

    async def start(self) -> None:
        """Open the live session with Gemini."""
        log.info("[Phase2-stub] GeminiLiveSession.start() called — not yet implemented")
        self._active = True

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Forward a raw PCM chunk to Gemini Live."""
        if not self._active:
            raise RuntimeError("Session not started")
        log.debug("[Phase2-stub] send_audio %d bytes — not yet implemented", len(pcm_bytes))

    async def receive_responses(self) -> AsyncIterator[str]:
        """Yield text/audio responses from Gemini Live."""
        # stub: yields nothing
        return
        yield  # make it an async generator

    async def interrupt(self) -> None:
        """Signal Gemini to stop the current turn (barge-in)."""
        log.debug("[Phase2-stub] interrupt() called")

    async def close(self) -> None:
        log.info("[Phase2-stub] GeminiLiveSession.close() called")
        self._active = False
