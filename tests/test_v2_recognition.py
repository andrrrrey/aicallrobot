#!/usr/bin/env python3
"""Тесты исправлений распознавания в алгоритме диалога v2.

Покрывают четыре бага:
1. Телефонный номер читается как единое число → нормализация по группам.
2. «Я не могу соединить, передайте информацию — я передам» → relay_message.
3. «Не записал, повторите ещё раз» → повтор прошлой реплики (а не рестарт).
4. «Татьяна, подождите, Татьяна» → «Да, я вас слушаю» (а не рестарт диалога).

Запуск: python -m pytest tests/test_v2_recognition.py -o asyncio_mode=auto
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.text_normalize import normalize_for_tts
from app.services.script_dialogue_v2 import (
    ScriptDialogueV2,
    _is_hold_request,
    _is_repeat_request,
    _keyword_intent,
)
from app.services.script_v2_data import SCRIPT


class _FakeGPT:
    """Заглушка ИИ-классификатора — всегда возвращает unknown."""

    async def classify(self, *a, **k):
        return "unknown"

    async def complete(self, *a, **k):
        return "unknown"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Баг 1: произношение телефонного номера ─────────────────────────────────────

def test_phone_number_read_by_groups():
    out = normalize_for_tts("Запишите, 8 800 775 96 31, Татьяна.")
    assert "восемьсот" in out
    assert "8 800" not in out  # цифры заменены словами
    assert out.startswith("Запишите, восемь восемьсот семьсот семьдесят пять")


def test_short_numbers_untouched():
    # Малые числа (кВ, адрес, проценты) не трогаем — их TTS читает верно
    src = "лицензия до 220 киловольт, дом 5а, 10% скидка, 2 месяца"
    assert normalize_for_tts(src) == src


def test_no_digits_passthrough():
    src = "Восемь — восемьсот — семь — семь — пять."
    assert normalize_for_tts(src) == src


# ── Баг 2: relay_message ───────────────────────────────────────────────────────

def test_relay_message_keyword():
    assert _keyword_intent(
        "ну я не могу соединить вы передайте информацию я передам"
    ) == "relay_message"
    assert _keyword_intent("давайте я передам ему") == "relay_message"


def test_relay_message_response():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("relay")
    text, node = _run(
        eng._handle_secretary(st, "Ну я не могу соединить вы передайте информацию я передам")
    )
    assert node == "relay_message"
    assert text == SCRIPT["secretary_relay_message"]


# ── Баг 3: повтор реплики ──────────────────────────────────────────────────────

def test_repeat_request_detection():
    assert _is_repeat_request("нет не записал повторите еще раз")
    assert _is_repeat_request("я не расслышал")
    assert not _is_repeat_request("не понял вас")
    assert not _is_repeat_request("да записал спасибо")


def test_repeat_returns_last_robot_text():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("rep")
    st.last_robot_text = "Восемь — восемьсот — семь. От кого мне ждать звонка?"
    st.awaiting_callback_name = True
    text, node = _run(eng._dispatch(st, "Нет не записал повторите еще раз"))
    assert node == "repeat"
    assert text == st.last_robot_text
    # Контекст ожидания имени не сброшен — следующий ответ примем как имя
    assert st.awaiting_callback_name is True


# ── Баг 4: просьба подождать ───────────────────────────────────────────────────

def test_hold_request_detection():
    assert _is_hold_request("татьяна подождите татьяна")
    assert _is_hold_request("минутку")
    assert _is_hold_request("оставайтесь на линии")
    # Есть иной смысл — это не чистая просьба подождать
    assert not _is_hold_request("подождите подождите я не соединяю")
    assert not _is_hold_request("секунду сейчас позову директора")


def test_hold_does_not_restart_dialog():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("hold")
    st.phase = "secretary"
    text, node = _run(eng._dispatch(st, "Татьяна подождите татьяна"))
    assert node == "hold_on"
    assert text == SCRIPT["hold_on"]
    # Фаза не изменилась — рестарта приветствия ЛПР не произошло
    assert st.phase == "secretary"


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-o", "asyncio_mode=auto", "-v"]))
