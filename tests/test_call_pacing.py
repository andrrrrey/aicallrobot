#!/usr/bin/env python3
"""Тесты темпа диалога: робот не «строчит» репликами и даёт человеку ответить.

Проверяют исправления по разбору реальных звонков, где робот выдавал подряд
3-4 реплики, не дождавшись ни одного ответа собеседника:

  A. Сторож тишины (ConversationDriver)
     — не считает молчанием время, пока звучит его же реплика (очередь
       воспроизведения не пуста);
     — не заговаривает, пока собеседник говорит;
     — первый переспрос — короткий оклик, а не новый скриптовый вопрос;
     — второй — повтор УЖЕ заданного вопроса, а не следующего по сценарию.
  B. Движок v2 — pending_question и защита от ложной диктовки нашего номера.
  C. Слой правок — лексическая страховка от ложных семантических совпадений.

Запуск: python -m tests.test_call_pacing
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _samples(value: int, n: int) -> bytes:
    return int(value).to_bytes(2, "little", signed=True) * n


VOICED_100MS = _samples(3000, 800)
SILENCE_100MS = _samples(0, 800)


# ─────────────────────────── Заглушки окружения ───────────────────────────

class _Spoken(list):
    """Реплики робота, попавшие в транскрипт."""


# Один-единственный поддельный реестр на весь прогон: conversation.py делает
# ``from app.services import registry`` на импорте, поэтому подменять модуль
# повторно бесполезно — драйвер продолжит держать ссылку на первый объект.
_FAKE_REGISTRY = None
_CURRENT = {"spoken": _Spoken(), "pending_question": ""}


def _install_fake_registry(spoken: _Spoken, pending_question: str = ""):
    global _FAKE_REGISTRY
    _CURRENT["spoken"] = spoken
    _CURRENT["pending_question"] = pending_question
    if _FAKE_REGISTRY is not None:
        return _FAKE_REGISTRY

    import app.services  # noqa: F401

    fake = types.ModuleType("app.services.registry")

    class _Obj:
        pass

    fake.asr_service = _Obj()
    fake.tts_service = _Obj()

    async def _synth_stream(text, voice=None, role=None, speed=None):
        yield b"\x00\x00" * 80

    fake.tts_service.synthesize_stream = _synth_stream
    fake.salutespeech_tts_service = _Obj()

    class _CM:
        async def add_to_transcript(self, call_id, role, text):
            if role == "robot":
                _CURRENT["spoken"].append(text)

        async def get_call(self, call_id):
            return None

        async def end_call(self, *a, **k):
            return None

    fake.call_manager = _CM()

    class _V2:
        def pending_question(self, call_id, state=None):
            return _CURRENT["pending_question"]

        def current_question(self, call_id, state=None):
            return "СЛЕДУЮЩИЙ вопрос сценария, которого тут быть не должно?"

    fake.script_v2_engine = _V2()

    sys.modules["app.services.registry"] = fake
    setattr(sys.modules["app.services"], "registry", fake)
    _FAKE_REGISTRY = fake
    return fake


def _patch_timeouts(module, first: float, repeat: float):
    """Подменяет get_settings в модуле разговора короткими таймаутами."""
    from app.core.config import get_settings

    real = get_settings()
    stub = types.SimpleNamespace(
        no_input_timeout_sec=first,
        no_input_repeat_timeout_sec=repeat,
        vad_end_pause_sec=real.vad_end_pause_sec,
    )
    module.get_settings = lambda: stub


def _make_driver(spoken: _Spoken, audio_pending, pending_question: str = ""):
    _install_fake_registry(spoken, pending_question)
    from app.services import conversation as conv_module
    from app.services.conversation import ConversationDriver

    _patch_timeouts(conv_module, first=0.6, repeat=0.6)

    async def send_audio(chunk):
        return None

    driver = ConversationDriver(
        call_id="pace", session=types.SimpleNamespace(algo_version="v2"),
        scenario=types.SimpleNamespace(steps={}),
        send_audio=send_audio, audio_pending=audio_pending,
    )
    return driver


# ─────────────────────── A. Сторож тишины ───────────────────────

async def test_no_prompt_while_robot_audio_still_playing():
    """Пока не проигран исходящий звук — робот молчит, а не переспрашивает.

    Главный баг «пулемёта»: генерация TTS 15-секундной фразы занимает пару
    секунд, после чего сторож считал робота молчащим и через 8 с задавал новый
    вопрос ПОВЕРХ ещё звучащей реплики.
    """
    spoken = _Spoken()
    still_playing = {"v": True}
    driver = _make_driver(spoken, audio_pending=lambda: still_playing["v"])

    driver.start_watchdog()
    await asyncio.sleep(2.0)          # заметно дольше таймаута (0.6 с)
    assert not spoken, f"робот заговорил поверх своей же реплики: {spoken}"

    still_playing["v"] = False        # реплика доиграла
    await asyncio.sleep(1.6)
    assert spoken, "после окончания реплики переспрос так и не прозвучал"
    driver.should_end = True
    driver._watchdog_task.cancel()
    print("   ✅ A1: пока звучит реплика робота, сторож молчит")


async def test_first_prompt_is_short_nudge():
    """Первый переспрос — короткий оклик, а не новый скриптовый вопрос."""
    spoken = _Spoken()
    driver = _make_driver(
        spoken, audio_pending=lambda: False,
        pending_question="Кто у вас отвечает за электрохозяйство?",
    )
    from app.services.conversation import _SILENCE_NUDGE

    driver.start_watchdog()
    await asyncio.sleep(1.6)
    assert spoken[:1] == [_SILENCE_NUDGE], spoken
    driver.should_end = True
    driver._watchdog_task.cancel()
    print("   ✅ A2: первый переспрос — короткий оклик")


async def test_ladder_repeats_asked_question_then_closes():
    """Шаг 2 — повтор УЖЕ заданного вопроса, шаг 3 — вежливое завершение."""
    spoken = _Spoken()
    question = "Кто у вас отвечает за электрохозяйство?"
    driver = _make_driver(spoken, audio_pending=lambda: False, pending_question=question)
    from app.services.conversation import _SILENCE_NUDGE, _SILENCE_GIVE_UP

    driver.start_watchdog()
    for _ in range(40):
        if driver.should_end:
            break
        await asyncio.sleep(0.1)
    assert spoken == [_SILENCE_NUDGE, question, _SILENCE_GIVE_UP], spoken
    assert driver.should_end, "звонок не завершён после лестницы молчания"
    print("   ✅ A3: лестница = оклик → повтор вопроса → завершение")


async def test_no_prompt_while_client_is_speaking():
    """Пока в буфере копится реплика собеседника — робот не перебивает."""
    spoken = _Spoken()
    driver = _make_driver(spoken, audio_pending=lambda: False)

    driver.start_watchdog()
    for _ in range(20):               # 2 с непрерывной речи собеседника
        await driver.feed_chunk(VOICED_100MS)
        await asyncio.sleep(0.1)
    assert not spoken, f"робот перебил говорящего собеседника: {spoken}"
    driver.should_end = True
    driver._watchdog_task.cancel()
    print("   ✅ A4: сторож не перебивает говорящего собеседника")


async def test_quiet_line_does_not_block_watchdog():
    """Тишина на линии не считается «собеседник говорит».

    Буфер VAD копит и тихие кадры, поэтому проверять «идёт ли реплика» по
    непустому буферу нельзя: на молчащей линии сторож не сработал бы никогда,
    и звонок висел бы до аварийного лимита длительности.
    """
    spoken = _Spoken()
    driver = _make_driver(spoken, audio_pending=lambda: False)
    from app.services.conversation import _SILENCE_NUDGE

    driver.start_watchdog()
    for _ in range(16):               # 1.6 с тишины на линии
        await driver.feed_chunk(SILENCE_100MS)
        await asyncio.sleep(0.1)
    assert spoken[:1] == [_SILENCE_NUDGE], spoken
    driver.should_end = True
    driver._watchdog_task.cancel()
    print("   ✅ A5: тишина на линии не блокирует сторожа")


# ─────────────────────── B. Движок v2 ───────────────────────

def test_pending_question_repeats_only_the_question():
    from app.services.script_dialogue_v2 import ScriptDialogueV2

    eng = ScriptDialogueV2(gpt_service=None)
    state = eng.create_session("s1")
    state.last_robot_text = (
        "Звоню по обязательным проверкам электросетей. "
        "Кто у вас отвечает за электрохозяйство? Инженер или энергетик?"
    )
    assert eng.pending_question("s1") == "Кто у вас отвечает за электрохозяйство?"

    # Реплика без вопроса — повторять нечего
    state.last_robot_text = "Записываю."
    assert eng.pending_question("s1") == ""
    print("   ✅ B1: pending_question повторяет ровно заданный вопрос")


def test_our_number_not_dictated_without_request():
    from app.services.script_dialogue_v2 import _guard_contact_code

    # Реального запроса номера нет — код отклоняем
    assert _guard_contact_code(
        "ask_our_number", "здравствуйте вы позвонили в колледж сферы услуг номер три",
    ) == "unknown"
    assert _guard_contact_code("ask_our_number", "а кто это звонит") == "unknown"
    # Явная просьба — код проходит
    for phrase in (
        "продиктуйте ваш номер",
        "оставьте свой телефон, я передам",
        "как с вами связаться?",
    ):
        assert _guard_contact_code("ask_our_number", phrase) == "ask_our_number", phrase
    # Прочие коды не трогаем
    assert _guard_contact_code("transfer_to_lpr", "соединяю") == "transfer_to_lpr"
    print("   ✅ B2: наш номер диктуется только когда его действительно просят")


async def test_same_answer_three_times_closes_call():
    """Третий подряд «никто» закрывает разговор, а не запускает новый круг.

    В реальном звонке собеседник четыре раза ответил «никто», а робот столько же
    раз переформулировал один и тот же вопрос.
    """
    from app.services.script_dialogue_v2 import ScriptDialogueV2

    eng = ScriptDialogueV2(gpt_service=None)
    state = eng.create_session("s3")
    state.phase = "secretary"

    nodes = []
    for _ in range(3):
        result = await eng.process_turn("s3", "никто")
        nodes.append(result["node"])
    assert nodes[-1] == "stonewall_close", nodes
    assert eng.get_session("s3").phase == "closed"
    assert eng.get_outcome("s3")["outcome"] == "refused"
    print("   ✅ B4: третий одинаковый ответ завершает разговор")


async def test_absent_responsible_asks_when_and_number():
    """«Директор, но его нет» → узнаём имя/время/номер, а не «соедините с ним».

    В реальном звонке робот трижды просил соединить с директором, которого
    секретарь трижды назвал отсутствующим.
    """
    from app.services.script_dialogue_v2 import ScriptDialogueV2
    from app.services.script_v2_data import SCRIPT

    eng = ScriptDialogueV2(gpt_service=None)
    st = eng.create_session("s4")
    st.phase = "secretary"
    st.last_robot_text = SCRIPT["greeting"]

    # Должность названа вместе с «его нет» — сразу спрашиваем, когда будет
    text, node = await eng._handle_secretary(st, "директор, его нет")
    assert node == "not_present", node
    assert text == SCRIPT["secretary_not_present"], text

    # Повторное «его в данный момент нету» — тоже не «соедините меня с ним»
    st.last_robot_text = SCRIPT["secretary_connect_responsible"]
    text, node = await eng._handle_secretary(st, "его в данный момент нету")
    assert node == "not_present", node
    assert "соедините" not in text.lower(), text
    print("   ✅ B5: отсутствующего ответственного не просим позвать к телефону")


async def test_no_role_list_when_responsible_already_named():
    """Раз должность уже назвали — не зачитываем список «директор, инженер…»."""
    from app.services.script_dialogue_v2 import ScriptDialogueV2
    from app.services.script_v2_data import SCRIPT

    eng = ScriptDialogueV2(gpt_service=None)
    st = eng.create_session("s5")
    st.phase = "secretary"
    st.secretary_name_known = True
    st.last_robot_text = SCRIPT["secretary_connect_responsible"]
    text, node = eng._secretary_code_to_response(st, "no_engineer")
    assert text != SCRIPT["secretary_no_engineer"], text
    assert node == "not_present", node
    print("   ✅ B6: список должностей не повторяется после названной должности")


async def test_pickup_after_ivr_reintroduces():
    """Взявшему трубку человеку робот представляется заново, а не с середины."""
    from app.services.script_dialogue_v2 import ScriptDialogueV2
    from app.services.script_v2_data import SCRIPT

    eng = ScriptDialogueV2(gpt_service=None)
    st = eng.create_session("s6")
    st.phase = "secretary"
    st.last_robot_text = SCRIPT["greeting"]
    for phrase in ("торговый центр леруа здравствуйте", "алло"):
        text, node = await eng._handle_secretary(st, phrase)
        assert node == "reintroduce", (phrase, node)
        assert text == SCRIPT["greeting"], text
    # Третий раз представляться не будем — переходим к сути
    text, node = await eng._handle_secretary(st, "алло")
    assert node == "pickup_no_transfer", node
    print("   ✅ B7: поднявшему трубку робот представляется заново")


async def test_corrections_skipped_during_handshake():
    """Правка не может сработать на рукопожатии (до начала скрипта)."""
    from app.services.script_dialogue_v2 import ScriptDialogueV2

    class _AlwaysMatches:
        async def match(self, user_text, phase):
            return "Запишите наш номер: восемь — восемьсот — семь…"

    eng = ScriptDialogueV2(gpt_service=None, corrections=_AlwaysMatches())
    eng.greeting("s2")
    result = await eng.process_turn("s2", "здравствуйте, вы позвонили в колледж")
    assert result["node"] != "correction", result
    assert "восемьсот" not in result["robot_text"], result["robot_text"]
    print("   ✅ B3: правки не применяются в фазе рукопожатия")


# ─────────────────────── C. Слой правок ───────────────────────

def test_correction_lexical_guard():
    from app.services.script_corrections import _lexically_related

    # Совсем разные фразы — правка не применяется
    assert not _lexically_related(
        "запишите телефон ответственного",
        "здравствуйте, вы позвонили в колледж сферы услуг",
    )
    # Настоящая перефразировка — общее значимое слово есть
    assert _lexically_related(
        "у нас нет ответственного за электрохозяйство",
        "ответственного за электрохозяйство у нас нету",
    )
    print("   ✅ C1: правка не срабатывает на фразе без общих слов")


async def main():
    print("\n🤖 Темп диалога — тесты v2\n")
    print("A. Сторож тишины:")
    await test_no_prompt_while_robot_audio_still_playing()
    await test_first_prompt_is_short_nudge()
    await test_ladder_repeats_asked_question_then_closes()
    await test_no_prompt_while_client_is_speaking()
    await test_quiet_line_does_not_block_watchdog()
    print("\nB. Движок v2:")
    test_pending_question_repeats_only_the_question()
    test_our_number_not_dictated_without_request()
    await test_same_answer_three_times_closes_call()
    await test_absent_responsible_asks_when_and_number()
    await test_no_role_list_when_responsible_already_named()
    await test_pickup_after_ivr_reintroduces()
    await test_corrections_skipped_during_handshake()
    print("\nC. Слой правок:")
    test_correction_lexical_guard()
    print("\n✅ Все тесты темпа диалога пройдены\n")


if __name__ == "__main__":
    asyncio.run(main())
