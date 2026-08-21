#!/usr/bin/env python3
"""Тесты перебивания робота (barge-in) для алгоритма v2.

Проверяют:
  A. AudioPipeline — детекцию перебивания с антидребезгом (порог по длительности
     речи) и то, что во время речи робота реплика клиента не распознаётся.
  B. ConversationDriver — что фоновый TTS отменяется при перебивании, событие
     stop_audio отправляется клиенту, а часть аудио не доигрывается.

Запуск: python -m tests.test_barge_in
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _samples(value: int, n: int) -> bytes:
    """n сэмплов int16 (little-endian) с заданной амплитудой."""
    return int(value).to_bytes(2, "little", signed=True) * n


# 800 сэмплов = 1600 байт = 100 мс при 8 кГц
VOICED_100MS = _samples(3000, 800)   # громко (> порога тишины 500)
SILENCE_100MS = _samples(0, 800)


# ─────────────────────────── A. AudioPipeline ───────────────────────────

class _FakeASR:
    async def recognize_short(self, audio: bytes) -> str:
        return "распознанный текст"

    async def start_stream(self):
        return None   # потоковый режим в тестах выключен → REST-путь


async def test_pipeline_debounce_no_false_interrupt():
    from app.services.audio_pipeline import AudioPipeline
    p = AudioPipeline(asr_service=_FakeASR(), tts_service=None, interrupt_threshold_ms=200)
    p._is_speaking = True
    # Один короткий воицированный чанк (100 мс) < порога 200 мс → НЕ перебивание
    res = await p.process_chunk(VOICED_100MS)
    assert res is None, res
    # Тишина сбрасывает накопитель
    res = await p.process_chunk(SILENCE_100MS)
    assert res is None, res
    assert not p._interrupted
    print("   ✅ A1: короткий шум не прерывает робота")


async def test_pipeline_interrupt_after_threshold():
    from app.services.audio_pipeline import AudioPipeline
    p = AudioPipeline(asr_service=_FakeASR(), tts_service=None, interrupt_threshold_ms=200)
    p._is_speaking = True
    r1 = await p.process_chunk(VOICED_100MS)   # 100 мс — ещё нет
    assert r1 is None, r1
    r2 = await p.process_chunk(VOICED_100MS)   # 200 мс — срабатывает
    assert r2 and r2["type"] == "interrupt", r2
    assert p._interrupted
    print("   ✅ A2: непрерывная речь ≥ порога → перебивание")


async def test_pipeline_no_recognition_while_speaking():
    from app.services.audio_pipeline import AudioPipeline
    p = AudioPipeline(asr_service=_FakeASR(), tts_service=None, interrupt_threshold_ms=200)
    p.buffer.pause_duration = 0.05
    p._is_speaking = True
    await p.process_chunk(VOICED_100MS)
    await asyncio.sleep(0.06)
    res = await p.process_chunk(SILENCE_100MS)
    # Пока робот говорит, распознавания быть не должно
    assert res is None or res.get("type") != "recognition", res
    print("   ✅ A3: во время речи робота реплика не распознаётся")


async def test_pipeline_utterance_when_silent():
    """После паузы пайплайн отдаёт аудио реплики (распознаёт уже драйвер)."""
    from app.services.audio_pipeline import AudioPipeline
    p = AudioPipeline(asr_service=_FakeASR(), tts_service=None, interrupt_threshold_ms=200)
    p.buffer.end_pause_short_sec = 0.05
    p.buffer.end_pause_sec = 0.05
    p._is_speaking = False
    res = None
    # 200 мс речи + тишина дольше порога паузы
    await p.process_chunk(VOICED_100MS)
    await p.process_chunk(VOICED_100MS)
    for _ in range(3):
        res = await p.process_chunk(SILENCE_100MS)
        if res:
            break
    assert res and res["type"] == "utterance", res
    assert len(res["audio"]) > 1600, len(res["audio"])
    assert await p.recognize_utterance(res["audio"]) == "распознанный текст"
    print("   ✅ A4: после паузы реплика уходит на распознавание")


async def test_pipeline_does_not_buffer_own_echo():
    """Пока робот говорит, входящее аудио (наше эхо) НЕ копится в буфере.

    Раньше буфер очищался только на тихих чанках, поэтому эхо собственной речи
    доезжало до ASR и робот обрабатывал свою же реплику как слова собеседника.
    """
    from app.services.audio_pipeline import AudioPipeline
    p = AudioPipeline(asr_service=_FakeASR(), tts_service=None, interrupt_threshold_ms=10_000)
    p._is_speaking = True
    for _ in range(10):                        # 1 секунда «эха» поверх речи робота
        assert await p.process_chunk(VOICED_100MS) is None
    assert p.buffer.is_empty, p.buffer.duration_ms
    print("   ✅ A5: эхо робота не попадает в буфер распознавания")


async def test_pipeline_echo_guard_after_speech():
    """Первые echo_guard_ms после реплики робота отбрасываются (хвост эха)."""
    from app.services.audio_pipeline import AudioPipeline
    p = AudioPipeline(asr_service=_FakeASR(), tts_service=None, interrupt_threshold_ms=10_000)
    p._echo_guard_ms = 200
    p._is_speaking = True
    await p.process_chunk(VOICED_100MS)        # робот говорит → взводим хвост
    p._is_speaking = False
    await p.process_chunk(VOICED_100MS)        # 100 мс хвоста — выброшено
    await p.process_chunk(VOICED_100MS)        # ещё 100 мс — выброшено
    assert p.buffer.is_empty, p.buffer.duration_ms
    await p.process_chunk(VOICED_100MS)        # хвост закончился — копим
    assert not p.buffer.is_empty
    print("   ✅ A6: эхо-хвост после своей реплики отбрасывается")


async def test_pipeline_keeps_start_of_interrupting_phrase():
    """После barge-in в буфере остаётся начало реплики собеседника."""
    from app.services.audio_pipeline import AudioPipeline
    p = AudioPipeline(asr_service=_FakeASR(), tts_service=None, interrupt_threshold_ms=200)
    p._is_speaking = True
    assert await p.process_chunk(VOICED_100MS) is None
    res = await p.process_chunk(VOICED_100MS)
    assert res and res["type"] == "interrupt", res
    assert not p.buffer.is_empty, "начало перебивающей реплики потеряно"
    print("   ✅ A7: начало перебивающей реплики сохранено")


async def test_pipeline_quiet_echo_does_not_interrupt():
    """Тихое эхо (громче тишины, но тише живого голоса) не прерывает робота."""
    from app.services.audio_pipeline import AudioPipeline
    p = AudioPipeline(asr_service=_FakeASR(), tts_service=None, interrupt_threshold_ms=200)
    p._is_speaking = True
    quiet = _samples(700, 800)        # > порога тишины 500, но < 500 * gain(2.0)
    for _ in range(10):
        assert await p.process_chunk(quiet) is None
    print("   ✅ A8: тихое эхо не считается перебиванием")


async def test_pipeline_pause_detected_in_backlog():
    """Пауза детектится и когда кадры разбираются «пачкой» из очереди.

    Телефония копит кадры, пока считается предыдущий ход, и отдаёт их подряд за
    миллисекунды. При расчёте паузы по стенным часам пауза в такой пачке не
    находилась вовсе — реплики склеивались в одну.
    """
    from app.services.audio_pipeline import AudioPipeline
    p = AudioPipeline(asr_service=_FakeASR(), tts_service=None)
    p._is_speaking = False
    res = None
    # 10 чанков речи + 15 чанков тишины, поданных подряд без задержек
    for _ in range(10):
        await p.process_chunk(VOICED_100MS)
    for _ in range(15):
        res = await p.process_chunk(SILENCE_100MS)
        if res:
            break
    assert res and res["type"] == "utterance", res
    print("   ✅ A9: пауза находится и при разборе накопившейся очереди")


# ─────────────────────── B. ConversationDriver ───────────────────────

def _install_fake_registry():
    """Подменяет app.services.registry лёгкими заглушками (без сети/ML)."""
    import app.services  # noqa: F401 — гарантируем, что пакет импортирован

    fake = types.ModuleType("app.services.registry")

    class _Obj:
        pass

    fake.asr_service = _Obj()
    fake.tts_service = _Obj()

    async def _synth_stream(text, voice=None, role=None, speed=None):
        # Долгий стрим: 20 чанков по ~20 мс — легко отменить на середине
        for _ in range(20):
            await asyncio.sleep(0.02)
            yield b"\x00\x00" * 80

    fake.tts_service.synthesize_stream = _synth_stream
    fake.salutespeech_tts_service = _Obj()

    class _CM:
        async def add_to_transcript(self, *a, **k):
            return None

        async def get_call(self, call_id):
            return None

        async def end_call(self, *a, **k):
            return None

    fake.call_manager = _CM()

    sys.modules["app.services.registry"] = fake
    setattr(sys.modules["app.services"], "registry", fake)


async def test_driver_barge_in_cancels_tts():
    _install_fake_registry()
    from app.services.conversation import ConversationDriver

    sent_audio = []
    events = []

    async def send_audio(chunk):
        sent_audio.append(chunk)

    async def send_event(ev):
        events.append(ev)

    driver = ConversationDriver(
        call_id="test", session=types.SimpleNamespace(algo_version="v2"),
        scenario=types.SimpleNamespace(steps={}),
        send_audio=send_audio, send_event=send_event,
    )

    driver.start_tts("длинная реплика робота, которую мы перебьём на середине")
    assert driver._tts_task is not None
    await asyncio.sleep(0.05)                 # дать проиграть пару чанков
    assert driver.pipeline._is_speaking is True

    await driver.interrupt()                  # barge-in

    assert driver._tts_task is None
    assert driver.pipeline._is_speaking is False
    assert 0 < len(sent_audio) < 20, len(sent_audio)   # прервано на середине
    assert any(e.get("type") == "stop_audio" for e in events), events
    print(f"   ✅ B1: TTS отменён на {len(sent_audio)}/20 чанках, отправлен stop_audio")


async def test_driver_flushes_input_after_speech():
    """После своей реплики драйвер чистит вход транспорта и взводит эхо-хвост."""
    _install_fake_registry()
    from app.services.conversation import ConversationDriver

    flushed = []

    async def send_audio(chunk):
        return None

    driver = ConversationDriver(
        call_id="test-flush", session=types.SimpleNamespace(algo_version="v2"),
        scenario=types.SimpleNamespace(steps={}),
        send_audio=send_audio, flush_input=lambda: flushed.append(1),
    )
    await driver.stream_tts("короткая реплика")
    assert flushed, "вход транспорта не очищен после речи робота"
    assert driver.pipeline._echo_guard_left > 0, "эхо-хвост не взведён"
    print("   ✅ B2: после речи робота вход очищен, эхо-хвост взведён")


async def test_driver_does_not_block_on_recognition():
    """Приём аудио не простаивает, пока считается предыдущий ход.

    Раньше feed_chunk ждал ASR + классификацию (2–5 с): поток чтения кадров
    телефонии блокировался, речь собеседника терялась в очереди транспорта.
    """
    _install_fake_registry()
    from app.services.conversation import ConversationDriver

    started = asyncio.Event()

    class _SlowASR:
        async def recognize_short(self, audio: bytes) -> str:
            started.set()
            await asyncio.sleep(0.5)
            return "медленно распознанный текст"

        async def start_stream(self):
            return None

    async def send_audio(chunk):
        return None

    driver = ConversationDriver(
        call_id="test-nonblock", session=types.SimpleNamespace(algo_version="v2"),
        scenario=types.SimpleNamespace(steps={}),
        send_audio=send_audio,
    )
    driver.pipeline.asr = _SlowASR()
    driver.pipeline.buffer.end_pause_sec = 0.05
    driver.pipeline.buffer.end_pause_short_sec = 0.05

    t0 = asyncio.get_event_loop().time()
    for _ in range(4):
        await driver.feed_chunk(VOICED_100MS)
    for _ in range(3):
        await driver.feed_chunk(SILENCE_100MS)
    elapsed = asyncio.get_event_loop().time() - t0

    assert elapsed < 0.2, f"feed_chunk блокируется на {elapsed:.2f}s"
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert driver._turn_task is not None and not driver._turn_task.done()
    driver._turn_task.cancel()
    print(f"   ✅ B3: приём аудио не блокируется распознаванием ({elapsed*1000:.0f} мс)")


async def main():
    print("\n🤖 Barge-in (перебивание робота) — тесты v2\n")
    print("A. AudioPipeline (детекция перебивания):")
    await test_pipeline_debounce_no_false_interrupt()
    await test_pipeline_interrupt_after_threshold()
    await test_pipeline_no_recognition_while_speaking()
    await test_pipeline_utterance_when_silent()
    await test_pipeline_does_not_buffer_own_echo()
    await test_pipeline_echo_guard_after_speech()
    await test_pipeline_keeps_start_of_interrupting_phrase()
    await test_pipeline_quiet_echo_does_not_interrupt()
    await test_pipeline_pause_detected_in_backlog()
    print("\nB. ConversationDriver (отмена речи):")
    await test_driver_barge_in_cancels_tts()
    await test_driver_flushes_input_after_speech()
    await test_driver_does_not_block_on_recognition()
    print("\n✅ Все тесты barge-in пройдены\n")


if __name__ == "__main__":
    asyncio.run(main())
