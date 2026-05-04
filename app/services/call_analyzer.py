"""Call analyzer: генерация саммари и квалификация клиента после завершения звонка."""

import json
import re
from loguru import logger
from app.services.yandex_gpt import YandexGPTService


_SUMMARY_PROMPT = """Ты — аналитик звонков. Проанализируй транскрипт и составь краткий отчёт.

Сценарий: {scenario_name}

Транскрипт разговора:
{transcript_text}

Составь отчёт строго в следующем формате:
ИТОГ: [одно предложение — результат звонка]
КЛЮЧЕВЫЕ МОМЕНТЫ:
- [момент 1]
- [момент 2]
СЛЕДУЮЩИЙ ШАГ: [рекомендация что делать дальше]
"""

_QUALIFY_PROMPT = """Определи статус клиента по результатам телефонного разговора.

Транскрипт:
{transcript_text}

Ответь строго в формате JSON (без лишних символов):
{{"status": "interested", "reasoning": "краткое объяснение"}}

Возможные значения status:
- "interested" — клиент заинтересован, готов продолжить общение
- "callback" — клиент просит перезвонить или взял паузу
- "not_interested" — клиент отказался
- "unknown" — результат неясен из-за короткого разговора
"""


class CallAnalyzer:
    """Анализирует завершённый звонок: генерирует саммари и квалификацию клиента."""

    def __init__(self, gpt_service: YandexGPTService):
        self.gpt = gpt_service

    async def generate_summary(self, transcript: list[dict], scenario) -> str:
        """
        Генерирует структурированное саммари разговора.
        Возвращает строку с разделами ИТОГ / КЛЮЧЕВЫЕ МОМЕНТЫ / СЛЕДУЮЩИЙ ШАГ.
        """
        if not transcript:
            return "Разговор не состоялся."

        transcript_text = self._format_transcript(transcript)
        scenario_name = getattr(scenario, "name", "Неизвестный сценарий")

        prompt = _SUMMARY_PROMPT.format(
            scenario_name=scenario_name,
            transcript_text=transcript_text,
        )
        try:
            summary = await self.gpt.complete(
                [{"role": "user", "text": prompt}],
                temperature=0.3,
                max_tokens=600,
            )
            return summary.strip()
        except Exception as e:
            logger.error(f"generate_summary error: {e}")
            return f"Ошибка генерации саммари: {e}"

    async def qualify_client(self, transcript: list[dict]) -> dict:
        """
        Определяет статус клиента по транскрипту.
        Возвращает: {"status": str, "reasoning": str}
        """
        if not transcript:
            return {"status": "unknown", "reasoning": "Разговор не состоялся"}

        transcript_text = self._format_transcript(transcript)
        prompt = _QUALIFY_PROMPT.format(transcript_text=transcript_text)

        try:
            result = await self.gpt.complete(
                [{"role": "user", "text": prompt}],
                temperature=0.1,
                max_tokens=200,
            )
            # Извлекаем JSON из ответа (GPT иногда добавляет лишний текст)
            match = re.search(r'\{[^{}]+\}', result, re.DOTALL)
            if match:
                data = json.loads(match.group())
                status = data.get("status", "unknown")
                if status not in ("interested", "callback", "not_interested", "unknown"):
                    status = "unknown"
                return {"status": status, "reasoning": data.get("reasoning", "")}
        except Exception as e:
            logger.error(f"qualify_client error: {e}")

        return {"status": "unknown", "reasoning": ""}

    @staticmethod
    def _format_transcript(transcript: list[dict]) -> str:
        lines = []
        for entry in transcript:
            role_label = "Робот" if entry.get("role") == "robot" else "Клиент"
            lines.append(f"{role_label}: {entry.get('text', '')}")
        return "\n".join(lines)
