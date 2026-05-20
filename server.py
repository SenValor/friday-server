"""
server.py
---------
FastAPI application entry-point.

Endpoints
---------
  GET  /              health check
  GET  /health        JSON health + active session count
  POST /tts/speak     Gemini TTS → base64 WAV
  WS   /ws/friday-live        text chat WebSocket (Phase 1, active)
  WS   /ws/friday-native-audio  Gemini Live native audio (Phase 2, stub)

WebSocket event flow (Phase 1 — text chat)
------------------------------------------
  browser  ──user_message──►  server
  server   ──state:thinking──► browser
  server   ──task:analyzing──► browser
  server   ──task:preparing──► browser
           (Gemini streams…)
  server   ──assistant_message──► browser
  server   ──state:speaking──► browser
           (Firebase write…)
  server   ──saved──────────► browser
  server   ──state:idle──────► browser

WebSocket event flow (Phase 2 — native audio, TODO)
----------------------------------------------------
  browser  ──audio_init──►   server (sampleRate, channels)
  browser  ──audio_chunk──►  server (raw PCM float32, base64)
  server   ──audio_out──►    browser (Gemini Live PCM response, base64)
  server   ──transcript──►   browser (optional: what Gemini heard)
  server   ──state:*──►      browser (listening / thinking / speaking)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.config import settings
from models.events import (
    AssistantMessageEvent,
    ConnectedEvent,
    DifficultyLevel,
    ErrorEvent,
    FridayState,
    MessageStatus,
    PongEvent,
    SavedEvent,
    StateEvent,
    TaskEvent,
    TaskStatus,
    UserMessageEvent,
    parse_inbound,
)
from services import firebase_service as fb
from services.tts_service import generate_speech
from services.gemini_live import (
    generate_text_response,
    GeminiQuotaError,
    GeminiPermissionError,
    GeminiModelError,
)
from services.session_manager import FridaySession, session_manager

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("friday.server")


# ─── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    missing = settings.validate()
    if missing:
        log.warning("Missing config keys (some features disabled): %s", missing)
    else:
        log.info("All config keys present — Gemini + Firebase fully enabled")
    log.info(
        "FRIDAY server starting on ws://%s:%s/ws/friday-live",
        settings.HOST,
        settings.PORT,
    )
    yield
    # Shutdown
    log.info("FRIDAY server shutting down. Active sessions: %d", session_manager.active_count)


# ─── FastAPI app ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="FRIDAY Backend",
    description="WebSocket backend for FRIDAY intern assistant",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── HTTP endpoints ───────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"service": "FRIDAY", "status": "online", "version": "1.0.0"}


class TTSRequest(BaseModel):
    text: str


@app.post("/tts/speak")
async def tts_speak(req: TTSRequest):
    """
    Metni Gemini TTS ile sese çevir.
    Döner: { "audio": "<base64 WAV>", "mime": "audio/wav" }
    Audio boşsa TTS başarısız olmuş — frontend sessiz kalır.
    """
    b64 = await generate_speech(req.text)
    return JSONResponse({"audio": b64, "mime": "audio/wav"})


@app.get("/health")
async def health():
    fb_ok = await fb.ping()
    return {
        "status":          "ok",
        "firebase":        "connected" if fb_ok else "unavailable",
        "active_sessions": session_manager.active_count,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
    }


# ─── WebSocket handler ────────────────────────────────────────────────────────

@app.websocket("/ws/friday-live")
async def friday_ws(websocket: WebSocket):
    await websocket.accept()

    # Create session
    session = await session_manager.create(websocket)
    log.info("WebSocket connected → session %s", session.id)

    # Greet the client
    await session.send(ConnectedEvent(sessionId=session.id))

    # Register session in Firestore (non-blocking, best-effort)
    try:
        await fb.ensure_session(
            session_id=session.id,
            user_id=session.user_id,
            user_name=session.user_name,
        )
        await fb.ensure_user(
            user_id=session.user_id,
            user_name=session.user_name,
        )
    except Exception as exc:
        log.warning("Firebase session init failed: %s", exc)

    try:
        while True:
            raw_text = await websocket.receive_text()
            await _handle_message(session, raw_text)

    except WebSocketDisconnect:
        log.info("WebSocket disconnected — session %s", session.id)
    except Exception as exc:
        log.error("Unexpected WebSocket error (session %s): %s", session.id, exc)
        try:
            await session.send(ErrorEvent(message="Beklenmeyen sunucu hatası.", code="INTERNAL"))
        except Exception:
            pass
    finally:
        await session_manager.remove(session.id)
        try:
            await fb.close_session(session.id)
        except Exception:
            pass


# ─── Message dispatch ─────────────────────────────────────────────────────────

async def _handle_message(session: FridaySession, raw_text: str) -> None:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        await session.send(ErrorEvent(message="Geçersiz JSON formatı.", code="BAD_JSON"))
        return

    try:
        event = parse_inbound(data)
    except ValueError as exc:
        await session.send(ErrorEvent(message=str(exc), code="UNKNOWN_EVENT"))
        return

    # ── Ping / Pong ──
    if event.type == "ping":
        await session.send(PongEvent())
        return

    # ── Audio chunk (Phase 2 stub) ──
    if event.type == "audio_chunk":
        await session.send(
            ErrorEvent(message="Ses akışı henüz aktif değil (Phase 2).", code="NOT_IMPLEMENTED")
        )
        return

    # ── Text message ──
    if event.type == "user_message":
        await _handle_user_message(session, event)  # type: ignore[arg-type]


async def _handle_user_message(session: FridaySession, event: UserMessageEvent) -> None:
    session.bump()

    # 1. State: thinking
    await session.send(StateEvent(state=FridayState.THINKING))

    # 2. Task: analyzing
    await session.send(TaskEvent(title="Soruyu analiz ediyorum", status=TaskStatus.WORKING))

    # 3. Task: context check
    await session.send(
        TaskEvent(
            title="Proje bağlamını kontrol ediyorum",
            description=f"Proje: {event.project}",
            status=TaskStatus.WORKING,
        )
    )

    # 4. Call Gemini
    await session.send(TaskEvent(title="Çözüm adımlarını hazırlıyorum", status=TaskStatus.WORKING))
    await session.send(StateEvent(state=FridayState.WORKING))

    try:
        result = await generate_text_response(
            user_message=event.content,
            history=session.history,
        )
    except GeminiQuotaError as exc:
        log.warning("Gemini quota: %s", exc)
        await session.send(StateEvent(state=FridayState.ERROR))
        await session.send(ErrorEvent(
            message="Gemini kotası doldu veya bu model için kota yok. "
                    "Lütfen API key kotasını veya model ayarını kontrol et.",
            code="GEMINI_QUOTA",
        ))
        return
    except GeminiPermissionError as exc:
        log.error("Gemini permission: %s", exc)
        await session.send(StateEvent(state=FridayState.ERROR))
        await session.send(ErrorEvent(
            message="Gemini projesinin bu modele erişim izni yok. "
                    ".env içindeki GEMINI_MODEL ve API key'i kontrol et.",
            code="GEMINI_PERMISSION",
        ))
        return
    except GeminiModelError as exc:
        log.error("Gemini model not found: %s", exc)
        await session.send(StateEvent(state=FridayState.ERROR))
        await session.send(ErrorEvent(
            message=f"Model bulunamadı. .env içindeki GEMINI_MODEL değerini kontrol et.",
            code="GEMINI_MODEL_NOT_FOUND",
        ))
        return
    except Exception as exc:
        log.error("Gemini unexpected error: %s", exc)
        await session.send(StateEvent(state=FridayState.ERROR))
        await session.send(ErrorEvent(
            message="Gemini API yanıt vermedi. Lütfen tekrar deneyin.",
            code="GEMINI_ERROR",
        ))
        return

    # Update history for multi-turn context
    session.add_turn("user", event.content)
    session.add_turn("model", result.content)

    # 5. Send assistant message
    status_map = {
        "solved":     MessageStatus.SOLVED,
        "needs_help": MessageStatus.NEEDS_HELP,
        "pending":    MessageStatus.PENDING,
    }
    diff_map = {
        "easy":   DifficultyLevel.EASY,
        "medium": DifficultyLevel.MEDIUM,
        "hard":   DifficultyLevel.HARD,
    }

    msg_status     = status_map.get(result.meta.status, MessageStatus.SOLVED)
    msg_difficulty = diff_map.get(result.meta.difficulty, DifficultyLevel.EASY)

    await session.send(StateEvent(state=FridayState.SPEAKING))
    await session.send(
        AssistantMessageEvent(
            content=result.content,
            topic=result.meta.topic,
            status=msg_status,
            difficultyLevel=msg_difficulty,
        )
    )

    # 5b. Gemini TTS — Gemini yanıt üretilirken paralel başlat
    # TTS ve Firebase yazımı aynı anda çalışır, ikisi de bitince idle'a geç
    tts_task: asyncio.Task | None = None
    if settings.ENABLE_TTS:
        async def _do_tts() -> None:
            try:
                tts_b64 = await generate_speech(result.content)
                if tts_b64:
                    await session.websocket.send_text(json.dumps({
                        "type": "tts_audio",
                        "audio": tts_b64,
                        "mime": "audio/wav",
                    }))
            except Exception as exc:
                log.warning("TTS gönderilemedi: %s", exc)

        tts_task = asyncio.create_task(_do_tts())

    # 6. Persist to Firestore
    await session.send(TaskEvent(title="Cevabı kaydediyorum", status=TaskStatus.WORKING))
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        doc_id = await fb.save_message(
            session_id=session.id,
            user_id=event.userId,
            user_name=event.userName,
            message=event.content,
            response=result.content,
            topic=result.meta.topic,
            project=event.project,
            difficulty_level=result.meta.difficulty,
            status=result.meta.status,
        )
        await fb.append_daily_log_topic(
            user_id=event.userId,
            user_name=event.userName,
            date_str=today,
            topic=result.meta.topic,
            project=event.project,
            status=result.meta.status,
        )
        await session.send(SavedEvent(status="success", messageId=doc_id))
        log.info("Message saved → %s (topic=%s, status=%s)", doc_id, result.meta.topic, result.meta.status)
    except Exception as exc:
        log.error("Firebase save failed: %s", exc)
        # Non-fatal — UI still gets the response
        await session.send(SavedEvent(status="failed"))

    # 7. TTS bitmesini bekle (Firebase ile paralel çalıştı), sonra idle
    if tts_task is not None:
        await tts_task

    # 8. Back to idle
    await session.send(StateEvent(state=FridayState.IDLE))


# ─── Phase 2: Gemini Live Native Audio (stub) ────────────────────────────────
#
# Bu endpoint gelecek aşamada browser'dan ham PCM audio alıp Gemini Live
# BidiGenerateContent API'sine iletecek ve ses yanıtını geri akıtacak.
#
# Mevcut durum: stub — bağlantıyı kabul eder, "not yet implemented" bilgisi gönderir.
#
# Pipeline (Phase 2):
#   1. Browser AudioContext → getDisplayMedia / getUserMedia → PCM Float32
#   2. Float32 → base64 → WebSocket → server
#   3. Server → google.ai.generativelanguage.BidiGenerateContent stream
#   4. Gemini PCM response → base64 → WebSocket → browser
#   5. Browser AudioContext → destination (autoplay)

@app.websocket("/ws/friday-native-audio")
async def friday_native_audio_ws(websocket: WebSocket):
    """
    Gemini Live Native Audio WebSocket — Phase 2 stub.

    Mevcut davranış:
      - Bağlantıyı kabul eder
      - 'not_implemented' olayı gönderir
      - Bağlantıyı kapatır

    Phase 2'de bu endpoint gerçek Gemini Live streaming pipeline'ına bağlanacak.
    """
    await websocket.accept()
    log.info("Native audio WS connected (Phase 2 stub)")

    await websocket.send_text(json.dumps({
        "type":    "native_audio_status",
        "status":  "not_implemented",
        "message": "Gemini Live native audio henüz aktif değil (Phase 2). "
                   "Metin tabanlı sohbet için /ws/friday-live kullanın.",
        "phase":   2,
    }))
    await websocket.close(code=1001, reason="phase2_not_ready")


# ─── Entry-point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
        log_level="info",
    )
