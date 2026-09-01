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
    _contains_spelled_number,
    _is_dictating_number,
    _says_call_here,
    _guard_contact_code,
)
from app.services.script_v2_data import SCRIPT


class _FakeGPT:
    """Заглушка ИИ-классификатора — всегда возвращает unknown."""

    async def classify(self, *a, **k):
        return "unknown"

    async def complete(self, *a, **k):
        return "unknown"


def _run(coro):
    """Синхронный прогон корутины в собственном цикле событий.

    asyncio.get_event_loop() в Python 3.11+ падает, если предыдущий тест уже
    закрыл цикл, — из-за этого весь набор падал при запуске целиком.
    """
    return asyncio.run(coro)


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


def test_handshake_ivr_waits_then_hangs_up():
    """Голосовое меню: молчим и ждём живого человека, затем кладём трубку.

    Сразу класть трубку нельзя: в торговых центрах и приёмных трубку часто
    подхватывают уже после меню. Скрипт при этом не запускаем — иначе живой
    человек услышит середину разговора и не поймёт, кто звонит.
    """
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("h2")
    menu = "Вы позвонили в компанию. Нажмите 1 для отдела продаж"
    for _ in range(2):
        r = _run(eng.process_turn("h2", menu))
        assert r["node"] == "ivr_wait", r
        assert r["phase"] == "handshake"
        assert r["robot_text"] == ""
    r = _run(eng.process_turn("h2", menu))
    assert r["node"] == "answering_machine", r
    assert r["phase"] == "closed"
    assert r["robot_text"] == ""


def test_live_person_after_ivr_gets_full_greeting():
    """Живой человек после меню слышит стандартное приветствие целиком."""
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("h2b")
    ivr = ("Здравствуйте, вас приветствует торговый центр. По вопросам аренды "
           "нажмите цифру два, чтобы оставить сообщение нажмите цифру три")
    assert _run(eng.process_turn("h2b", ivr))["node"] == "ivr_wait"
    r = _run(eng.process_turn("h2b", "торговый центр леруа здравствуйте"))
    assert r["robot_text"] == SCRIPT["greeting"], r
    assert r["phase"] == "secretary"


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
    # Человек только что взял трубку — коротко переспрашиваем суть, но остаёмся
    # в фазе секретаря (приветствие ЛПР говорится только после перевода).
    # Полное представление НЕ повторяем (это раздражало клиентов).
    assert node == "reintroduce", node
    assert st.phase == "secretary"
    assert "направили к вам" not in text
    assert "Добрый день" not in text
    assert "меня зовут" not in text.lower()


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


# ══════════════════════════════════════════════════════════════════════════════
# Баги, найденные по расшифровкам реальных звонков
# ══════════════════════════════════════════════════════════════════════════════

# ── «Я затрудняюсь ответить на ваш вопрос» больше не звучит ─────────────────────

def test_debug_stub_never_spoken():
    """Отладочная заглушка не должна попадать в речь ни при каких входах.

    В расшифровках она звучала по 4–5 раз подряд: перехват unknown подменял ею
    все штатные фоллбэки скрипта.
    """
    assert not any("затрудняюсь" in v for v in SCRIPT.values())

    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("stub")
    _run(eng.process_turn("stub", "Алло"))
    answers = [
        _run(eng.process_turn("stub", f"невнятная реплика {i}"))["robot_text"]
        for i in range(3)
    ]
    assert not any("затрудняюсь" in a for a in answers), answers
    # Три разных ответа: переспрос → уточнение → оставляем контакты
    assert len(set(answers)) == 3, answers


def test_unknown_escalation_closes_call():
    """После трёх нераспознанных реплик робот прощается, а не крутится в петле."""
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("esc")
    _run(eng.process_turn("esc", "Алло"))
    for _ in range(2):
        _run(eng.process_turn("esc", "кхм кхм шшш"))
    last = _run(eng.process_turn("esc", "кхм кхм шшш"))
    assert last["node"] == "unknown_close", last
    assert last["phase"] == "closed", last


# ── Разговор «в сторону»: робот молчит и ждёт ───────────────────────────────────

def test_side_talk_keeps_silence():
    """«Дим Фёдорович, бот спрашивает…» — это не реплика роботу."""
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("side")
    st.phase = "secretary"
    st.last_robot_text = SCRIPT["greeting"]
    text, node = _run(eng._dispatch(st, "Дим Фёдорович, бот спрашивает, кто у нас за электрохозяйство отвечает"))
    assert node == "side_talk"
    assert text == ""
    assert st.awaiting_new_person is True
    # Контекст не сброшен: последняя реплика робота осталась прежней
    assert st.last_robot_text == SCRIPT["greeting"]


def test_handoff_greets_new_person_shortly():
    """Трубку передали без слова «соединяю» — коротко представляемся заново."""
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("handoff")
    st.phase = "secretary"
    st.awaiting_new_person = True
    text, node = _run(eng._dispatch(st, "Алло!"))
    assert node == "handoff_hello"
    assert text == SCRIPT["handoff_hello"]
    assert st.phase == "lpr_greeting"


def test_hold_request_expects_new_person():
    """«Подождите, сейчас позову» — следующее «Алло» уже от другого человека."""
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("hold2")
    st.phase = "secretary"
    _run(eng._dispatch(st, "Одну секунду, подождите"))
    assert st.awaiting_new_person is True


# ── «Телефон запишите» — собеседник ДИКТУЕТ номер, а не просит наш ──────────────

def test_record_phone_any_word_order():
    assert _keyword_intent("телефон запишите") == "says_record"
    assert _keyword_intent("запишите номер") == "says_record"
    # «продиктуйте ваш номер» — наоборот, запрос НАШЕГО контакта
    assert _keyword_intent("продиктуйте ваш номер") != "says_record"


def test_record_phone_does_not_dictate_our_number():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("rec")
    st.phase = "secretary"
    text, node = _run(eng._handle_secretary(st, "Телефон запишите"))
    assert node == "says_record", node
    assert text == SCRIPT["secretary_recording"]
    assert "восемьсот" not in text.lower()


def test_other_org_asks_for_their_number():
    """«Имущество в другом городе, туда звоните» — записываем чужой контакт."""
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("org")
    st.phase = "secretary"
    text, node = _run(eng._handle_secretary(st, "У нас всё это имущество в Нерюнгри, туда звоните"))
    assert node == "other_org", node
    assert text == SCRIPT["secretary_other_org"]
    # Номер продиктовали сразу → благодарим и закрываем
    st2 = eng.create_session("org2")
    st2.phase = "secretary"
    text2, node2 = _run(eng._handle_secretary(st2, "Звоните туда, телефон 8 800 775 96 31"))
    assert node2 == "other_org_number", node2


# ── Рукопожатие: не кладём трубку на живого секретаря ───────────────────────────

def test_soft_ivr_marker_with_human_answer_continues():
    """«Добрый день, наберите добавочный 105» — это живой секретарь."""
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("soft")
    r = _run(eng.process_turn("soft", "Добрый день, наберите добавочный 105"))
    assert r["node"] != "answering_machine", r
    assert r["phase"] == "secretary"


def test_human_signal_matched_by_word_not_substring():
    """«для отдела продаж» не должно считаться ответом «да»."""
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("sub")
    r = _run(eng.process_turn("sub", "Вы позвонили в компанию. Нажмите 1 для отдела продаж"))
    # Живым ответом «да» это не считается: меню — либо ожидание, либо машина.
    assert r["node"] in ("ivr_wait", "answering_machine"), r
    assert r["phase"] != "secretary", r


# ── Один и тот же вопрос не задаётся дважды подряд ─────────────────────────────

def test_repeated_question_is_paraphrased():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("rep")
    first = _run(eng.process_turn("rep", "Алло"))["robot_text"]
    second = _run(eng.process_turn("rep", "мгм непонятно"))["robot_text"]
    assert "кто у вас отвечает за электрохозяйство?" in first.lower()
    assert "кто у вас отвечает за электрохозяйство?" not in second.lower(), second


def test_scripted_transition_is_not_paraphrased():
    """Перевод на ЛПР не должен терять «меня направили к вам, всё верно?»."""
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("tr")
    _run(eng.process_turn("tr", "Алло"))
    r = _run(eng.process_turn("tr", "Соединяю"))
    assert r["robot_text"] == SCRIPT["lpr_greeting"], r["robot_text"]


# ── Итог звонка фиксируется движком ───────────────────────────────────────────

def test_outcome_contact_obtained():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("out1")
    _run(eng.process_turn("out1", "Алло"))
    _run(eng.process_turn("out1", "Телефон запишите"))
    _run(eng.process_turn("out1", "9141407100"))
    result = eng.get_outcome("out1")
    assert result["outcome"] == "contact_obtained", result
    assert result["data"].get("phone") == "9141407100", result


def test_outcome_ignores_our_own_number_echo():
    """Наш номер, «вернувшийся» эхом линии, не записывается как контакт клиента."""
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("out2")
    _run(eng.process_turn("out2", "Алло"))
    _run(eng.process_turn("out2", "8 800 775 96 31"))
    assert "phone" not in eng.get_outcome("out2")["data"]


def test_outcome_application_on_qualification_close():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("out3")
    st.phase = "qualification"
    st.qual_step = 5
    _run(eng.process_turn("out3", "Записывайте, 9141407100"))
    result = eng.get_outcome("out3")
    assert result["outcome"] == "application", result


# ── Скрипт возражений «у меня есть компания» (правки заказчика) ─────────────────

def test_lpr_responds_i_am_responsible_asks_name_and_topic():
    # «я отвечаю за электрохозяйство» → тема + «Как могу к вам обращаться?»,
    # БЕЗ второго «Добрый день» и без имени «Электрохозяйство».
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("iamr")
    st.phase = "secretary"
    st.last_robot_text = SCRIPT["greeting"]
    text, node = _run(eng._handle_secretary(st, "я отвечаю за электрохозяйство"))
    assert node == "i_am_lpr", node
    assert "Добрый день" not in text
    assert "Электрохозяйство" not in text  # не приняли предмет за имя
    assert "испытаниям электросетей" in text
    assert "как могу к вам обращаться" in text.lower()


def test_own_company_ladder_monitor_market_then_phone():
    # 10% ниже → отказ → «мониторите рынок» + сроки → «в следующем году» →
    # просим прямой номер и месяц → номер → закрытие.
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("ocl")
    st.phase = "lpr_main"
    st.lpr_topic_asked = True
    st.lpr_own_company_attempt = 1
    st.last_robot_text = SCRIPT["lpr_own_company_1"]

    text, node = _run(eng._handle_lpr_main(st, "нет, не надо"))
    assert node == "own_company_2", node
    assert text == SCRIPT["lpr_own_company_2"]
    assert "мониторить рынок" in text
    assert st.lpr_oc2_asked is True

    text, node = _run(eng._handle_lpr_main(st, "позвоните в следующем году"))
    assert node == "own_company_far", node
    assert text == SCRIPT["lpr_own_company_far"]
    assert st.lpr_far_date_pending is True

    text, node = _run(eng._handle_lpr_main(st, "89001234567"))
    assert node == "far_date_closed", node
    assert st.phase == "closed"


def test_own_company_ladder_near_term_goes_to_qual():
    # Если после «мониторите рынок» назвали близкий срок — оформляем заявку.
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("ocn")
    st.phase = "lpr_main"
    st.lpr_topic_asked = True
    st.lpr_oc2_asked = True
    text, node = _run(eng._handle_lpr_main(st, "в этом месяце планируем"))
    assert node == "oc2→qual0", node
    assert st.phase == "qualification"


# ── Робот не «додумывает» фразу «с инженером или энергетиком» ───────────────────

def test_no_invented_engineer_phrase_in_paraphrases():
    from app.services.script_dialogue_v2 import _PARAPHRASES
    for variants in _PARAPHRASES.values():
        for phrase in variants:
            assert "с инженером или энергетиком" not in phrase.lower(), phrase


# ── Отказ дать номер → спрашиваем, как ещё связаться (а не прощаемся сразу) ──────

def test_name_given_number_refused_asks_how_to_reach():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("reach")
    st.phase = "secretary"
    st.secretary_name_known = True
    st.secretary_name_pending_number = True
    st.last_robot_text = SCRIPT["secretary_gave_name"]
    text, node = _run(eng._handle_secretary(st, "нет"))
    assert node == "how_to_reach", node
    assert text == SCRIPT["secretary_how_to_reach"]
    assert "до свидания" not in text.lower()
    assert st.secretary_reach_asked is True
    # Любой следующий ответ — вежливо завершаем
    text2, node2 = _run(eng._handle_secretary(st, "через приёмную"))
    assert node2 == "reach_answer", node2
    assert text2 == SCRIPT["secretary_callback_thanks"]


# ── Ответственного нет на месте → не переспрашиваем «кто отвечает», берём имя/номер

def test_absent_responsible_followup_takes_name_not_reask():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("absent")
    st.phase = "secretary"
    st.secretary_absent_pending = True
    # Дали имя с отчеством → просим прямой номер, а не «кто отвечает»
    text, node = _run(eng._handle_secretary(st, "дорошенко елена александровна"))
    assert node == "gave_name", node
    assert text == SCRIPT["secretary_gave_name"]
    assert st.secretary_name_pending_number is True


def test_absent_flow_end_to_end_no_reask():
    # Полный путь через process_turn: «их нет, на объектах» → «когда будет, как
    # зовут, номер» → «раньше утром» → просим имя+номер, НЕ «кто отвечает».
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    eng.greeting("ae")
    _run(eng.process_turn("ae", "да слушаю"))  # запуск скрипта
    r1 = _run(eng.process_turn(
        "ae", "с инженером но их сейчас нет они на объектах"))
    assert r1["node"] in ("not_present", "secretary_not_present"), r1["node"]
    r2 = _run(eng.process_turn("ae", "раньше утром и"))
    assert "отвечает за электрохозяйство" not in r2["robot_text"].lower(), r2["robot_text"]
    assert r2["node"] == "absent_ask_name_phone", r2["node"]


def test_absent_responsible_vague_answer_asks_name_and_phone_once():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("absent2")
    st.phase = "secretary"
    st.secretary_absent_pending = True
    # «раньше утром» — это не имя: просим чётко имя и прямой номер (один раз)
    text, node = _run(eng._handle_secretary(st, "раньше утром и"))
    assert node == "absent_ask_name_phone", node
    assert "как зовут" in text.lower() and "номер" in text.lower()
    assert "отвечает за электрохозяйство" not in text.lower()
    # Ещё раз ничего конкретного → вежливо завершаем, а не крутимся
    text2, node2 = _run(eng._handle_secretary(st, "не знаю точно"))
    assert node2 == "absent_close", node2


# ── Номер, продиктованный прописью, и запись по частям ─────────────────────────

def test_spelled_number_detection():
    assert _contains_spelled_number("девятьсот пятнадцать")
    assert _contains_spelled_number("четыреста шестьдесят пять")
    assert _contains_spelled_number("восемьсот")           # сотенное слово
    assert not _contains_spelled_number("одну минуту")     # не номер
    assert not _contains_spelled_number("два вопроса")     # одно число — не номер
    assert _is_dictating_number("его номер девятьсот пятнадцать")
    assert _is_dictating_number("89001234567")
    assert not _is_dictating_number("нет")


def test_guard_rejects_our_number_when_dictating():
    # Собеседник диктует свой номер прописью — не диктуем свой в ответ
    assert _guard_contact_code("ask_our_number", "давайте его номер девятьсот пятнадцать") == "unknown"


def test_pending_number_spelled_records_not_dictates_ours():
    # Назвали имя, попросили номер; собеседник диктует прописью → записываем,
    # НЕ диктуем свой номер и НЕ переспрашиваем имя.
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("dict")
    st.phase = "secretary"
    st.secretary_name_known = True
    st.secretary_name_pending_number = True
    st.last_robot_text = SCRIPT["secretary_gave_name"]
    text, node = _run(eng._handle_secretary(st, "давайте его номер девятьсот пятнадцать"))
    assert node == "recording_number", node
    assert "восемьсот" not in text.lower()   # не диктуем наш номер
    assert st.secretary_collecting_number is True
    # Продолжение номера — продолжаем записывать
    text2, node2 = _run(eng._handle_secretary(st, "четыреста шестьдесят пять"))
    assert node2 == "recording_number", node2
    assert "восемьсот" not in text2.lower()
    # Диктовка закончилась (не-номер) → благодарим и завершаем
    text3, node3 = _run(eng._handle_secretary(st, "всё записали"))
    assert node3 == "gave_number", node3


def test_absent_inflected_patronymic_is_name_not_reask():
    # «от сергея евгеньевича…» (родительный падеж) — это имя, просим номер,
    # а не переспрашиваем «как зовут / кто отвечает».
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("infl")
    st.phase = "secretary"
    st.secretary_absent_pending = True
    text, node = _run(eng._handle_secretary(st, "от сергея евгеньевича давайте оставляйте свой контакт"))
    assert node == "gave_name", node
    assert st.secretary_name_pending_number is True


def test_absent_call_this_number_wraps_up_without_asking_number():
    # «Звоните на этот же номер» при отсутствии ЛПР → не диктуем пичт и не просим
    # прямой номер, а уточняем имя/когда застать и прощаемся.
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("callhere")
    st.phase = "secretary"
    st.secretary_absent_pending = True
    text, node = _run(eng._handle_secretary(st, "да вот поэтому можете звонить"))
    assert node == "same_number_wrapup", node
    assert text == SCRIPT["secretary_same_number_wrapup"]
    assert "ростехнадзор" not in text.lower()   # не вываливаем пичт
    assert "как зовут" in text.lower()
    # Следующий ответ (имя/когда) → вежливо завершаем
    text2, node2 = _run(eng._handle_secretary(st, "иванов, после обеда"))
    assert node2 == "absent_close", node2


# ── Вопрос текущей фазы (для переспроса при молчании) ──────────────────────────

def test_current_question_by_phase():
    eng = ScriptDialogueV2(_FakeGPT(), corrections=None)
    st = eng.create_session("cq")
    assert eng.current_question("cq") == SCRIPT["fallback_secretary"]
    st.phase = "lpr_main"
    assert eng.current_question("cq") == SCRIPT["fallback_lpr"]
    st.phase = "qualification"
    st.qual_step = 1
    assert eng.current_question("cq") == SCRIPT["qual_step1"]


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-o", "asyncio_mode=auto", "-v"]))
