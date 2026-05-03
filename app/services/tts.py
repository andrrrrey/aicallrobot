"""Yandex SpeechKit TTS — API v3 REST. All voices including new ones."""

import httpx
import base64
import json
from loguru import logger
from app.core.config import get_settings


class TTSService:
    """Синтез речи через Yandex SpeechKit API v3 REST."""

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
        self.url = "https://tts.api.cloud.yandex.net:443/tts/v3/utteranceSynthesis"
        self.headers = {
            "Authorization": f"Api-Key {self.settings.yandex_api_key}",
            "Content-Type": "application/json",
        }

    async def synthesize(
        self, text: str, voice: str | None = None, speed: float | None = None,
        role: str | None = None, sample_rate: int | None = None,
    ) -> bytes:
        voice = voice or self.settings.tts_voice
        speed = speed or self.settings.tts_speed
        role = role or self.settings.tts_emotion
        sample_rate = sample_rate or self.settings.audio_sample_rate

        allowed = self.VOICE_ROLES.get(voice, ["neutral"])
        if role not in allowed:
            role = allowed[0]
            logger.warning(f"Role '{role}' not supported for '{voice}', using '{role}'")

        hints = [{"voice": voice}, {"speed": speed}]
        if len(allowed) > 1 or allowed[0] != "neutral":
            hints.append({"role": role})

        body = {
            "text": text,
            "hints": hints,
            "output_audio_spec": {
                "raw_audio": {
                    "audio_encoding": "LINEAR16_PCM",
                    "sample_rate_hertz": sample_rate,
                }
            },
        }

        logger.info(f"TTS v3: voice={voice}, role={role}, text='{text[:50]}...'")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(self.url, headers=self.headers, json=body)
            if response.status_code != 200:
                logger.error(f"TTS v3 error {response.status_code}: {response.text[:500]}")
                raise Exception(f"TTS failed: {response.status_code} {response.text[:200]}")

            audio_chunks = []
            for line in response.text.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    chunk_b64 = data.get("result", {}).get("audioChunk", {}).get("data", "")
                    if chunk_b64:
                        audio_chunks.append(base64.b64decode(chunk_b64))
                except json.JSONDecodeError:
                    continue

            audio_data = b"".join(audio_chunks)
            logger.info(f"TTS v3 ok: {len(audio_data)} bytes")
            return audio_data

    @classmethod
    def get_voices_info(cls) -> list[dict]:
        return [
            {"id": v, "roles": [{"id": r, "label": cls.ROLE_LABELS.get(r, r)} for r in roles]}
            for v, roles in cls.VOICE_ROLES.items()
        ]
