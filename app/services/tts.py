"""Yandex SpeechKit TTS — API v3 REST. All voices including new ones."""

import asyncio
import httpx
import base64
import json
import re
from loguru import logger
from app.core.config import get_settings

_TTS_MAX_CHARS = 450  # utteranceSynthesis v3 hard limit (safe margin)


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

    @staticmethod
    def _split_text(text: str) -> list[str]:
        """Split text into chunks ≤ _TTS_MAX_CHARS at sentence boundaries."""
        parts = re.findall(r'[^.!?]+[.!?]+(?:\s|$)|[^.!?]+$', text)
        if not parts:
            parts = [text]
        chunks, current = [], ''
        for part in parts:
            if len(current + part) > _TTS_MAX_CHARS and current:
                chunks.append(current.strip())
                current = part
            else:
                current += part
        if current.strip():
            chunks.append(current.strip())
        # Hard-cut any piece that still exceeds the limit
        result = []
        for c in (chunks or [text]):
            for i in range(0, len(c), _TTS_MAX_CHARS):
                result.append(c[i:i + _TTS_MAX_CHARS])
        return result

    async def _synthesize_chunk(
        self, client: httpx.AsyncClient, text: str,
        hints: list, sample_rate: int,
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
        return b"".join(audio_chunks)

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
        logger.info(f"TTS v3: voice={voice}, role={role}, len={len(text)}, chunks={len(chunks)}, text='{text[:60]}'")

        async with httpx.AsyncClient(timeout=30.0) as client:
            if len(chunks) == 1:
                return await self._synthesize_chunk(client, chunks[0], hints, sample_rate)
            # Multiple chunks — synthesize in parallel and concatenate
            results = await asyncio.gather(
                *[self._synthesize_chunk(client, c, hints, sample_rate) for c in chunks]
            )
        audio_data = b"".join(results)
        logger.info(f"TTS v3 ok: {len(audio_data)} bytes ({len(chunks)} chunks)")
        return audio_data

    @classmethod
    def get_voices_info(cls) -> list[dict]:
        return [
            {"id": v, "roles": [{"id": r, "label": cls.ROLE_LABELS.get(r, r)} for r in roles]}
            for v, roles in cls.VOICE_ROLES.items()
        ]
