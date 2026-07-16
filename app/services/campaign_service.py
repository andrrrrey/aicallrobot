"""Сервис кампаний обзвона: импорт базы клиентов и операции над БД.

Используется и HTTP-эндпоинтами (импорт, создание/старт/стоп, прогресс), и
диалером (выбор клиентов на обзвон, обновление статусов).
"""

from __future__ import annotations

import csv
import io
import json
import time

from loguru import logger
from sqlalchemy import select, func, update

from app.services.db import session_scope
from app.services.models import Campaign, Client, ClientStatus, CampaignStatus
from app.services.telephony.dialplan import resolve

# Подсказки для распознавания строки-шапки таблицы.
_HEADER_HINTS = ("phone", "телефон", "номер", "name", "имя", "фио", "company",
                 "компания", "организация")


def _looks_like_header(cells: list[str]) -> bool:
    joined = " ".join(c.lower() for c in cells if c)
    return any(h in joined for h in _HEADER_HINTS)


def parse_clients_table(file_bytes: bytes, filename: str) -> list[dict]:
    """Разбирает CSV/XLSX в список клиентов.

    Ожидаемые столбцы (по порядку): телефон, имя, компания. Имя и компания
    необязательны. Строка-шапка распознаётся эвристикой и пропускается.
    """
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    rows: list[list[str]] = []

    if ext == "csv":
        text = file_bytes.decode("utf-8-sig", errors="replace")
        sample = text[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
        rows = [list(r) for r in csv.reader(io.StringIO(text), dialect)]
    elif ext == "xlsx":
        try:
            import openpyxl
        except ImportError:
            raise ValueError("Для чтения .xlsx требуется библиотека openpyxl")
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb.active
        for r in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else str(c) for c in r])
    else:
        raise ValueError("Поддерживаются только .csv и .xlsx")

    clients: list[dict] = []
    for i, cells in enumerate(rows):
        cells = [(c or "").strip() for c in cells]
        if not any(cells):
            continue
        if i == 0 and _looks_like_header(cells):
            continue
        while len(cells) < 3:
            cells.append("")
        phone, name, company = cells[0], cells[1], cells[2]
        if not phone:
            continue
        target = resolve(phone)
        clients.append({
            "phone": phone,
            "name": name,
            "company": company,
            "route": target.route.value,
        })
    return clients


# === Операции над БД ===

async def create_campaign(
    name: str,
    scenario_id: str = "default",
    algo_version: str = "v2",
    voice_config: dict | None = None,
    call_window_start: int = 0,
    call_window_end: int = 24,
) -> int:
    async with session_scope() as s:
        camp = Campaign(
            name=name,
            scenario_id=scenario_id,
            algo_version=algo_version,
            voice_config=json.dumps(voice_config or {}, ensure_ascii=False),
            call_window_start=call_window_start,
            call_window_end=call_window_end,
        )
        s.add(camp)
        await s.flush()
        return camp.id


async def import_clients(campaign_id: int, rows: list[dict]) -> int:
    async with session_scope() as s:
        objs = [
            Client(
                campaign_id=campaign_id,
                phone=r["phone"],
                name=r.get("name", ""),
                company=r.get("company", ""),
                route=r.get("route", ""),
            )
            for r in rows
        ]
        s.add_all(objs)
        return len(objs)


async def set_campaign_status(campaign_id: int, status: CampaignStatus):
    async with session_scope() as s:
        await s.execute(
            update(Campaign).where(Campaign.id == campaign_id).values(status=status.value)
        )


async def list_campaigns() -> list[dict]:
    async with session_scope() as s:
        camps = (await s.execute(select(Campaign).order_by(Campaign.created_at.desc()))).scalars().all()
        return [
            {
                "id": c.id, "name": c.name, "scenario_id": c.scenario_id,
                "algo_version": c.algo_version, "status": c.status,
                "created_at": c.created_at,
            }
            for c in camps
        ]


async def campaign_progress(campaign_id: int) -> dict:
    async with session_scope() as s:
        rows = (await s.execute(
            select(Client.status, func.count())
            .where(Client.campaign_id == campaign_id)
            .group_by(Client.status)
        )).all()
        by_status = {status: cnt for status, cnt in rows}
        interested = (await s.execute(
            select(func.count()).where(
                Client.campaign_id == campaign_id,
                Client.client_status == "interested",
            )
        )).scalar_one()
        total = sum(by_status.values())
        return {
            "campaign_id": campaign_id,
            "total": total,
            "by_status": by_status,
            "interested": interested,
        }


async def get_campaign(campaign_id: int) -> Campaign | None:
    async with session_scope() as s:
        return await s.get(Campaign, campaign_id)


async def claim_due_clients(campaign_id: int, limit: int, route: str | None = None) -> list[dict]:
    """Атомарно берёт до ``limit`` клиентов в работу (status → calling).

    Выбирает pending и callback (по наступившему next_attempt_at). Опционально
    фильтрует по маршруту (для лимитов транка). Возвращает простые dict'ы
    (id/phone/route/name), чтобы не тащить ORM-объекты за пределы сессии.
    """
    if limit <= 0:
        return []
    now = time.time()
    async with session_scope() as s:
        conds = [
            Client.campaign_id == campaign_id,
            Client.status.in_([ClientStatus.PENDING.value, ClientStatus.CALLBACK.value]),
            (Client.next_attempt_at.is_(None)) | (Client.next_attempt_at <= now),
        ]
        if route is not None:
            conds.append(Client.route == route)
        stmt = (
            select(Client)
            .where(*conds)
            .order_by(Client.next_attempt_at.is_(None).desc(), Client.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        clients = (await s.execute(stmt)).scalars().all()
        result = []
        for c in clients:
            c.status = ClientStatus.CALLING.value
            c.attempts += 1
            c.last_attempt_at = now
            result.append({"id": c.id, "phone": c.phone, "route": c.route, "name": c.name})
        return result


async def count_active_by_route(campaign_id: int) -> dict[str, int]:
    """Сколько клиентов сейчас в статусе calling по маршрутам (для лимитов)."""
    async with session_scope() as s:
        rows = (await s.execute(
            select(Client.route, func.count())
            .where(
                Client.campaign_id == campaign_id,
                Client.status == ClientStatus.CALLING.value,
            )
            .group_by(Client.route)
        )).all()
        return {route: cnt for route, cnt in rows}


async def mark_result(
    client_id: int,
    status: ClientStatus,
    call_id: str = "",
    asterisk_uniqueid: str = "",
    client_status: str = "",
    summary: str = "",
    recording_url: str = "",
    next_attempt_at: float | None = None,
):
    """Записывает результат звонка по клиенту."""
    async with session_scope() as s:
        values: dict = {"status": status.value}
        if call_id:
            values["call_id"] = call_id
        if asterisk_uniqueid:
            values["asterisk_uniqueid"] = asterisk_uniqueid
        if client_status:
            values["client_status"] = client_status
        if summary:
            values["summary"] = summary
        if recording_url:
            values["recording_url"] = recording_url
        if next_attempt_at is not None:
            values["next_attempt_at"] = next_attempt_at
        await s.execute(update(Client).where(Client.id == client_id).values(**values))
