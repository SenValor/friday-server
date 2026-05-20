"""
core/config.py
--------------
Centralised settings loaded from .env via python-dotenv.
All other modules import `settings` from here — never read os.environ directly.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root (one level up from core/)
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)


class Settings:
    # ── Gemini ────────────────────────────────────────────────────────────────
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

    # Primary model — Phase 1 text generation.
    # Default: JARVIS ile aynı model. .env ile override edilebilir.
    GEMINI_MODEL: str = os.getenv(
        "GEMINI_MODEL", "models/gemini-2.5-flash-native-audio-latest"
    )

    # Live / audio model — Phase 2.
    # Ayrı bir live model tanımlanmamışsa GEMINI_MODEL'i kullan.
    GEMINI_LIVE_MODEL: str = os.getenv(
        "GEMINI_LIVE_MODEL",
        os.getenv("GEMINI_MODEL", "models/gemini-2.5-flash-native-audio-latest"),
    )

    # ── Firebase ──────────────────────────────────────────────────────────────
    # Path to the downloaded service-account JSON file (local dev)
    FIREBASE_CREDENTIALS_PATH: str = os.getenv(
        "FIREBASE_CREDENTIALS_PATH", "firebase-service-account.json"
    )
    # Full JSON content of the service account (Railway / cloud deploy)
    # If set, takes priority over FIREBASE_CREDENTIALS_PATH
    FIREBASE_SERVICE_ACCOUNT_JSON: str = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    FIREBASE_PROJECT_ID: str = os.getenv("FIREBASE_PROJECT_ID", "")

    # ── Server ────────────────────────────────────────────────────────────────
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8765"))

    # ── TTS ───────────────────────────────────────────────────────────────────
    # false → TTS atlanır, mesaj anında idle'a döner (düşük gecikme)
    # true  → Gemini TTS üretilip WebSocket üzerinden gönderilir
    ENABLE_TTS: bool = os.getenv("ENABLE_TTS", "false").lower() == "true"

    # Comma-separated list of allowed CORS origins
    # e.g.  ALLOWED_ORIGINS=http://localhost:3000,https://friday.example.com
    ALLOWED_ORIGINS: list[str] = [
        o.strip()
        for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
        if o.strip()
    ]

    # ── FRIDAY character ──────────────────────────────────────────────────────
    FRIDAY_SYSTEM_PROMPT: str = """Sen FRIDAY isimli web tabanlı stajyer destek asistanısın.
Kullanıcın Simge. Hüseyin ana yönetici ve JARVIS sisteminin sahibidir.
Sen Hüseyin'e değil Simge'ye hizmet edersin.
Simge yazılım stajyeri gibi düşünülmeli.
Kod, proje, hata, Firebase, Next.js, React, Expo, Tailwind gibi konularda adım adım yardım edersin.
Cevapların kısa, doğal, samimi ve profesyonel olur.
Simge bir şeyi anlamazsa sabırla basitleştirirsin.
Çözülemeyen veya kritik konuları "needs_help" olarak işaretlersin.
Her konuşma daha sonra Hüseyin'in JARVIS sistemi tarafından analiz edilebilecek şekilde kaydedilir.

Cevabının sonunda — kullanıcıya gösterme, sadece aşağıdaki JSON satırını ekle:
{"_meta": {"topic": "<kısa_konu>", "difficulty": "<easy|medium|hard>", "status": "<solved|needs_help|pending>"}}
"""

    def validate(self) -> list[str]:
        """Return a list of missing required config keys (empty = OK)."""
        missing = []
        if not self.GEMINI_API_KEY:
            missing.append("GEMINI_API_KEY")
        if not self.FIREBASE_PROJECT_ID:
            missing.append("FIREBASE_PROJECT_ID")
        return missing


settings = Settings()
