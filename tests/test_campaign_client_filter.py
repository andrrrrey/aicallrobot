#!/usr/bin/env python3
"""Тест фильтра клиентов кампании по квалификации (client_status).

Проверяет, что в разделе «Кампании» можно отфильтровать звонки, в которых
ИИ выявил интерес («заинтересован»), — новый параметр ``client_status`` у
``campaign_service.list_clients`` / эндпоинта ``/campaigns/{id}/clients``.

Запуск: python -m pytest tests/test_campaign_client_filter.py -o asyncio_mode=auto
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{os.path.join(tempfile.mkdtemp(), 'filter.db')}",
)


async def _run():
    import app.services.db as db
    from app.services import campaign_service as cs
    from app.services.models import Client, ClientStatus

    await db.init_db()
    cid = await cs.create_campaign(name="Фильтр")

    # Три состоявшихся звонка: два «заинтересован», один «не интересно»,
    # плюс один неотвеченный (ещё без квалификации).
    async with db.session_scope() as s:
        s.add_all([
            Client(campaign_id=cid, phone="+70000000001", status=ClientStatus.DONE.value,
                   client_status="interested"),
            Client(campaign_id=cid, phone="+70000000002", status=ClientStatus.DONE.value,
                   client_status="interested"),
            Client(campaign_id=cid, phone="+70000000003", status=ClientStatus.DONE.value,
                   client_status="not_interested"),
            Client(campaign_id=cid, phone="+70000000004", status=ClientStatus.NO_ANSWER.value,
                   client_status="unknown"),
        ])

    # Без фильтра — все четыре
    all_clients = await cs.list_clients(cid)
    assert all_clients["total"] == 4, all_clients["total"]

    # Фильтр по квалификации «заинтересован» — только два
    interested = await cs.list_clients(cid, client_status="interested")
    assert interested["total"] == 2, interested["total"]
    assert all(c["client_status"] == "interested" for c in interested["clients"]), interested

    # Фильтр по статусу набора не сломан
    done = await cs.list_clients(cid, status=ClientStatus.DONE.value)
    assert done["total"] == 3, done["total"]

    # Комбинация статуса и квалификации
    both = await cs.list_clients(cid, status=ClientStatus.DONE.value,
                                 client_status="interested")
    assert both["total"] == 2, both["total"]

    # Счётчик «заинтересован» в статистике совпадает
    st = await cs.campaign_stats(cid)
    assert st["by_qualification"]["interested"] == 2, st["by_qualification"]
    print("   ✅ Фильтр клиентов по «заинтересован» работает")


def test_campaign_client_status_filter():
    asyncio.run(_run())


if __name__ == "__main__":
    asyncio.run(_run())
    print("\n✅ Тест фильтра кампании пройден\n")
