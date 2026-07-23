#!/usr/bin/env python3
"""Тесты баз клиентов и устойчивого парсера с сопоставлением столбцов.

Проверяют сценарий из реального бага: телефон не в первом столбце, а длинные
названия организаций не должны попадать в колонку phone (StringDataRightTruncation).

Запуск: python -m tests.test_client_bases
"""

import asyncio
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{os.path.join(tempfile.mkdtemp(), 'bases.db')}",
)


def _make_xls() -> bytes:
    """Файл в стиле проблемного: [Организация, Код, Категория, Телефон]."""
    import xlwt
    wb = xlwt.Workbook()
    ws = wb.add_sheet("base")
    ws.write(0, 0, "Организация"); ws.write(0, 1, "Код")
    ws.write(0, 2, "Категория"); ws.write(0, 3, "Телефон")
    long_name = "Бирюлевский экспериментальный завод Промышленное строительство"
    ws.write(1, 0, "1000 Вещей, торговый центр"); ws.write(1, 1, "00-00179795")
    ws.write(1, 2, "Бизнес центры/Торг.Центры"); ws.write(1, 3, 74951234567)
    ws.write(2, 0, long_name); ws.write(2, 1, "00-00592757")
    ws.write(2, 2, "Производственные"); ws.write(2, 3, "+7 (495) 765-43-21")
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def test_parser_mapping():
    from app.services import campaign_service as cs
    xls = _make_xls()

    prev = cs.preview_table(xls, "base.xls")
    assert prev["columns"] == 4, prev
    assert prev["has_header"] is True, prev
    assert prev["total_rows"] == 2, prev

    # Явное сопоставление: телефон — столбец 3 (индекс), имя — 0
    rows = cs.read_table(xls, "base.xls")
    contacts = cs.rows_to_contacts(rows, {"phone": 3, "name": 0, "company": 2}, has_header=True)
    assert len(contacts) == 2, contacts
    assert contacts[0]["phone"] == "74951234567", contacts[0]
    # Длинное название организации ушло в name, а не в phone
    assert contacts[1]["name"].startswith("Бирюлевский"), contacts[1]
    assert all(len(c["phone"]) <= 64 for c in contacts)
    print("   ✅ парсер: телефон берётся из выбранного столбца, длинные имена не в phone")


async def _run_db_flow():
    import app.services.db as db
    from app.services import campaign_service as cs
    from app.services.models import Client
    from sqlalchemy import select, func

    await db.init_db()
    bid = await cs.create_base("Тестовая база")
    await cs.add_base_contacts(bid, [{"phone": "+79001112233", "name": "Иван", "company": "Ромашка"}])

    rows = cs.read_table(_make_xls(), "base.xls")
    contacts = cs.rows_to_contacts(rows, {"phone": 3, "name": 0, "company": 2}, has_header=True)
    await cs.add_base_contacts(bid, contacts)

    bases = await cs.list_bases()
    assert bases[0]["count"] == 3, bases

    lc = await cs.list_base_contacts(bid)
    assert lc["total"] == 3, lc

    # Кампания из базы: контакты копируются в clients
    cid = await cs.create_campaign(name="Из базы", base_id=bid)
    copied = await cs.copy_base_to_campaign(bid, cid)
    assert copied == 3, copied
    async with db.session_scope() as s:
        n = (await s.execute(select(func.count()).where(Client.campaign_id == cid))).scalar_one()
    assert n == 3, n

    assert await cs.delete_base(bid) is True
    assert await cs.list_bases() == []
    print("   ✅ БД: база → контакты → копирование в кампанию → удаление")


async def main():
    print("\n🗂 Базы клиентов — тесты\n")
    test_parser_mapping()
    await _run_db_flow()
    print("\n✅ Все тесты баз клиентов пройдены\n")


if __name__ == "__main__":
    asyncio.run(main())
