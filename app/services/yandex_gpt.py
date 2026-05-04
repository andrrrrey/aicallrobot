"""Yandex GPT service: генерация текста через Yandex Foundation Models API."""

import httpx
from loguru import logger
from app.core.config import get_settings


class YandexGPTService:
    """Клиент Yandex GPT (Foundation Models v1)."""

    COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    def __init__(self):
        self.settings = get_settings()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Api-Key {self.settings.yandex_api_key}",
            "x-folder-id": self.settings.yandex_folder_id,
            "Content-Type": "application/json",
        }

    def _model_uri(self) -> str:
        return f"gpt://{self.settings.yandex_folder_id}/{self.settings.yandex_gpt_model}"

    async def complete(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """
        Генерирует ответ от Yandex GPT.

        Args:
            messages: список сообщений в формате
                      [{"role": "system"|"user"|"assistant", "text": "..."}]
            temperature: температура (0.0–1.0), по умолчанию из конфига
            max_tokens: максимальное число токенов, по умолчанию из конфига

        Returns:
            Текст ответа ассистента.
        """
        body = {
            "modelUri": self._model_uri(),
            "completionOptions": {
                "stream": False,
                "temperature": temperature if temperature is not None else self.settings.yandex_gpt_temperature,
                "maxTokens": str(max_tokens if max_tokens is not None else self.settings.yandex_gpt_max_tokens),
            },
            "messages": messages,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.COMPLETION_URL,
                    headers=self._headers(),
                    json=body,
                )
                response.raise_for_status()
                data = response.json()
                text = data["result"]["alternatives"][0]["message"]["text"]
                logger.debug(f"YandexGPT response: {text[:100]}...")
                return text
        except httpx.HTTPStatusError as e:
            logger.error(f"YandexGPT HTTP error {e.response.status_code}: {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"YandexGPT error: {e}")
            raise
