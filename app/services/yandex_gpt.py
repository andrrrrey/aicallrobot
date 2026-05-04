"""Yandex GPT service: генерация текста через Yandex Foundation Models API."""

import httpx
from loguru import logger
from app.core.config import get_settings

# Переиспользуемый HTTP-клиент с keep-alive (создаётся один раз на процесс)
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=25.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _http_client


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

    def _model_uri(self, model: str | None = None) -> str:
        m = model or self.settings.yandex_gpt_model
        return f"gpt://{self.settings.yandex_folder_id}/{m}"

    async def complete(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> str:
        """
        Генерирует ответ от Yandex GPT.

        Args:
            messages: [{"role": "system"|"user"|"assistant", "text": "..."}]
            temperature: 0.0–1.0, по умолчанию из конфига
            max_tokens: по умолчанию из конфига
            model: переопределить модель (напр. "yandexgpt-lite/latest")
        """
        body = {
            "modelUri": self._model_uri(model),
            "completionOptions": {
                "stream": False,
                "temperature": temperature if temperature is not None else self.settings.yandex_gpt_temperature,
                "maxTokens": str(max_tokens if max_tokens is not None else self.settings.yandex_gpt_max_tokens),
            },
            "messages": messages,
        }

        try:
            client = _get_client()
            response = await client.post(
                self.COMPLETION_URL,
                headers=self._headers(),
                json=body,
            )
            response.raise_for_status()
            data = response.json()
            text = data["result"]["alternatives"][0]["message"]["text"]
            logger.debug(f"YandexGPT ({model or self.settings.yandex_gpt_model}): {text[:80]}...")
            return text
        except httpx.HTTPStatusError as e:
            logger.error(f"YandexGPT HTTP {e.response.status_code}: {e.response.text[:200]}")
            raise
        except Exception as e:
            logger.error(f"YandexGPT error: {e}")
            raise
