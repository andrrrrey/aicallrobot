"""Yandex SpeechKit TTS — API v3 REST. All voices including new ones."""

import re
import httpx
import base64
import json
from collections.abc import AsyncGenerator
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

    MAX_TTS_CHARS = 4000

    def __init__(self):
        self.settings = get_settings()
        self.url = "https://tts.api.cloud.yandex.net:443/tts/v3/utteranceSynthesis"
        self.headers = {
            "Authorization": f"Api-Key {self.settings.yandex_api_key}",
            "Content-Type": "application/json",
        }
        # Персистентный клиент: переиспользует TCP/TLS-соединения
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    async def synthesize(
        self, text: str, voice: str | None = None, speed: float | None = None,
        role: str | None = None, sample_rate: int | None = None,
    ) -> bytes:
        voice, speed, role, sample_rate, body = self._build_request(
            text, voice, speed, role, sample_rate
        )
        logger.info(f"TTS v3: voice={voice}, role={role}, text='{text[:50]}...'")

        response = await self._client.post(self.url, headers=self.headers, json=body)
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

    async def synthesize_stream(
        self, text: str, voice: str | None = None, speed: float | None = None,
        role: str | None = None, sample_rate: int | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """
        Стриминг синтеза речи: отдаёт PCM-чанки по мере поступления от API.
        Автоматически разбивает длинные тексты на части (лимит Yandex TTS ~5000 символов).
        """
        voice, speed, role, sample_rate, _ = self._build_request(
            "x", voice, speed, role, sample_rate
        )
        for part in self._split_text(text):
            _, _, _, _, body = self._build_request(part, voice, speed, role, sample_rate)
            logger.info(f"TTS v3 stream: voice={voice}, role={role}, text='{part[:50]}...'")

            async with self._client.stream("POST", self.url, headers=self.headers, json=body) as response:
                if response.status_code != 200:
                    body_text = await response.aread()
                    logger.error(f"TTS v3 stream error {response.status_code}: {body_text[:300]}")
                    raise Exception(f"TTS stream failed: {response.status_code}")

                total = 0
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        chunk_b64 = data.get("result", {}).get("audioChunk", {}).get("data", "")
                        if chunk_b64:
                            chunk = base64.b64decode(chunk_b64)
                            total += len(chunk)
                            yield chunk
                    except json.JSONDecodeError:
                        continue
                logger.info(f"TTS v3 stream ok: {total} bytes total")

    def _split_text(self, text: str) -> list[str]:
        """Split text at sentence boundaries to stay within TTS character limit."""
        if len(text) <= self.MAX_TTS_CHARS:
            return [text]
        sentences = re.split(r'(?<=[.!?…])\s+', text)
        parts, current = [], ""
        for sentence in sentences:
            if not current:
                current = sentence
            elif len(current) + 1 + len(sentence) <= self.MAX_TTS_CHARS:
                current += " " + sentence
            else:
                parts.append(current)
                current = sentence
        if current:
            parts.append(current)
        # Force-split any part that still exceeds the limit (no sentence boundaries)
        result = []
        for part in parts:
            while len(part) > self.MAX_TTS_CHARS:
                result.append(part[:self.MAX_TTS_CHARS])
                part = part[self.MAX_TTS_CHARS:]
            if part:
                result.append(part)
        return result

    def _build_request(
        self,
        text: str,
        voice: str | None,
        speed: float | None,
        role: str | None,
        sample_rate: int | None,
    ) -> tuple:
        voice = voice or self.settings.tts_voice
        speed = speed or self.settings.tts_speed
        role = role or self.settings.tts_emotion
        sample_rate = sample_rate or self.settings.audio_sample_rate

        allowed = self.VOICE_ROLES.get(voice, ["neutral"])
        if role not in allowed:
            role = allowed[0]

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
        return voice, speed, role, sample_rate, body

    @classmethod
    def get_voices_info(cls) -> list[dict]:
        return [
            {"id": v, "roles": [{"id": r, "label": cls.ROLE_LABELS.get(r, r)} for r in roles]}
            for v, roles in cls.VOICE_ROLES.items()
        ]
