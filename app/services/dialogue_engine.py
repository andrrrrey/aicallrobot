"""Dialogue engine: AI-классификация намерений и генерация персонализированных ответов."""

import re
from loguru import logger
from app.services.yandex_gpt import YandexGPTService
from app.services.knowledge_base import KnowledgeBaseService


_INTENT_PROMPT = """Определи намерение клиента по его реплике в телефонном разговоре.
Ответь ОДНИМ словом на английском: positive, negative, objection или unknown.

Правила:
- positive: клиент согласен, заинтересован, отвечает "да"
- negative: клиент отказывается, не заинтересован, отвечает "нет"
- objection: клиент возражает, задаёт уточняющие вопросы, сомневается
- unknown: непонятно или нет ответа

Реплика клиента: "{text}"
"""

_OBJECTION_SYSTEM = (
    "Ты — профессиональный AI-ассистент, ведёшь исходящий звонок. "
    "Клиент высказал возражение. Ответь эмпатично и профессионально, "
    "2-3 предложения на русском языке. "
    "Не давай ложных обещаний, отвечай честно."
)


class DialogueEngine:
    """AI-движок диалога: классификация намерений и генерация ответов."""

    def __init__(self, gpt_service: YandexGPTService, kb_service: KnowledgeBaseService):
        self.gpt = gpt_service
        self.kb = kb_service

    async def classify_intent(self, text: str) -> str:
        """
        Классифицирует намерение клиента.
        Возвращает: "positive" | "negative" | "objection" | "unknown"
        """
        if not text or not text.strip():
            return "unknown"

        try:
            messages = [{"role": "user", "text": _INTENT_PROMPT.format(text=text)}]
            result = await self.gpt.complete(messages, temperature=0.1, max_tokens=10)
            result = result.strip().lower()

            if result in ("positive", "negative", "objection", "unknown"):
                return result

            # Русскоязычный фоллбэк (если GPT ответил по-русски)
            text_lower = text.lower()
            if any(w in result for w in ("да", "согласен", "интересно", "хорошо", "ладно", "конечно")):
                return "positive"
            if any(w in result for w in ("нет", "отказ", "не нужно", "не интересно")):
                return "negative"
            if any(w in result for w in ("возражен", "но ", "почему", "зачем", "дорого")):
                return "objection"

            # Анализ исходного текста клиента как запасной вариант
            if any(w in text_lower for w in ("да", "хорошо", "конечно", "согласен", "интересно", "расскажите")):
                return "positive"
            if any(w in text_lower for w in ("нет", "не надо", "не интересно", "откажусь", "не хочу")):
                return "negative"
            if any(w in text_lower for w in ("почему", "зачем", "дорого", "не уверен", "подумаю", "сомневаюсь")):
                return "objection"

            return "unknown"
        except Exception as e:
            logger.error(f"classify_intent error: {e}")
            return "unknown"

    async def generate_response(
        self,
        step,
        transcript: list[dict],
        knowledge_context: list[str],
        ai_config: dict,
    ) -> str:
        """
        Генерирует AI-ответ для текущего шага диалога.

        Args:
            step: ScenarioStep с полями id, greeting, prompt
            transcript: история разговора
            knowledge_context: релевантные чанки из базы знаний
            ai_config: {"system_prompt": str, "scenario_context": str}
        """
        system_parts = []

        base_prompt = ai_config.get("system_prompt", "").strip()
        if base_prompt:
            system_parts.append(base_prompt)
        else:
            system_parts.append(
                "Ты — AI-ассистент по имени Алиса, ведёшь исходящий звонок. "
                "Веди вежливый деловой диалог на русском языке. "
                "Отвечай кратко — 2-3 предложения максимум."
            )

        scenario_ctx = ai_config.get("scenario_context", "").strip()
        if scenario_ctx:
            system_parts.append(f"Контекст сценария:\n{scenario_ctx}")

        if knowledge_context:
            system_parts.append(
                "Релевантная информация из базы знаний:\n" +
                "\n---\n".join(knowledge_context)
            )

        step_task = (step.prompt or step.greeting or "").strip()
        if step_task:
            system_parts.append(f"Текущая задача шага '{step.id}': {step_task}")

        # Если робот уже говорил и это не шаг первого приветствия ЛПР — запретить повторное приветствие
        GREETING_STEPS = {"lpr_greeting", "lpr_found"}
        already_greeted = any(e.get("role") == "robot" for e in transcript)
        if already_greeted and step and step.id not in GREETING_STEPS:
            system_parts.append(
                "ВАЖНО: приветствие уже произнесено в начале разговора. "
                "НЕ начинай ответ с нового приветствия («Добрый день», «Здравствуйте» и т.п.) "
                "и не представляйся заново. Продолжай разговор естественно по контексту диалога."
            )

        messages = [{"role": "system", "text": "\n\n".join(system_parts)}]

        # Добавляем последние 6 записей транскрипта (3 обмена)
        for entry in transcript[-6:]:
            role = "assistant" if entry.get("role") == "robot" else "user"
            messages.append({"role": role, "text": entry.get("text", "")})

        return await self.gpt.complete(messages)

    async def generate_with_intent(
        self,
        step,
        transcript: list[dict],
        knowledge_context: list[str],
        ai_config: dict,
    ) -> tuple[str, str]:
        """
        Один GPT-вызов вместо двух: возвращает (intent, response_text).

        Экономит ~400 мс по сравнению с раздельными classify_intent + generate_response.
        Intent встроен в конец ответа как тег [INTENT:X] и отрезается перед отдачей клиенту.
        """
        system_parts = []

        base_prompt = ai_config.get("system_prompt", "").strip()
        system_parts.append(
            base_prompt or
            "Ты — AI-ассистент Алиса, ведёшь исходящий звонок. "
            "Отвечай кратко, 1–2 предложения, на русском языке."
        )

        scenario_ctx = ai_config.get("scenario_context", "").strip()
        if scenario_ctx:
            system_parts.append(f"Контекст сценария:\n{scenario_ctx}")

        if knowledge_context:
            system_parts.append(
                "Релевантная информация из базы знаний:\n" +
                "\n---\n".join(knowledge_context)
            )

        step_task = (step.prompt or step.greeting or "").strip() if step else ""
        if step_task:
            system_parts.append(f"Текущая задача шага '{step.id}': {step_task}")

        GREETING_STEPS = {"lpr_greeting", "lpr_found"}
        already_greeted = any(e.get("role") == "robot" for e in transcript)
        if already_greeted and step and step.id not in GREETING_STEPS:
            system_parts.append(
                "ВАЖНО: приветствие уже произнесено. "
                "НЕ начинай ответ с нового приветствия. "
                "Продолжай разговор естественно."
            )

        system_parts.append(
            "После своего ответа, на отдельной строке, укажи намерение последней реплики собеседника:\n"
            "[INTENT:positive] — согласен, подтверждает, отвечает «да»\n"
            "[INTENT:negative] — отказывается, не хочет, «нет»\n"
            "[INTENT:objection] — возражает, задаёт вопрос, сомневается, просит объяснить\n"
            "[INTENT:unknown] — неясно или просто приветствие"
        )

        messages = [{"role": "system", "text": "\n\n".join(system_parts)}]
        for entry in transcript[-6:]:
            role = "assistant" if entry.get("role") == "robot" else "user"
            messages.append({"role": role, "text": entry.get("text", "")})

        raw = await self.gpt.complete(messages)

        # Извлекаем тег [INTENT:X] из конца ответа
        intent = "unknown"
        response_text = raw.strip()
        match = re.search(
            r'\[INTENT:(positive|negative|objection|unknown)\]\s*$',
            raw.strip(),
            re.IGNORECASE | re.MULTILINE,
        )
        if match:
            intent = match.group(1).lower()
            response_text = raw[:match.start()].strip()

        logger.debug(f"generate_with_intent → intent={intent}, response='{response_text[:80]}...'")
        return intent, response_text

    async def handle_objection(
        self,
        text: str,
        transcript: list[dict],
        knowledge_context: list[str],
        ai_config: dict | None = None,
        step=None,
    ) -> str:
        """
        Генерирует ответ на возражение клиента.
        Дополнительно ищет в базе знаний информацию по теме возражения.
        """
        # Дополнительный поиск по теме возражения
        extra_context = await self.kb.search(text, n_results=3)
        combined = list(dict.fromkeys(knowledge_context + extra_context))  # deduplicate, preserve order

        # Используем AI config (кастомный промпт) если есть, иначе — дефолтный
        base_prompt = (ai_config or {}).get("system_prompt", "").strip()
        if base_prompt:
            system_parts = [base_prompt]
            scenario_ctx = (ai_config or {}).get("scenario_context", "").strip()
            if scenario_ctx:
                system_parts.append(f"Контекст сценария:\n{scenario_ctx}")
            if combined:
                system_parts.append("Релевантная информация:\n" + "\n---\n".join(combined))
            if step:
                step_task = (step.prompt or "").strip()
                if step_task:
                    system_parts.append(f"Текущий шаг '{step.id}': {step_task}")
            system = "\n\n".join(system_parts)
        else:
            system = _OBJECTION_SYSTEM
            if combined:
                system += "\n\nРелевантная информация:\n" + "\n---\n".join(combined)

        messages = [{"role": "system", "text": system}]
        # Последние 6 реплик для контекста
        for entry in transcript[-6:]:
            role = "assistant" if entry.get("role") == "robot" else "user"
            messages.append({"role": role, "text": entry.get("text", "")})

        return await self.gpt.complete(messages)
