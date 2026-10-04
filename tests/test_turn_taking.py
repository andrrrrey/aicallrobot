#!/usr/bin/env python3
"""Разговор «в параллель»: робот даёт договорить и отвечает на последнее сказанное.

  * собеседник сделал паузу посреди фразы и продолжил — робот не отвечает на
    обрывок, а склеивает его с продолжением и отвечает на всю фразу;
  * пока робот считал ответ, собеседник снова заговорил — ответ не звучит
    поверх него; если собеседник сказал что-то новое, старый ответ
    отбрасывается, если это была помеха — произносится, когда стало тихо.

Запуск: python -m pytest tests/test_turn_taking.py -o asyncio_mode=auto
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_call_pacing import _Spoken, _make_driver  # noqa: E402


def _driver_with_asr(texts: list[str]):
    """Драйвер, у которого ASR по очереди «распознаёт» тексты из списка."""
    spoken = _Spoken()
    driver = _make_driver(spoken, audio_pending=lambda: False)
    queue = list(texts)

    async def _recognize(audio, session=None):
        return queue.pop(0) if queue else ""

    driver.pipeline.recognize_utterance = _recognize
    handled: list[str] = []

    async def _handle(text):
        handled.append(text)

    driver.handle_recognition = _handle
    return driver, spoken, handled


def test_phrase_continuation_is_glued():
    async def run():
        driver, _, handled = _driver_with_asr(["главный инженер", "но его сейчас нет"])
        # Пока распознавали первый кусок, собеседник уже говорит дальше
        driver.pipeline.buffer._speech_started = True
        await driver._process_utterance(b"\x00" * 3200)
        assert handled == [], "на обрывок фразы не отвечаем"
        driver.pipeline.buffer._speech_started = False
        await driver._process_utterance(b"\x00" * 3200)
        assert handled == ["главный инженер но его сейчас нет"], handled
    asyncio.run(run())


def test_carry_answered_after_silence():
    async def run():
        driver, _, handled = _driver_with_asr(["главный инженер"])
        driver.pipeline.buffer._speech_started = True
        await driver._process_utterance(b"\x00" * 3200)
        driver.pipeline.buffer._speech_started = False
        # Продолжения так и не было (помеха) — отвечаем на сказанное
        await driver._flush_waiting_turn()
        assert handled == ["главный инженер"], handled
    asyncio.run(run())


def test_held_reply_dropped_when_client_says_more():
    async def run():
        driver, spoken, handled = _driver_with_asr(["его нет на месте"])
        driver._held_reply = "Отлично, соедините меня с ним, пожалуйста."
        await driver._process_utterance(b"\x00" * 3200)
        assert driver._held_reply == ""
        assert handled == ["его нет на месте"]
        assert "Отлично, соедините меня с ним, пожалуйста." not in spoken
    asyncio.run(run())


def test_held_reply_spoken_when_only_noise():
    async def run():
        driver, spoken, handled = _driver_with_asr([""])
        driver._held_reply = "Подскажите, как зовут ответственного?"
        await driver._process_utterance(b"\x00" * 3200)
        assert handled == []
        assert spoken[-1] == "Подскажите, как зовут ответственного?"
        if driver._tts_task:
            driver._tts_task.cancel()
    asyncio.run(run())


def test_watchdog_flushes_waiting_turn_on_quiet_line():
    async def run():
        driver, _, handled = _driver_with_asr([])
        driver._carry_text = "главный инженер"
        driver.start_watchdog()
        await asyncio.sleep(1.8)
        driver.should_end = True
        assert handled == ["главный инженер"], handled
    asyncio.run(run())
