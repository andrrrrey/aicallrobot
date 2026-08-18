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
    _is_goodbye,
    _is_callback_request,
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


def test_interruption_detection():
    # Перебивания: зов по имени, «стойте», «остановитесь», «дайте сказать»
    assert _is_hold_request("татьяна татьяна")
    assert _is_hold_request("странно стойте стойте")
    assert _is_hold_request("яна остановитесь татьяна")
    assert _is_hold_request("один момент")
    assert _is_hold_request("дайте сказать")
    assert _is_hold_request("не так быстро")
    # Осмысленный вопрос с именем — НЕ перебивание
    assert not _is_hold_request("татьяна какой ваш номер телефона я передам директору")


def test_hold_does_not_restart_dialog():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("hold")
    st.phase = "secretary"
    text, node = _run(eng._dispatch(st, "Татьяна подождите татьяна"))
    assert node == "hold_on"
    assert text == SCRIPT["hold_on"]
    # Фаза не изменилась — рестарта приветствия ЛПР не произошло
    assert st.phase == "secretary"


# ── Завершение по прощанию ─────────────────────────────────────────────────────

def test_goodbye_detection():
    assert _is_goodbye("спасибо до свидания")
    assert _is_goodbye("я кладу трубку")
    assert _is_goodbye("всего доброго")
    assert _is_goodbye("больше не звоните")
    # «Пока не скажу…» — НЕ прощание (не должно ловиться на «пока»)
    assert not _is_goodbye("да записал пока не скажу кого директор сам наберет спасибо")
    assert not _is_goodbye("подождите не соединяю")


def test_goodbye_closes_dialog():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("bye")
    st.phase = "secretary"
    phrase = "Яна остановитесь татьяна остановитесь я все понял все передам спасибо до свидания"
    text, node = _run(eng._dispatch(st, phrase))
    assert node == "farewell"
    assert text == SCRIPT["farewell"]
    assert st.phase == "closed"


# ── Рукопожатие: живой человек vs IVR/автоответчик ─────────────────────────────

def test_handshake_greeting_is_hello():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    g = eng.greeting("hs")
    assert g["phase"] == "handshake"
    assert g["robot_text"] == SCRIPT["handshake_hello"]


def test_handshake_human_starts_script():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("h1")
    r = _run(eng.process_turn("h1", "Алло, да, слушаю"))
    assert r["node"] == "greeting"
    assert r["phase"] == "secretary"
    assert r["robot_text"] == SCRIPT["greeting"]


def test_handshake_ivr_hangs_up():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("h2")
    r = _run(eng.process_turn("h2", "Вы позвонили в компанию. Нажмите 1 для отдела продаж"))
    assert r["node"] == "answering_machine"
    assert r["phase"] == "closed"
    assert r["robot_text"] == ""


def test_handshake_voicemail_hangs_up():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("h3")
    r = _run(eng.process_turn("h3", "Оставьте сообщение после сигнала"))
    assert r["node"] == "answering_machine"
    assert r["phase"] == "closed"


def test_handshake_unclear_asks_company():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("h4", company_name="РусГидроМонтаж")
    r = _run(eng.process_turn("h4", "кхх шшш"))
    assert r["node"] == "handshake_clarify"
    assert "РусГидроМонтаж" in r["robot_text"]
    # Подтверждение → запускаем скрипт
    r2 = _run(eng.process_turn("h4", "да"))
    assert r2["node"] == "greeting"
    assert r2["phase"] == "secretary"


def test_handshake_unclear_twice_hangs_up():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("h5")
    r1 = _run(eng.process_turn("h5", "..."))
    assert r1["node"] == "handshake_clarify"
    r2 = _run(eng.process_turn("h5", "брр шшш"))
    assert r2["node"] == "no_human"
    assert r2["phase"] == "closed"


def test_handshake_does_not_hang_up_on_ambiguous_human():
    # Живой человек, чья фраза похожа на «машинную», не должен получить трубку в лицо
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    for i, phrase in enumerate(["Перезвоните позже, я занят", "Это робот что ли?"]):
        eng.greeting(f"amb{i}")
        r = _run(eng.process_turn(f"amb{i}", phrase))
        assert r["node"] != "answering_machine"
        assert r["phase"] != "closed"


# ── Перепроверка: приветствие ЛПР говорим только после реального перевода ───────

def test_pickup_signal_does_not_trigger_transfer():
    # Голое «слушаю вас» от секретаря НЕ означает перевод на ЛПР —
    # робот не должен говорить «меня направили к вам, всё верно?»
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("pk")
    st.phase = "secretary"
    st.last_robot_text = SCRIPT["greeting"]
    text, node = _run(eng._handle_secretary(st, "да, слушаю вас"))
    assert node == "pickup_no_transfer"
    assert st.phase == "secretary"
    assert "направили к вам" not in text


def test_explicit_transfer_still_greets_lpr():
    # Явный перевод («соединяю») по-прежнему ведёт к приветствию ЛПР
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("tr")
    st.phase = "secretary"
    text, node = _run(eng._handle_secretary(st, "хорошо, соединяю"))
    assert node == "transfer_signal"
    assert st.phase == "lpr_greeting"
    assert text == SCRIPT["lpr_greeting"]


def test_bare_i_am_responsible_no_double_greeting():
    # «Кто отвечает за электрохозяйство?» → «я» → не представляемся заново,
    # сразу спрашиваем имя и переходим к теме
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("iam")
    st.phase = "secretary"
    st.last_robot_text = SCRIPT["greeting"]
    text, node = _run(eng._handle_secretary(st, "я"))
    assert node == "i_am_lpr"
    assert st.phase == "lpr_main"
    assert text == SCRIPT["secretary_i_am_lpr_topic"]
    assert "направили к вам" not in text
    assert "Добрый день" not in text  # без второго приветствия


def test_bare_i_am_ignored_without_responsible_question():
    # Одиночное «я» вне контекста вопроса «кто отвечает» не считаем ответом ЛПР
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("iam2")
    st.phase = "secretary"
    st.last_robot_text = "Всего доброго!"
    text, node = _run(eng._handle_secretary(st, "я"))
    assert node != "i_am_lpr"


# ── Перепроверка: «по какому вопросу» — это цель звонка, а не запрос номера ──────

def test_what_do_you_want_not_our_number():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("wdw")
    st.phase = "secretary"
    st.last_robot_text = SCRIPT["greeting"]
    text, node = _run(eng._handle_secretary(st, "а по какому вопросу вы звоните?"))
    assert node == "what_do_you_want"
    assert text == SCRIPT["secretary_what_do_you_want"]
    assert "восемьсот" not in text.lower()  # не диктуем наш номер


# ── Просьба перезвонить не должна уходить в «Я затрудняюсь ответить» ─────────────

def test_callback_request_detection():
    assert _is_callback_request("перезвоните позже пожалуйста")
    assert _is_callback_request("можете перезвонить завтра")
    assert _is_callback_request("давайте перезвоните попозже")
    # «Перезвоните на этот же номер» — это не «позвоните позже»
    assert not _is_callback_request("перезвоните на этот же номер")
    # С цифрами — диктуют номер, а не просят перезвонить
    assert not _is_callback_request("перезвоните на 89001234567")


def test_callback_in_secretary_not_debug():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("cb")
    _run(eng.process_turn("cb", "да, слушаю"))  # запускаем скрипт (секретарь)
    r = _run(eng.process_turn("cb", "перезвоните позже, сейчас некогда"))
    assert r["node"] == "call_back"
    assert r["robot_text"] == SCRIPT["secretary_call_back"]
    assert "затрудняюсь" not in r["robot_text"]


def test_callback_in_lpr_main():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("cbl")
    st.phase = "lpr_main"
    text, node = _run(eng._handle_lpr_main(st, "перезвоните завтра, сегодня занят"))
    assert node == "call_back"
    assert text == SCRIPT["lpr_call_back"]


# ── «У нас своя лаборатория» → уточняем: своя в штате или подрядчик ──────────────

def test_own_lab_clarify():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("lab")
    st.phase = "lpr_main"
    text, node = _run(eng._handle_lpr_main(st, "у нас своя электролаборатория"))
    assert node == "own_lab_staff"
    assert text == SCRIPT["lpr_own_lab_clarify"]
    assert "в штате" in text and "компания" in text
    assert st.lpr_works_clarify_asked is True
    # Ответ «компания» → работаем как со «своей компанией»
    text2, node2 = _run(eng._handle_lpr_main(st, "работаем с компанией"))
    assert text2 == SCRIPT["lpr_own_company_1"]


# ── Замена фразы ухода от темы ──────────────────────────────────────────────────

def test_off_topic_phrase_replaced():
    assert SCRIPT["off_topic_response"] == (
        "Это немного не по той теме, по которой я вам звоню."
    )
    assert "Хорошая попытка" not in SCRIPT["off_topic_response"]


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-o", "asyncio_mode=auto", "-v"]))
