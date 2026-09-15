#!/usr/bin/env python3
"""Проверка обзвона дополнительных номеров контакта (extra_phones).

Когда у компании в базе указано несколько телефонов, при недозвоне (занято/
неответ/ошибка) робот должен переключаться на следующий номер и набирать его,
а не бросать контакт. Когда номера кончились — обычная логика перезвона/провала.

Запуск: python -m pytest tests/test_extra_phones.py -o asyncio_mode=auto
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+aiosqlite:///{os.path.join(tempfile.mkdtemp(), 'extra_phones.db')}",
)


def test_extract_phones_splits_multiple_in_one_cell():
    # В одной ячейке несколько номеров → первый основной, остальные — extra_phones.
    from app.services.campaign_service import rows_to_contacts
    rows = [["телефон", "компания"],
            ["+7 495 111-22-33, +7 495 444-55-66; 84957778899", "ООО Ромашка"]]
    contacts = rows_to_contacts(rows, {"phone": 0, "name": None, "company": 1}, has_header=True)
    assert len(contacts) == 1, contacts
    c = contacts[0]
    assert len(c["extra_phones"]) == 2, c
    # Всего три уникальных номера: основной + два запасных.
    assert len([c["phone"], *c["extra_phones"]]) == 3


async def _run_dial_fallback():
    import app.services.db as db
    from app.services import campaign_service as cs
    from app.services.models import Client, ClientStatus
    from app.services.dialer import dialer

    await db.init_db()
    cid = await cs.create_campaign(name="Доп. номера")

    async with db.session_scope() as s:
        c = Client(
            campaign_id=cid,
            phone="+74950000001",
            extra_phones=json.dumps(["+74950000002", "+74950000003"], ensure_ascii=False),
            status=ClientStatus.PENDING.value,
            attempts=0,
        )
        s.add(c)
        await s.flush()
        client_id = c.id

    # 1) Недозвон по первому номеру → переключаемся на второй, набираем сразу.
    await dialer._schedule_retry_or_fail(client_id, failed_status="no_answer")
    async with db.session_scope() as s:
        c = await s.get(Client, client_id)
        assert c.phone == "+74950000002", c.phone
        assert json.loads(c.extra_phones) == ["+74950000003"], c.extra_phones
        assert c.status == ClientStatus.PENDING.value, c.status

    # 2) Недозвон по второму → третий (последний запасной).
    await dialer._schedule_retry_or_fail(client_id, failed_status="busy")
    async with db.session_scope() as s:
        c = await s.get(Client, client_id)
        assert c.phone == "+74950000003", c.phone
        assert json.loads(c.extra_phones) == [], c.extra_phones
        assert c.status == ClientStatus.PENDING.value, c.status

    # 3) Запасные кончились → обычная логика (перезвон/провал), номер не меняется.
    await dialer._schedule_retry_or_fail(client_id, failed_status="no_answer")
    async with db.session_scope() as s:
        c = await s.get(Client, client_id)
        assert c.phone == "+74950000003", c.phone
        assert c.status in (ClientStatus.CALLBACK.value, ClientStatus.FAILED.value), c.status


def test_dialer_falls_back_to_extra_phones():
    asyncio.run(_run_dial_fallback())


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-o", "asyncio_mode=auto", "-v"]))
