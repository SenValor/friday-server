"""
services/tts_service.py
-----------------------
Gemini 2.5 Flash TTS — FRIDAY'in sesi.

- generate_speech(text) → ham PCM bytes (L16, 24 kHz, mono)
- PCM'i WAV'a dönüştürüp base64 olarak döndürür
- Frontend direkt <audio src="data:audio/wav;base64,..."> ile çalar

Ses karakteri: "Aoede" — yumuşak, doğal kadın sesi
(Diğer seçenekler: Puck, Charon, Kore, Fenrir, Leda, Orus, Zephyr)
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import struct
import wave

from google import genai
from google.genai import types as genai_types

from core.config import settings

log = logging.getLogger(__name__)

TTS_MODEL   = "gemini-2.5-flash-preview-tts"
TTS_VOICE   = "Aoede"          # FRIDAY'in sesi
SAMPLE_RATE = 24_000           # Gemini PCM çıktısı 24 kHz
CHANNELS    = 1
SAMPLE_WIDTH = 2               # 16-bit = 2 byte

# TTS için maksimum karakter — hızlı yanıt için sadece ilk 200 karakter
# Uzun yanıtlarda ilk 1-2 cümle okunur, geri kalanı chatde görülür
MAX_CHARS = 200


# ─── PCM → WAV dönüşümü ──────────────────────────────────────────────────────

def _pcm_to_wav(pcm_bytes: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


# ─── Markdown temizleyici ─────────────────────────────────────────────────────

def _clean_for_tts(text: str) -> str:
    """Markdown işaretlerini ve kod bloklarını TTS için temizle."""
    import re
    # Kod blokları → kısa açıklama
    text = re.sub(r"```[\s\S]*?```", " Kod bloğu. ", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Bold/italic
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    # Başlıklar
    text = re.sub(r"#{1,6}\s", "", text)
    # Madde işaretleri
    text = re.sub(r"^[-*+]\s", "", text, flags=re.MULTILINE)
    # Çoklu boşluk/satır
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Demo notu varsa çıkar
    text = re.sub(r"\(Not: Gemini[^\)]+\)", "", text)
    text = text.strip()
    if len(text) <= MAX_CHARS:
        return text
    # Cümle sınırında kes — yarım cümle okuma
    cut = text[:MAX_CHARS]
    last_stop = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '), cut.rfind('\n'))
    if last_stop > 60:
        return cut[:last_stop + 1].strip()
    return cut.strip()


# ─── Ana TTS fonksiyonu ───────────────────────────────────────────────────────

async def generate_speech(text: str) -> str:
    """
    Metni Gemini TTS ile sese çevir.
    Dönen değer: base64 WAV string (data URI olmadan, sadece data kısmı).
    Hata durumunda None döner — frontend sessiz kalır.
    """
    clean = _clean_for_tts(text)
    if not clean:
        return ""

    def _call() -> bytes:
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        resp = client.models.generate_content(
            model=TTS_MODEL,
            contents=clean,
            config=genai_types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=genai_types.SpeechConfig(
                    voice_config=genai_types.VoiceConfig(
                        prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                            voice_name=TTS_VOICE
                        )
                    )
                ),
            ),
        )
        part = resp.candidates[0].content.parts[0]
        if not (hasattr(part, "inline_data") and part.inline_data):
            raise ValueError("TTS yanıtında audio verisi yok")
        return part.inline_data.data

    try:
        pcm_bytes = await asyncio.to_thread(_call)
        wav_bytes  = _pcm_to_wav(pcm_bytes)
        b64        = base64.b64encode(wav_bytes).decode("utf-8")
        log.info("TTS üretildi: %d karakter → %d bytes WAV", len(clean), len(wav_bytes))
        return b64
    except Exception as exc:
        log.warning("TTS başarısız (sessiz devam): %s", exc)
        return ""
