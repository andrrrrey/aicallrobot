"""Yandex SpeechKit TTS — API v3 REST. All voices including new ones."""

import asyncio
import httpx
import base64
import json
import re
from loguru import logger
from app.core.config import get_settings

_TTS_URL = "https://tts.api.cloud.yandex.net:443/tts/v3/utteranceSynthesis"
# v3 utteranceSynthesis: limit is ~250 UTF-8 chars for Cyrillic (2 bytes each)
_TTS_MAX_CHARS = 200


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
        self.headers = {
            "Authorization": f"Api-Key {self.settings.yandex_api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _split_text(text: str) -> list[str]:
        """Split at sentence boundaries; hard-cut anything still over the limit."""
        # Collapse all whitespace first
        text = " ".join(text.split())
        if len(text) <= _TTS_MAX_CHARS:
            return [text]

        parts = re.findall(r'[^.!?]+[.!?]+(?:\s|$)|[^.!?]+$', text) or [text]
        chunks, current = [], ''
        for part in parts:
            if len(current) + len(part) > _TTS_MAX_CHARS and current:
                chunks.append(current.strip())
                current = part
            else:
                current += part
        if current.strip():
            chunks.append(current.strip())

        # Hard-cut any piece still over the limit
        result = []
        for c in chunks:
            for i in range(0, len(c), _TTS_MAX_CHARS):
                result.append(c[i:i + _TTS_MAX_CHARS])
        return result or [text[:_TTS_MAX_CHARS]]

    async def _synthesize_one(
        self, client: httpx.AsyncClient, text: str, hints: list, sample_rate: int,
    ) -> bytes:
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
        response = await client.post(_TTS_URL, headers=self.headers, json=body)
        if response.status_code != 200:
            logger.error(f"TTS error {response.status_code} len={len(text)}: {response.text[:300]}")
            raise Exception(f"TTS failed: {response.status_code} {response.text[:200]}")

        audio = []
        for line in response.text.strip().split("\n"):
            if not line.strip():
                continue
            try:
                chunk_b64 = json.loads(line).get("result", {}).get("audioChunk", {}).get("data", "")
                if chunk_b64:
                    audio.append(base64.b64decode(chunk_b64))
            except json.JSONDecodeError:
                continue
        return b"".join(audio)

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

        hints = [{"voice": voice}, {"speed": speed}]
        if len(allowed) > 1 or allowed[0] != "neutral":
            hints.append({"role": role})

        chunks = self._split_text(text)
        logger.info(
            f"TTS: voice={voice} role={role} total_len={len(text)} "
            f"chunks={len(chunks)} max_chunk={max(len(c) for c in chunks)}"
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            if len(chunks) == 1:
                return await self._synthesize_one(client, chunks[0], hints, sample_rate)
            results = await asyncio.gather(
                *[self._synthesize_one(client, c, hints, sample_rate) for c in chunks]
            )
        return b"".join(results)

    @classmethod
    def get_voices_info(cls) -> list[dict]:
        return [
            {"id": v, "roles": [{"id": r, "label": cls.ROLE_LABELS.get(r, r)} for r in roles]}
            for v, roles in cls.VOICE_ROLES.items()
        ]
