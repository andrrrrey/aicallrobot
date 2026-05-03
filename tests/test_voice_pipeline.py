#!/usr/bin/env python3
"""
Тестирование голосового контура: TTS → ASR → TTS.
Запуск: python -m tests.test_voice_pipeline
"""

import asyncio
import httpx
import base64
import sys
import os

# Для запуска из корня проекта
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = os.getenv("API_URL", "http://localhost:8000")


async def test_health():
    """Проверка здоровья сервиса."""
    print("=" * 60)
    print("1. Проверка здоровья сервиса")
    print("=" * 60)

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_URL}/health")
        print(f"   Status: {r.status_code}")
        print(f"   Response: {r.json()}")
        assert r.status_code == 200
        print("   ✅ OK\n")


async def test_tts():
    """Тест синтеза речи."""
    print("=" * 60)
    print("2. Тест TTS (синтез речи)")
    print("=" * 60)

    test_phrases = [
        "Здравствуйте! Удобно ли вам сейчас говорить?",
        "Спасибо за уделённое время. Всего доброго!",
        "Понимаю, что вы заняты. Когда вам будет удобно перезвонить?",
    ]

    async with httpx.AsyncClient(timeout=30.0) as client:
        for i, phrase in enumerate(test_phrases, 1):
            print(f"\n   Фраза {i}: '{phrase}'")
            r = await client.post(
                f"{BASE_URL}/api/v1/tts",
                json={"text": phrase},
            )
            if r.status_code == 200:
                data = r.json()
                print(f"   Audio size: {data['size_bytes']} bytes")
                print(f"   Format: {data['format']}, Sample rate: {data['sample_rate']}")

                # Сохраняем первый результат для теста ASR
                if i == 1:
                    audio_b64 = data["audio_base64"]
                    # Сохраняем в файл
                    audio_bytes = base64.b64decode(audio_b64)
                    os.makedirs("test_output", exist_ok=True)
                    with open("test_output/tts_test.raw", "wb") as f:
                        f.write(audio_bytes)
                    print(f"   Saved to test_output/tts_test.raw")
                print(f"   ✅ OK")
            else:
                print(f"   ❌ Error: {r.status_code} — {r.text}")
                return None

    return audio_b64


async def test_asr(audio_b64: str):
    """Тест распознавания речи."""
    print("\n" + "=" * 60)
    print("3. Тест ASR (распознавание речи)")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{BASE_URL}/api/v1/asr",
            json={"audio_base64": audio_b64, "format": "lpcm"},
        )
        if r.status_code == 200:
            data = r.json()
            print(f"   Распознанный текст: '{data['text']}'")
            print(f"   Audio size: {data['audio_size']} bytes")
            print(f"   ✅ OK")
        else:
            print(f"   ❌ Error: {r.status_code} — {r.text}")


async def test_scenarios():
    """Тест сценариев."""
    print("\n" + "=" * 60)
    print("4. Тест сценариев")
    print("=" * 60)

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_URL}/api/v1/scenarios")
        if r.status_code == 200:
            data = r.json()
            for s in data["scenarios"]:
                print(f"   Сценарий: {s['id']} — {s['name']} ({s['steps']} шагов)")
            print(f"   ✅ OK")
        else:
            print(f"   ❌ Error: {r.status_code}")


async def test_call_lifecycle():
    """Тест жизненного цикла звонка."""
    print("\n" + "=" * 60)
    print("5. Тест жизненного цикла звонка")
    print("=" * 60)

    async with httpx.AsyncClient() as client:
        # Start call
        r = await client.post(
            f"{BASE_URL}/api/v1/calls/start",
            json={"phone_number": "+70001234567", "scenario_id": "default"},
        )
        if r.status_code != 200:
            print(f"   ❌ Start failed: {r.status_code}")
            return

        call_data = r.json()
        call_id = call_data["call_id"]
        print(f"   Call started: {call_id}")
        print(f"   Greeting: {call_data['greeting']}")

        # Get call status
        r = await client.get(f"{BASE_URL}/api/v1/calls/{call_id}")
        print(f"   Status: {r.json()['status']}")

        # List active
        r = await client.get(f"{BASE_URL}/api/v1/calls")
        print(f"   Active calls: {len(r.json()['calls'])}")

        # End call
        r = await client.post(f"{BASE_URL}/api/v1/calls/{call_id}/end")
        print(f"   Call ended: {r.json()['status']}")

        # Stats
        r = await client.get(f"{BASE_URL}/api/v1/stats")
        print(f"   Stats: {r.json()}")
        print(f"   ✅ OK")


async def main():
    print("\n🤖 AI Robot — Тестирование голосового контура\n")

    await test_health()
    audio = await test_tts()
    if audio:
        await test_asr(audio)
    await test_scenarios()
    await test_call_lifecycle()

    print("\n" + "=" * 60)
    print("✅ Все тесты завершены!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
