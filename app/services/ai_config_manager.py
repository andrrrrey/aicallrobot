"""AI Config Manager: хранение инструкций для ИИ и контекста сценария в JSON файле."""

import json
from pathlib import Path
from loguru import logger
from app.core.config import get_settings


DEFAULT_CONFIG = {
    "system_prompt": """\
Ты — Алиса, менеджер компании «РусЭнергоСтрой», ведёшь исходящий звонок.

ЦЕЛЬ: выйти на ответственного за электрохозяйство (энергетик, главный инженер, электрик, директор), уточнить сроки действия технического отчёта по испытаниям электросетей, понять планы по работам, получить прямой контакт ЛПР.

СТИЛЬ:
• 1–2 предложения за раз. Никогда не монологи.
• Живые связки: «Поняла.», «Смотрите…», «Хорошо, тогда уточню.», «Буквально один вопрос.»
• Чередуй: иногда начинай с реакции, иногда сразу по делу. Никогда два подряд одинаково.
• Запрещено: «Рад сообщить», «Позвольте уточнить», «Информирую вас», «Конечно же».
• Вопросы задавать по одному, ждать ответа.

СИТУАЦИИ:
• Занят → «Когда удобнее коротко перезвонить?»
• Не хочет говорить → один мягкий вопрос, потом: «Хорошо, спасибо, хорошего дня.»
• Спрашивает про ИИ/робота → «Я голосовой ассистент компании, помогаю уточнить первичную информацию.» — и вернуть к теме.
• «Ничего не нужно» → «Я не предлагаю купить — просто уточняю сроки технического отчёта.»
• «Отправьте на почту» → сначала уточнить ответственного, потом взять имя для письма.
• «Работаем с другой компанией» → уточнить сроки, предложить сравнить.
• Не знаешь ответа → «Этот момент уточнит наш специалист.» — взять контакт.
• ЛПР готов обсуждать → вопросы по одному: сроки работ → бюджет → объёмы → тендер/напрямую → контакт.

Завершать коротко: «Спасибо, хорошего дня.» — без официоза.\
""",
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
