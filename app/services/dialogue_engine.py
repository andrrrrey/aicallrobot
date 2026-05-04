"""Dialogue engine: AI-классификация намерений и генерация персонализированных ответов."""

import re
from loguru import logger
from app.services.yandex_gpt import YandexGPTService
from app.services.knowledge_base import KnowledgeBaseService

# Быстрая модель для диалога — yandexgpt-lite в 3-5 раз быстрее полной
_FAST_MODEL = "yandexgpt-lite/latest"
_VOICE_MAX_TOKENS = 80    # ~1 предложение — быстрее генерация и TTS
_INTENT_MAX_TOKENS = 10

# Враппер минимальный — вся личность/стиль задаётся через system_prompt в ai_config
_COMBINED_SYSTEM = """Веди телефонный разговор строго по инструкции ниже.
Одно-два предложения за ответ, живо и по-человечески — без монологов, без канцелярита.

{context}

Ответь строго в формате (две строки, ничего лишнего):
INTENT: <positive|negative|objection|unknown>
RESPONSE: <твой ответ>"""

_OBJECTION_SYSTEM = """Веди телефонный разговор строго по инструкции ниже.
Клиент возразил. Ответь ОДНИМ предложением: прими возражение + задай вопрос или предложи альтернативу.
Без клише "Понимаю ваши опасения" и "Позвольте объяснить".

{context}

Ответь строго в формате:
INTENT: objection
RESPONSE: <твой ответ>"""


def _parse_combined(raw: str) -> tuple[str, str]:
    """Разбирает ответ формата INTENT: ... / RESPONSE: ..."""
    intent = "unknown"
    response = raw.strip()

    m_intent = re.search(r'INTENT:\s*(\w+)', raw, re.IGNORECASE)
    m_resp = re.search(r'RESPONSE:\s*(.+)', raw, re.IGNORECASE | re.DOTALL)

    if m_intent:
        w = m_intent.group(1).lower()
        if w in ("positive", "negative", "objection", "unknown"):
            intent = w

    if m_resp:
        response = m_resp.group(1).strip()
    elif m_intent:
        # GPT ответил только INTENT без RESPONSE — берём весь текст без строки с INTENT
        response = re.sub(r'INTENT:\s*\w+\s*', '', raw, flags=re.IGNORECASE).strip()

    return intent, response or raw.strip()


class DialogueEngine:
    """AI-движок диалога: объединённая классификация намерений + генерация ответов."""

    def __init__(self, gpt_service: YandexGPTService, kb_service: KnowledgeBaseService):
        self.gpt = gpt_service
        self.kb = kb_service

    async def classify_and_respond(
        self,
        user_text: str,
        step,
        transcript: list[dict],
        knowledge_context: list[str],
        ai_config: dict,
    ) -> tuple[str, str]:
        """
        Определяет намерение И генерирует ответ за ОДИН GPT-вызов.
        Использует yandexgpt-lite для минимальной задержки.

        Returns: (intent, response_text)
        """
        context_parts = []

        base = ai_config.get("system_prompt", "").strip()
        if base:
            context_parts.append(base)

        scenario = ai_config.get("scenario_context", "").strip()
        if scenario:
            context_parts.append(f"Сценарий:\n{scenario}")

        if knowledge_context:
            context_parts.append("Релевантная информация:\n" + "\n---\n".join(knowledge_context))

        step_task = (getattr(step, "prompt", "") or getattr(step, "greeting", "") or "").strip()
        if step_task:
            context_parts.append(f"Твоя задача сейчас: {step_task}")

        system = _COMBINED_SYSTEM.format(context="\n\n".join(context_parts))

        messages = [{"role": "system", "text": system}]
        for entry in transcript[-6:]:
            role = "assistant" if entry.get("role") == "robot" else "user"
            messages.append({"role": role, "text": entry.get("text", "")})
        messages.append({"role": "user", "text": user_text})

        try:
            raw = await self.gpt.complete(
                messages,
                temperature=0.5,
                max_tokens=_VOICE_MAX_TOKENS,
                model=_FAST_MODEL,
            )
            return _parse_combined(raw)
        except Exception as e:
            logger.error(f"classify_and_respond error: {e}")
            fallback = getattr(step, "greeting", "") or "Понял. Продолжим?"
            return "unknown", fallback

    async def handle_objection_fast(
        self,
        user_text: str,
        transcript: list[dict],
        knowledge_context: list[str],
        ai_config: dict,
    ) -> tuple[str, str]:
        """
        Быстрый ответ на возражение за один GPT-вызов.
        Returns: ("objection", response_text)
        """
        context_parts = []
        base = ai_config.get("system_prompt", "").strip()
        if base:
            context_parts.append(base)
        if knowledge_context:
            context_parts.append("Релевантная информация:\n" + "\n---\n".join(knowledge_context))

        system = _OBJECTION_SYSTEM.format(context="\n\n".join(context_parts))

        messages = [{"role": "system", "text": system}]
        for entry in transcript[-4:]:
            role = "assistant" if entry.get("role") == "robot" else "user"
            messages.append({"role": role, "text": entry.get("text", "")})
        messages.append({"role": "user", "text": user_text})

        try:
            raw = await self.gpt.complete(
                messages,
                temperature=0.5,
                max_tokens=_VOICE_MAX_TOKENS,
                model=_FAST_MODEL,
            )
            _, response = _parse_combined(raw)
            return "objection", response
        except Exception as e:
            logger.error(f"handle_objection_fast error: {e}")
            return "objection", "Понимаю. Давайте уточним детали?"

    # --- Legacy methods kept for /api/v1/ai/chat endpoint ---

    async def classify_intent(self, text: str) -> str:
        """Классификация намерения (используется в чат-тесте)."""
        if not text or not text.strip():
            return "unknown"
        try:
            prompt = (
                "Определи намерение клиента ОДНИМ словом: positive, negative, objection, unknown.\n"
                f"Реплика: {text}"
            )
            result = await self.gpt.complete(
                [{"role": "user", "text": prompt}],
                temperature=0.1,
                max_tokens=_INTENT_MAX_TOKENS,
                model=_FAST_MODEL,
            )
            w = result.strip().lower()
            if w in ("positive", "negative", "objection", "unknown"):
                return w
            t = text.lower()
            if any(x in t for x in ("да", "хорошо", "конечно", "согласен", "интересно")):
                return "positive"
            if any(x in t for x in ("нет", "не надо", "не интересно", "откажусь")):
                return "negative"
            if any(x in t for x in ("почему", "дорого", "не уверен", "подумаю")):
                return "objection"
            return "unknown"
        except Exception as e:
            logger.error(f"classify_intent error: {e}")
            return "unknown"

    async def generate_response(self, step, transcript, knowledge_context, ai_config) -> str:
        """Генерация ответа (используется в /api/v1/ai/chat)."""
        _, response = await self.classify_and_respond(
            user_text=transcript[-1].get("text", "") if transcript else "",
            step=step,
            transcript=transcript[:-1],
            knowledge_context=knowledge_context,
            ai_config=ai_config,
        )
        return response
