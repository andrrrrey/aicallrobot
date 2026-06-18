"""AI Config Manager: хранение инструкций для ИИ и контекста сценария в JSON файле."""

import json
from pathlib import Path
from loguru import logger
from app.core.config import get_settings


DEFAULT_CONFIG = {
    "system_prompt": (
        "Ты — AI-ассистент по имени Татьяна, выполняешь исходящие звонки от имени компании. "
        "Веди вежливый деловой диалог на русском языке. "
        "Отвечай кратко — не более 2-3 предложений за раз. "
        "Если клиент не хочет разговаривать — вежливо прощайся. "
        "Если не знаешь ответа — предложи связаться со специалистом."
    ),
    "scenario_context": "",
}


class AIConfigManager:
    """Управляет конфигурацией ИИ (инструкции + контекст сценария)."""

    def __init__(self):
        self.settings = get_settings()
        self._config_path = Path(self.settings.ai_config_file)
        self._config: dict = {}
        self._load()

    def _load(self):
        if self._config_path.exists():
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self._config = {**DEFAULT_CONFIG, **loaded}
                logger.info(f"AI config loaded from {self._config_path}")
                return
            except Exception as e:
                logger.error(f"Failed to load AI config: {e}")
        self._config = DEFAULT_CONFIG.copy()
        logger.info("AI config initialized with defaults")

    def get(self) -> dict:
        """Возвращает текущую конфигурацию ИИ."""
        return self._config.copy()

    def save(self, system_prompt: str, scenario_context: str) -> dict:
        """Сохраняет конфигурацию ИИ в JSON файл."""
        self._config = {
            "system_prompt": system_prompt,
            "scenario_context": scenario_context,
        }
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)
            logger.info("AI config saved")
        except Exception as e:
            logger.error(f"Failed to save AI config: {e}")
        return self._config.copy()
