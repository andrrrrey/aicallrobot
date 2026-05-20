"""Dialogue engine: AI-классификация намерений и генерация персонализированных ответов."""

import asyncio
import re
from loguru import logger
from app.services.yandex_gpt import YandexGPTService, SafetyRefusalError
from app.services.knowledge_base import KnowledgeBaseService


_INTENT_PROMPT = """Определи намерение клиента в телефонном разговоре.
Ответь ОДНИМ словом на английском: positive, negative, objection или unknown.

Текущий шаг разговора: "{step_id}"
Последняя фраза робота: "{last_robot}"
Реплика клиента: "{text}"

Правила:
- positive: клиент согласен, заинтересован, готов продолжить
- negative: клиент КАТЕГОРИЧЕСКИ отказывается от продолжения разговора целиком
- objection: клиент возражает, задаёт вопрос, отвечает "нет" на конкретный вопрос (а не отказывается от разговора)
- unknown: непонятно или нет ответа

Важно: если клиент отвечает "нет" на конкретный вопрос (например, "нет, не планирую") — это objection, а не negative. negative — только если клиент явно отказывается продолжать разговор ("не хочу разговаривать", "не интересно, до свидания").

Для шагов "secretary_objection" и "start" дополнительно:
- Слова передачи трубки («соединяю», «переведу», «сейчас переведу», «передаю трубку», «подождите», «минутку», «оставайтесь на линии», «переключаю») → positive
- Слова нового собеседника, берущего трубку («алло», «слушаю», «да-да», «я вас слушаю», «да слушаю») → positive (ЛПР взял трубку)
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

    async def classify_intent(self, text: str, step_id: str = "", last_robot: str = "") -> str:
        """
        Классифицирует намерение клиента.
        Возвращает: "positive" | "negative" | "objection" | "unknown"
        """
        if not text or not text.strip():
            return "unknown"

        try:
            prompt = _INTENT_PROMPT.format(
                text=text,
                step_id=step_id or "unknown",
                last_robot=last_robot or "",
            )
            messages = [{"role": "user", "text": prompt}]
            result = await self.gpt.complete(messages, temperature=0.1, max_tokens=10)
            result = result.strip().lower()

            if result in ("positive", "negative", "objection", "unknown"):
                return result

            # Русскоязычный фоллбэк (если GPT ответил по-русски)
            text_lower = text.lower()
            if any(w in result for w in ("да", "согласен", "интересно", "хорошо", "ладно", "конечно")):
                return "positive"
            if any(w in result for w in ("нет", "отказ", "не нужно", "не интересно")):
                return "objection"
            if any(w in result for w in ("возражен", "но ", "почему", "зачем", "дорого")):
                return "objection"

            # Анализ исходного текста клиента как запасной вариант
            if any(w in text_lower for w in ("да", "хорошо", "конечно", "согласен", "интересно", "расскажите")):
                return "positive"
            # "нет" без явного отказа от разговора → objection, а не negative
            if any(w in text_lower for w in ("не хочу разговаривать", "не интересно до свидания", "не звоните")):
                return "negative"
            if any(w in text_lower for w in ("нет", "не надо", "не планирую", "не интересно", "откажусь", "не хочу")):
                return "objection"
            if any(w in text_lower for w in ("почему", "зачем", "дорого", "не уверен", "подумаю", "сомневаюсь")):
                return "objection"

            # Детекция сигналов передачи трубки (специально для шагов с секретарём)
            if step_id in ("secretary_objection", "start"):
                transfer_phrases = (
                    "соединяю", "переведу", "передаю трубку", "подождите",
                    "минутку", "оставайтесь на линии", "переключаю", "сейчас переведу",
                    "алло", "слушаю", "да-да", "я вас слушаю",
                )
                if any(ph in text_lower for ph in transfer_phrases):
                    return "positive"

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
            entry_role = entry.get("role")
            if entry_role == "robot":
                role = "assistant"
            elif entry_role == "system":
                role = "system"
            else:
                role = "user"
            messages.append({"role": role, "text": entry.get("text", "")})

        try:
            return await self.gpt.complete(messages)
        except SafetyRefusalError:
            # Фильтр безопасности сработал на сложном промпте — повторяем с минимальным
            logger.warning(f"Safety refusal on step '{step.id if step else '?'}', retrying with minimal prompt")
            minimal_system = (
                "Ты — Алиса, менеджер компании «РусЭнергоСтрой», ведёшь деловой телефонный разговор. "
                "Отвечай кратко и по делу, 1-2 предложения на русском языке."
            )
            if step and (step.prompt or step.greeting):
                minimal_system += f"\nТекущая задача: {step.prompt or step.greeting}"
            minimal_messages = [{"role": "system", "text": minimal_system}]
            for entry in transcript[-4:]:
                role = "assistant" if entry.get("role") == "robot" else "user"
                minimal_messages.append({"role": role, "text": entry.get("text", "")})
            try:
                return await self.gpt.complete(minimal_messages)
            except SafetyRefusalError:
                logger.error("Safety refusal on minimal prompt too, using step fallback")
                return step.greeting if step and step.greeting else "Понял. Продолжайте, пожалуйста."

    async def generate_with_intent(
        self,
        step,
        transcript: list[dict],
        knowledge_context: list[str],
        ai_config: dict,
    ) -> tuple[str, str]:
        """
        Параллельный запуск classify_intent + generate_response через asyncio.gather.
        Оба вызова независимы → суммарное время = max(intent_time, response_time)
        вместо intent_time + response_time.
        """
        # Берём последнюю реплику клиента для классификации
        client_entries = [e for e in transcript if e.get("role") == "client"]
        last_text = client_entries[-1].get("text", "") if client_entries else ""

        # Берём последнюю фразу робота для контекста классификации
        robot_entries = [e for e in transcript if e.get("role") == "robot"]
        last_robot = robot_entries[-1].get("text", "") if robot_entries else ""

        step_id = step.id if step else ""

        intent_coro = self.classify_intent(last_text, step_id=step_id, last_robot=last_robot)
        response_coro = self.generate_response(step, transcript, knowledge_context, ai_config)

        results = await asyncio.gather(intent_coro, response_coro, return_exceptions=True)

        intent: str = results[0] if not isinstance(results[0], Exception) else "unknown"
        if isinstance(results[0], Exception):
            logger.error(f"classify_intent failed: {results[0]}")

        response_text: str = results[1] if not isinstance(results[1], Exception) else ""
        if isinstance(results[1], Exception):
            logger.error(f"generate_response failed: {results[1]}")
        if not response_text:
            # Фоллбэк: greeting шага или нейтральная фраза
            response_text = (step.greeting if step and step.greeting else "Понял. Уточните, пожалуйста.")

        logger.info(f"generate_with_intent → step={step_id}, intent={intent}, last_robot='{last_robot[:60]}', response='{response_text[:80]}'")
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
            entry_role = entry.get("role")
            if entry_role == "robot":
                role = "assistant"
            elif entry_role == "system":
                role = "system"
            else:
                role = "user"
            messages.append({"role": role, "text": entry.get("text", "")})

        return await self.gpt.complete(messages)
