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


async def test_pipeline_recognition_when_silent():
    from app.services.audio_pipeline import AudioPipeline
    p = AudioPipeline(asr_service=_FakeASR(), tts_service=None, interrupt_threshold_ms=200)
    p.buffer.pause_duration = 0.05
    p._is_speaking = False
    await p.process_chunk(VOICED_100MS)
    await asyncio.sleep(0.06)
    res = await p.process_chunk(SILENCE_100MS)   # пауза → конец реплики
    assert res and res["type"] == "recognition", res
    assert res["text"] == "распознанный текст", res
    print("   ✅ A4: после паузы реплика распознаётся")


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


async def main():
    print("\n🤖 Barge-in (перебивание робота) — тесты v2\n")
    print("A. AudioPipeline (детекция перебивания):")
    await test_pipeline_debounce_no_false_interrupt()
    await test_pipeline_interrupt_after_threshold()
    await test_pipeline_no_recognition_while_speaking()
    await test_pipeline_recognition_when_silent()
    print("\nB. ConversationDriver (отмена речи):")
    await test_driver_barge_in_cancels_tts()
    print("\n✅ Все тесты barge-in пройдены\n")


if __name__ == "__main__":
    asyncio.run(main())
