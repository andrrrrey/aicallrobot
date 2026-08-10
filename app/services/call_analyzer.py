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
{{"status": "unknown", "reasoning": "краткое объяснение"}}

Возможные значения status:
- "interested" — клиент ЯВНО проявил интерес к предложению: согласился продолжить \
по сути, задал вопросы о продукте/условиях, попросил КП, дал контакт для связи.
- "callback" — клиент просит перезвонить или взял паузу.
- "not_interested" — клиент отказался.
- "unknown" — результат неясен: короткий разговор, клиент только ответил на звонок \
(«алло», «да», «слушаю», «добрый день»), поздоровался, но по сути предложения \
ничего не сказал, или разговор оборвался.

ВАЖНО: сам факт того, что человек взял трубку и поздоровался («алло», «добрый \
день, слушаю вас», «да-да») — это НЕ заинтересованность. Ставь "interested" ТОЛЬКО \
при явных сигналах интереса по сути предложения. Если таких сигналов нет — "unknown".
"""

# Приветствия/филлеры: слова и короткие фразы, которые сами по себе НЕ несут
# заинтересованности. Считаются и как целые реплики, и пословно — чтобы «Добрый
# день, слушаю вас» распадалось на приветственные токены и давало 0 содержательных.
_GREETING_FILLERS = {
    # целые реплики
    "алло да", "алло слушаю", "да слушаю", "да да слушаю", "слушаю вас",
    "я вас слушаю", "у аппарата", "на связи", "кто это", "кто говорит",
    # пословные компоненты приветствий/филлеров
    "алло", "аллё", "да", "дада", "да-да", "нуда", "слушаю", "говорите",
    "добрый", "доброе", "день", "вечер", "утро", "здравствуйте", "здрасте",
    "привет", "вас", "тебя", "аппарата", "связи", "кто", "это", "говорит",
    "у", "на", "угу", "ага", "так", "и", "ну", "а",
}


def _client_meaningful_words(transcript: list[dict]) -> int:
    """Считает «содержательные» слова клиента (без приветствий/филлеров).

    Нужно, чтобы не квалифицировать как «заинтересован» разговор, где клиент лишь
    взял трубку и поздоровался («Алло», «Добрый день, слушаю вас»).
    """
    import re as _re
    words = 0
    for e in transcript:
        if e.get("role") == "robot" or e.get("role") == "system":
            continue
        text = (e.get("text") or "").lower().strip()
        # Убираем пунктуацию, нормализуем пробелы
        norm = _re.sub(r"[^\w\s]", " ", text, flags=_re.UNICODE)
        norm = _re.sub(r"\s+", " ", norm).strip()
        if not norm:
            continue
        # Реплика целиком — приветствие/филлер? тогда не считаем.
        if norm in _GREETING_FILLERS:
            continue
        for w in norm.split():
            if w and w not in _GREETING_FILLERS:
                words += 1
    return words


_CAMPAIGN_ANALYSIS_PROMPT = """Ты — руководитель отдела телемаркетинга. Проанализируй \
результаты кампании обзвона и дай практические выводы для повышения результативности.

Кампания: {name}

Метрики кампании:
{metrics}

Примеры итогов состоявшихся разговоров (саммари):
{summaries}

Составь анализ на русском языке строго в формате:
ОБЩАЯ ОЦЕНКА: [2–3 предложения об эффективности кампании по метрикам]
ЧТО РАБОТАЕТ: [что идёт хорошо]
ПРОБЛЕМЫ И ВОЗРАЖЕНИЯ: [типичные отказы и на каком этапе теряются клиенты]
РЕКОМЕНДАЦИИ: [конкретные шаги: скрипт, время обзвона, голос, работа с возражениями]
"""


class CallAnalyzer:
    """Анализирует завершённый звонок: генерирует саммари и квалификацию клиента."""

    def __init__(self, gpt_service: YandexGPTService):
        self.gpt = gpt_service

    async def analyze_campaign(self, name: str, metrics_text: str, summaries_text: str) -> str:
        """Формирует аналитический отчёт по кампании на основе метрик и саммари звонков."""
        prompt = _CAMPAIGN_ANALYSIS_PROMPT.format(
            name=name or "—",
            metrics=metrics_text,
            summaries=summaries_text or "— (нет завершённых разговоров с саммари)",
        )
        result = await self.gpt.complete(
            [{"role": "user", "text": prompt}],
            temperature=0.4,
            max_tokens=1200,
        )
        return result.strip()

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

        # Защита от ложного «заинтересован»: если клиент по сути ничего не сказал
        # (только взял трубку и поздоровался — «Алло», «Добрый день, слушаю вас»),
        # квалификация недостоверна — сразу возвращаем unknown, не спрашивая GPT.
        if _client_meaningful_words(transcript) < 3:
            return {"status": "unknown",
                    "reasoning": "Клиент только ответил на звонок — заинтересованность не выражена"}

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
