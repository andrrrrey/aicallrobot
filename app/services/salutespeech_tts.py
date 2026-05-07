"""SaluteSpeech TTS — Sber SmartSpeech REST API v1."""

import uuid
import time
import httpx
from loguru import logger
from app.core.config import get_settings


class SaluteSpeechTTSService:
    """Синтез речи через SaluteSpeech (Sber SmartSpeech) REST API."""

    TOKEN_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    TTS_URL = "https://smartspeech.sber.ru/rest/v1/text:synthesize"

    VOICES = {
        "Neyra": {"gender": "female", "label": "Нейра (жен.)"},
        "Bor": {"gender": "male", "label": "Бор (муж.)"},
        "Marfa": {"gender": "female", "label": "Марфа (жен.)"},
        "Natasha": {"gender": "female", "label": "Наташа (жен.)"},
        "Boris": {"gender": "male", "label": "Борис (муж.)"},
        "Lika": {"gender": "female", "label": "Лика (жен.)"},
        "Tur": {"gender": "male", "label": "Тур (муж.)"},
        "Kirill": {"gender": "male", "label": "Кирилл (муж.)"},
        "Alexander": {"gender": "male", "label": "Александр (муж.)"},
    }

    # Sample rates supported by the API
    SAMPLE_RATES = [8000, 16000, 24000, 48000]

    def __init__(self):
        self.settings = get_settings()
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def _get_token(self) -> str:
        """Получить действующий OAuth-токен, обновив его при необходимости."""
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        auth_key = self.settings.salutespeech_auth_key
        if not auth_key:
            raise RuntimeError("SALUTESPEECH_AUTH_KEY не задан в настройках")

        headers = {
            "Authorization": f"Basic {auth_key}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"scope": self.settings.salutespeech_scope}

        async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
            resp = await client.post(self.TOKEN_URL, headers=headers, data=data)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"SaluteSpeech auth failed: {resp.status_code} {resp.text[:300]}"
                )

        payload = resp.json()
        self._token = payload["access_token"]
        # expires_at is in milliseconds
        self._token_expires_at = payload.get("expires_at", 0) / 1000.0
        logger.info("SaluteSpeech: токен получен")
        return self._token

    async def synthesize(
        self,
        text: str,
        voice: str | None = None,
        sample_rate: int | None = None,
    ) -> bytes:
        voice = voice or self.settings.salutespeech_voice
        sample_rate = sample_rate or self.settings.audio_sample_rate

        if voice not in self.VOICES:
            voice = "Neyra"

        # Align sample rate to nearest supported value
        supported = self.SAMPLE_RATES
        if sample_rate not in supported:
            sample_rate = min(supported, key=lambda r: abs(r - sample_rate))

        token = await self._get_token()

        params = {
            "voice": f"{voice}_{sample_rate}",
            "format": f"wav{sample_rate // 1000}",
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/text",
        }

        logger.info(f"SaluteSpeech TTS: voice={voice}, rate={sample_rate}, text='{text[:50]}...'")

        async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
            resp = await client.post(
                self.TTS_URL, params=params, headers=headers, content=text.encode("utf-8")
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"SaluteSpeech TTS failed: {resp.status_code} {resp.text[:300]}"
                )

        audio = resp.content
        logger.info(f"SaluteSpeech TTS ok: {len(audio)} bytes (WAV)")
        return audio

    @classmethod
    def get_voices_info(cls) -> list[dict]:
        return [
            {"id": vid, "label": info["label"], "gender": info["gender"]}
            for vid, info in cls.VOICES.items()
        ]
