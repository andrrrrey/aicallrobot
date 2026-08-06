"""fish.audio TTS — REST API (https://api.fish.audio).

Третий провайдер синтеза речи (наряду с Yandex SpeechKit и SaluteSpeech).
Голос задаётся через ``reference_id`` — ID голосовой модели fish.audio. Личные
модели пользователя подтягиваются динамически через ``GET /model?self=true``.

Пайплайн звонка ожидает raw PCM 8 кГц / 16 бит / моно (см. ``pjsua_agent.py``),
а fish.audio отдаёт PCM на своей частоте дискретизации, поэтому поток
ресемплится «на лету» через ``audioop.ratecv`` (stdlib, без ffmpeg).
"""

import audioop
import httpx
from collections.abc import AsyncGenerator
from loguru import logger

from app.core.config import get_settings


class FishAudioTTSService:
    """Синтез речи через fish.audio REST API."""

    TTS_URL = "https://api.fish.audio/v1/tts"
    MODEL_URL = "https://api.fish.audio/model"

    # Частота, которую запрашиваем у fish.audio. 24 кГц гарантированно
    # поддерживается и кратно делится на целевые 8 кГц.
    SOURCE_SAMPLE_RATE = 24000
    # Целевой формат пайплайна звонка.
    TARGET_SAMPLE_RATE = 8000
    _SAMPLE_WIDTH = 2   # 16 бит
    _CHANNELS = 1       # моно

    def __init__(self):
        self.settings = get_settings()
        # Персистентный клиент: переиспользует TCP/TLS-соединения.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )

    def _api_key(self) -> str:
        key = self.settings.fishaudio_api_key
        if not key:
            raise RuntimeError("FISHAUDIO_API_KEY не задан в настройках")
        return key

    async def synthesize_stream(
        self,
        text: str,
        reference_id: str | None = None,
        speed: float | None = None,
        sample_rate: int | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Стриминг синтеза: отдаёт PCM 8 кГц / 16 бит / моно по мере поступления.

        Аудио запрашивается у fish.audio в формате PCM на ``SOURCE_SAMPLE_RATE`` и
        ресемплится до ``TARGET_SAMPLE_RATE`` потоково, с сохранением состояния
        ресемплера между чанками.
        """
        reference_id = reference_id or self.settings.fishaudio_model
        target_rate = sample_rate or self.TARGET_SAMPLE_RATE

        body: dict = {
            "text": text,
            "format": "pcm",
            "sample_rate": self.SOURCE_SAMPLE_RATE,
            "latency": "normal",
        }
        if reference_id:
            body["reference_id"] = reference_id
        if speed and speed != 1.0:
            body["prosody"] = {"speed": speed, "volume": 0}

        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

        logger.info(
            f"fish.audio TTS stream: reference_id={reference_id or '<default>'}, "
            f"speed={speed}, text='{text[:50]}...'"
        )

        # Состояние потокового ресемплинга и «хвост» из нечётного байта,
        # который нельзя отдать в audioop (нужна кратность sample_width).
        rate_state = None
        leftover = b""
        total = 0

        async with self._client.stream(
            "POST", self.TTS_URL, headers=headers, json=body
        ) as response:
            if response.status_code != 200:
                err = await response.aread()
                logger.error(
                    f"fish.audio TTS error {response.status_code}: {err[:300]}"
                )
                raise RuntimeError(
                    f"fish.audio TTS failed: {response.status_code} {err[:200]}"
                )

            async for raw in response.aiter_bytes():
                if not raw:
                    continue
                buf = leftover + raw
                # audioop требует длину, кратную sample_width; лишний байт
                # переносим в следующий чанк.
                usable = len(buf) - (len(buf) % self._SAMPLE_WIDTH)
                if usable <= 0:
                    leftover = buf
                    continue
                leftover = buf[usable:]
                pcm, rate_state = audioop.ratecv(
                    buf[:usable], self._SAMPLE_WIDTH, self._CHANNELS,
                    self.SOURCE_SAMPLE_RATE, target_rate, rate_state,
                )
                if pcm:
                    total += len(pcm)
                    yield pcm

        logger.info(f"fish.audio TTS stream ok: {total} bytes (PCM {target_rate} Hz)")

    async def synthesize(
        self,
        text: str,
        reference_id: str | None = None,
        speed: float | None = None,
        sample_rate: int | None = None,
    ) -> bytes:
        """Синхронная обёртка: собирает весь PCM из ``synthesize_stream``.

        Используется HTTP-эндпоинтом теста/демо, где нужен цельный буфер.
        """
        chunks: list[bytes] = []
        async for chunk in self.synthesize_stream(
            text=text, reference_id=reference_id, speed=speed, sample_rate=sample_rate
        ):
            chunks.append(chunk)
        return b"".join(chunks)

    async def list_my_voices(self) -> list[dict]:
        """Список личных голосовых моделей пользователя (GET /model?self=true)."""
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        params = {"self": "true", "page_size": 100}

        resp = await self._client.get(self.MODEL_URL, headers=headers, params=params)
        if resp.status_code != 200:
            logger.error(
                f"fish.audio list models error {resp.status_code}: {resp.text[:300]}"
            )
            raise RuntimeError(
                f"fish.audio list models failed: {resp.status_code} {resp.text[:200]}"
            )

        items = resp.json().get("items", [])
        voices = [
            {"id": it.get("_id"), "label": it.get("title") or it.get("_id")}
            for it in items
            if it.get("_id")
        ]
        logger.info(f"fish.audio: получено {len(voices)} личных голосов")
        return voices

    @classmethod
    def get_voices_info(cls) -> list[dict]:
        # Голоса fish.audio динамические (личные модели пользователя) —
        # статического каталога нет, список берётся через ``list_my_voices``.
        return []
