#!/usr/bin/env python3
"""
Тесты слоя правок скрипта (script corrections): парсинг таблиц, CRUD, matcher.
Запуск: python -m tests.test_script_corrections

Семантическое сопоставление требует chromadb. Если он не установлен — эти
проверки пропускаются (CRUD и парсинг проверяются всегда).
"""

import asyncio
import io
import os
import sys
import tempfile

# Для запуска из корня проекта
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Изолируем хранилище правок и индекс на временные пути ДО импорта сервиса
os.environ["script_corrections_file"] = tempfile.mktemp(suffix=".json")
os.environ["knowledge_base_dir"] = tempfile.mkdtemp()

from app.services.script_corrections import (  # noqa: E402
    parse_correction_table,
    ScriptCorrectionsService,
)


def test_parse_csv():
    print("=" * 60)
    print("1. Парсинг CSV (точка с запятой + шапка + пустые строки)")
    print("=" * 60)
    csv_text = (
        "Пример фразы;Что отвечает сейчас;Правильный ответ;Фаза\n"
        "Зачем вам директор;Хм;Мы по испытаниям электросетей;secretary\n"
        ";;;\n"
        "Нам не нужно;Жаль;Это бесплатная проверка;\n"
    )
    rows = parse_correction_table(csv_text.encode("utf-8"), "test.csv")
    assert len(rows) == 2, rows
    assert rows[0]["phase"] == "secretary"
    assert rows[1]["phase"] == "any"  # пустая фаза нормализуется в any
    # CSV с запятой без шапки
    rows2 = parse_correction_table("привет,текущий,правильный ответ\n".encode(), "b.csv")
    assert len(rows2) == 1 and rows2[0]["trigger"] == "привет"
    print("   ✅ OK\n")


def test_parse_xlsx():
    print("=" * 60)
    print("2. Парсинг XLSX")
    print("=" * 60)
    try:
        import openpyxl
    except ImportError:
        print("   ⏭  openpyxl не установлен — пропуск\n")
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Пример", "Сейчас", "Правильный", "Фаза"])
    ws.append(["алло кто это", "...", "Это компания РЭС", "lpr_greeting"])
    ws.append([None, None, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    rows = parse_correction_table(buf.getvalue(), "x.xlsx")
    assert len(rows) == 1, rows
    assert rows[0]["phase"] == "lpr_greeting"
    print("   ✅ OK\n")


def test_crud_and_export():
    print("=" * 60)
    print("3. CRUD + экспорт")
    print("=" * 60)
    svc = ScriptCorrectionsService()
    item = svc.add({"trigger": "тест", "correct_answer": "ответ"})
    assert item["id"] and svc.list()
    svc.update(item["id"], {"trigger": "тест2", "correct_answer": "ответ2"})
    assert svc.list()[0]["trigger"] == "тест2"
    n = svc.import_rows(
        [{"trigger": "a", "correct_answer": "b"}, {"trigger": "c", "correct_answer": "d"}],
        mode="replace",
    )
    assert n == 2 and len(svc.list()) == 2
    assert len(svc.export_rows("csv")[0]) > 0
    try:
        import openpyxl  # noqa: F401
        assert len(svc.export_rows("xlsx")[0]) > 0
    except ImportError:
        pass
    assert svc.delete(svc.list()[0]["id"]) is True
    print("   ✅ OK\n")


def test_match():
    print("=" * 60)
    print("4. Семантическое сопоставление (matcher)")
    print("=" * 60)
    svc = ScriptCorrectionsService()
    if not svc.index_available:
        print("   ⏭  chromadb не установлен — пропуск\n")
        return
    svc.import_rows(
        [
            {
                "trigger": "Зачем вам наш директор?",
                "correct_answer": "Мы проводим испытания электросетей, это к нему.",
                "phase": "secretary",
            },
            {
                "trigger": "Нам ваши услуги не нужны",
                "correct_answer": "Это обязательная проверка по требованию Ростехнадзора.",
                "phase": "any",
            },
        ],
        mode="replace",
    )
    loop = asyncio.new_event_loop()

    # Близкая фраза в нужной фазе → срабатывает
    r1 = loop.run_until_complete(svc.match("а зачем вам нужен наш руководитель", "secretary"))
    assert r1 and "испытания" in r1, r1
    # Та же фраза, но правило только для secretary → в lpr_main не срабатывает
    r2 = loop.run_until_complete(svc.match("а зачем вам нужен наш руководитель", "lpr_main"))
    assert r2 is None, r2
    # Правило phase=any срабатывает в любой фазе
    r3 = loop.run_until_complete(svc.match("нам не нужны ваши услуги", "lpr_main"))
    assert r3 and "Ростехнадзора" in r3, r3
    # Нерелевантная фраза → None
    r4 = loop.run_until_complete(svc.match("какая сегодня погода в москве", "secretary"))
    assert r4 is None, r4
    print("   ✅ OK\n")


def main():
    print("\n🛠  Тесты слоя правок скрипта\n")
    test_parse_csv()
    test_parse_xlsx()
    test_crud_and_export()
    test_match()
    print("=" * 60)
    print("✅ Все тесты завершены!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
