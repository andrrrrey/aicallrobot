"""Yandex SpeechKit TTS — API v1 REST. Reliable 5000-char limit, raw PCM output."""

import httpx
from loguru import logger
from app.core.config import get_settings

# v3 utteranceSynthesis has undocumented low limits; v1 is stable with 5000 chars
_TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
_TTS_MAX_CHARS = 3500  # conservative margin under the 5000-char v1 limit

# v1 API supports: good, evil, neutral — map v3-style roles
_ROLE_MAP = {
    "neutral": "neutral",
    "good": "good",
    "evil": "evil",
    "friendly": "good",
    "strict": "neutral",
    "whisper": "neutral",
}


class TTSService:
    """Синтез речи через Yandex SpeechKit API v1."""

    VOICE_ROLES = {
        "alena":     ["neutral", "good"],
        "filipp":    ["neutral"],
        "ermil":     ["neutral", "good"],
        "jane":      ["neutral", "good", "evil"],
        "omazh":     ["neutral", "evil"],
        "zahar":     ["neutral", "good"],
        "marina":    ["neutral", "whisper", "friendly"],
        "madi_ru":   ["neutral"],
        "dasha":     ["neutral", "good", "friendly"],
        "julia":     ["neutral", "strict"],
        "lera":      ["neutral", "friendly"],
        "masha":     ["good", "strict", "friendly"],
        "alexander": ["neutral", "good"],
        "kirill":    ["neutral", "strict", "good"],
        "anton":     ["neutral", "good"],
    }

    ROLE_LABELS = {
        "neutral": "Нейтральный", "good": "Радостный", "evil": "Раздражённый",
        "friendly": "Дружелюбный", "strict": "Строгий", "whisper": "Шёпот",
    }

    def __init__(self):
        self.settings = get_settings()

    def _headers(self) -> dict:
        return {"Authorization": f"Api-Key {self.settings.yandex_api_key}"}

    async def synthesize(
        self, text: str, voice: str | None = None, speed: float | None = None,
        role: str | None = None, sample_rate: int | None = None,
    ) -> bytes:
        voice = voice or self.settings.tts_voice
        speed = speed or self.settings.tts_speed
        role = role or self.settings.tts_emotion
        sample_rate = sample_rate or self.settings.audio_sample_rate

        # Validate role for this voice
        allowed = self.VOICE_ROLES.get(voice, ["neutral"])
        if role not in allowed:
            role = allowed[0]

        # Map to v1 emotion values
        emotion = _ROLE_MAP.get(role, "neutral")

        # Clean and truncate text
        text = " ".join(text.split())  # collapse whitespace/newlines
        text = text[:_TTS_MAX_CHARS]

        logger.info(f"TTS v1: voice={voice}, emotion={emotion}, len={len(text)}, text='{text[:60]}'")

        params = {
            "folderId": self.settings.yandex_folder_id,
            "text": text,
            "voice": voice,
            "emotion": emotion,
            "speed": str(speed),
            "format": "lpcm",
            "sampleRateHertz": str(sample_rate),
            "lang": "ru-RU",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                _TTS_URL,
                headers=self._headers(),
                data=params,
            )
            if response.status_code != 200:
                logger.error(f"TTS v1 error {response.status_code}: {response.text[:500]}")
                raise Exception(f"TTS failed: {response.status_code} {response.text[:200]}")

            audio_data = response.content
            logger.info(f"TTS v1 ok: {len(audio_data)} bytes")
            return audio_data

    @classmethod
    def get_voices_info(cls) -> list[dict]:
        return [
            {"id": v, "roles": [{"id": r, "label": cls.ROLE_LABELS.get(r, r)} for r in roles]}
            for v, roles in cls.VOICE_ROLES.items()
        ]
