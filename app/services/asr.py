"""Yandex SpeechKit ASR (Speech-to-Text) service."""

import httpx
from loguru import logger
from app.core.config import get_settings


class ASRService:
    """Распознавание речи через Yandex SpeechKit API v1 (short audio / streaming)."""

    def __init__(self):
        self.settings = get_settings()
        self.headers = {
            "Authorization": f"Api-Key {self.settings.yandex_api_key}",
        }

    async def recognize_short(
        self,
        audio_data: bytes,
        format: str = "lpcm",
        sample_rate: int | None = None,
        language: str | None = None,
    ) -> str:
        """
        Распознавание короткого аудио (до 30 сек).

        Args:
            audio_data: Аудиоданные в raw PCM или OGG
            format: Формат (lpcm, oggopus)
            sample_rate: Частота дискретизации
            language: Язык распознавания

        Returns:
            str: Распознанный текст
        """
        sample_rate = sample_rate or self.settings.audio_sample_rate
        language = language or self.settings.asr_language

        url = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
        params = {
            "folderId": self.settings.yandex_folder_id,
            "lang": language,
            "format": format,
            "sampleRateHertz": str(sample_rate),
            "model": self.settings.asr_model,
        }

        logger.info(f"ASR short request: {len(audio_data)} bytes, lang={language}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                headers=self.headers,
                params=params,
                content=audio_data,
            )

            if response.status_code != 200:
                logger.error(f"ASR error {response.status_code}: {response.text}")
                raise Exception(f"ASR recognition failed: {response.status_code}")

            result = response.json()
            text = result.get("result", "")
            logger.info(f"ASR result: '{text}'")
            return text

    async def recognize_long(
        self,
        audio_url: str,
        format: str = "lpcm",
        sample_rate: int | None = None,
    ) -> str:
        """
        Асинхронное распознавание длинного аудио.
        Отправляет задачу и возвращает operation_id.
        """
        sample_rate = sample_rate or self.settings.audio_sample_rate

        url = "https://transcribe.api.cloud.yandex.net/speech/stt/v2/longRunningRecognize"
        body = {
            "config": {
                "specification": {
                    "languageCode": self.settings.asr_language,
                    "model": self.settings.asr_model,
                    "audioEncoding": "LINEAR16_PCM" if format == "lpcm" else "OGG_OPUS",
                    "sampleRateHertz": sample_rate,
                    "audioChannelCount": self.settings.audio_channels,
                },
                "folderId": self.settings.yandex_folder_id,
            },
            "audio": {"uri": audio_url},
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=self.headers, json=body)
            if response.status_code != 200:
                raise Exception(f"ASR long recognition failed: {response.status_code}")
            return response.json().get("id", "")
