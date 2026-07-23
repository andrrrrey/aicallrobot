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
from app.services.models import (
    Campaign, Client, ClientStatus, CampaignStatus, ClientBase, BaseContact,
)
from app.services.telephony.dialplan import resolve

# Подсказки для распознавания строки-шапки таблицы.
_HEADER_HINTS = ("phone", "телефон", "номер", "name", "имя", "фио", "company",
                 "компания", "организация")


def _looks_like_header(cells: list[str]) -> bool:
    joined = " ".join(c.lower() for c in cells if c)
    return any(h in joined for h in _HEADER_HINTS)


def _cell_str(v) -> str:
    """Приводит значение ячейки к строке, не превращая номер телефона в float.

    Excel часто хранит телефон как число (79001112233), и наивный ``str`` для
    float даёт «79001112233.0». Целочисленные float приводим к int.
    """
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def read_table(file_bytes: bytes, filename: str) -> list[list[str]]:
    """Читает CSV/XLS/XLSX в матрицу строк (без интерпретации столбцов)."""
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    rows: list[list[str]] = []

    if ext == "csv":
        text = file_bytes.decode("utf-8-sig", errors="replace")
        sample = text[:2048]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
        rows = [[(_c or "").strip() for _c in r] for r in csv.reader(io.StringIO(text), dialect)]
    elif ext == "xlsx":
        try:
            import openpyxl
        except ImportError:
            raise ValueError("Для чтения .xlsx требуется библиотека openpyxl")
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb.active
        for r in ws.iter_rows(values_only=True):
            rows.append([_cell_str(c).strip() for c in r])
    elif ext == "xls":
        # Легаси-формат Excel 97–2003 (бинарный) — читаем через xlrd.
        try:
            import xlrd
        except ImportError:
            raise ValueError("Для чтения .xls требуется библиотека xlrd")
        book = xlrd.open_workbook(file_contents=file_bytes)
        sheet = book.sheet_by_index(0)
        for r in range(sheet.nrows):
            rows.append([_cell_str(c).strip() for c in sheet.row_values(r)])
    else:
        raise ValueError("Поддерживаются только .csv, .xls и .xlsx")

    # Отбрасываем полностью пустые строки
    return [r for r in rows if any(r)]


def _phone_digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _looks_like_phone(value: str) -> bool:
    """Похоже ли значение на телефон: 6–15 цифр и преимущественно цифры."""
    if not value:
        return False
    digits = _phone_digits(value)
    non_space = "".join(value.split())
    return 6 <= len(digits) <= 15 and len(digits) >= 0.6 * max(1, len(non_space))


def _has_letters(value: str) -> bool:
    return any(ch.isalpha() for ch in (value or ""))


def detect_mapping(rows: list[list[str]], has_header: bool) -> dict:
    """Определяет, какой столбец — телефон, имя, компания (эвристика).

    Возвращает {"phone": idx|None, "name": idx|None, "company": idx|None}.
    """
    data = rows[1:] if has_header else rows
    sample = data[:30]
    ncol = max((len(r) for r in sample), default=0)
    if ncol == 0:
        return {"phone": None, "name": None, "company": None}

    def col_vals(c: int) -> list[str]:
        return [r[c] for r in sample if c < len(r) and r[c]]

    # Телефон — столбец с наибольшей долей «телефоноподобных» значений
    phone_col, phone_score = None, -1.0
    for c in range(ncol):
        vals = col_vals(c)
        if not vals:
            continue
        score = sum(1 for v in vals if _looks_like_phone(v)) / len(vals)
        if score > phone_score:
            phone_score, phone_col = score, c
    if phone_score <= 0:
        phone_col = 0  # ничего не нашли — берём первый столбец

    # Имя/компания — текстовые столбцы (с буквами), кроме телефонного
    def text_score(c: int) -> float:
        vals = col_vals(c)
        if not vals:
            return 0.0
        return sum(1 for v in vals if _has_letters(v)) / len(vals)

    text_cols = sorted(
        (c for c in range(ncol) if c != phone_col and text_score(c) > 0.3),
        key=text_score, reverse=True,
    )
    name_col = text_cols[0] if text_cols else None
    company_col = text_cols[1] if len(text_cols) > 1 else None
    return {"phone": phone_col, "name": name_col, "company": company_col}


def rows_to_contacts(rows: list[list[str]], mapping: dict, has_header: bool) -> list[dict]:
    """Преобразует матрицу строк в контакты по заданному сопоставлению столбцов."""
    data = rows[1:] if has_header else rows
    pc = mapping.get("phone")
    nc = mapping.get("name")
    cc = mapping.get("company")
    out: list[dict] = []
    for r in data:
        phone = r[pc].strip() if pc is not None and pc < len(r) else ""
        if not phone or not _phone_digits(phone):
            continue
        name = (r[nc].strip() if nc is not None and nc < len(r) else "")[:255]
        company = (r[cc].strip() if cc is not None and cc < len(r) else "")[:255]
        out.append({"phone": phone[:64], "name": name, "company": company})
    return out


def preview_table(file_bytes: bytes, filename: str, sample_size: int = 8) -> dict:
    """Готовит превью файла для выбора столбцов в UI.

    Возвращает число столбцов, первые строки, догадку о наличии шапки и
    предполагаемое сопоставление столбцов.
    """
    rows = read_table(file_bytes, filename)
    if not rows:
        raise ValueError("Файл пуст")
    has_header = _looks_like_header(rows[0])
    mapping = detect_mapping(rows, has_header)
    ncol = max((len(r) for r in rows[: sample_size + 1]), default=0)
    return {
        "columns": ncol,
        "has_header": has_header,
        "header": rows[0] if has_header else [],
        "sample": [r for r in rows[:sample_size]],
        "total_rows": len(rows) - (1 if has_header else 0),
        "mapping": mapping,
    }


def parse_clients_table(file_bytes: bytes, filename: str, mapping: dict | None = None,
                        has_header: bool | None = None) -> list[dict]:
    """Разбирает CSV/XLS/XLSX в список клиентов (phone/name/company/route).

    Столбцы определяются автоматически (телефон ищется по содержимому, а не по
    позиции), либо задаются явным ``mapping``. Строка-шапка распознаётся
    эвристикой, если ``has_header`` не задан явно.
    """
    rows = read_table(file_bytes, filename)
    if not rows:
        return []
    if has_header is None:
        has_header = _looks_like_header(rows[0])
    if mapping is None:
        mapping = detect_mapping(rows, has_header)
    contacts = rows_to_contacts(rows, mapping, has_header)
    for c in contacts:
        c["route"] = resolve(c["phone"]).route.value
    return contacts


# === Базы клиентов (переиспользуемые списки контактов) ===

async def create_base(name: str) -> int:
    async with session_scope() as s:
        base = ClientBase(name=name)
        s.add(base)
        await s.flush()
        return base.id


async def list_bases() -> list[dict]:
    async with session_scope() as s:
        bases = (await s.execute(
            select(ClientBase).order_by(ClientBase.created_at.desc())
        )).scalars().all()
        counts = dict((await s.execute(
            select(BaseContact.base_id, func.count()).group_by(BaseContact.base_id)
        )).all())
        return [
            {"id": b.id, "name": b.name, "created_at": b.created_at,
             "count": counts.get(b.id, 0)}
            for b in bases
        ]


async def get_base(base_id: int) -> ClientBase | None:
    async with session_scope() as s:
        return await s.get(ClientBase, base_id)


async def delete_base(base_id: int) -> bool:
    async with session_scope() as s:
        base = await s.get(ClientBase, base_id)
        if not base:
            return False
        await s.delete(base)
        return True


async def add_base_contacts(base_id: int, contacts: list[dict]) -> int:
    """Добавляет контакты в базу. contacts: [{phone, name, company}]."""
    async with session_scope() as s:
        objs = [
            BaseContact(
                base_id=base_id,
                phone=(c.get("phone", "") or "")[:64],
                name=(c.get("name", "") or "")[:255],
                company=(c.get("company", "") or "")[:255],
            )
            for c in contacts if c.get("phone")
        ]
        s.add_all(objs)
        return len(objs)


async def list_base_contacts(base_id: int, limit: int = 100, offset: int = 0) -> dict:
    async with session_scope() as s:
        total = (await s.execute(
            select(func.count()).where(BaseContact.base_id == base_id)
        )).scalar_one()
        rows = (await s.execute(
            select(BaseContact).where(BaseContact.base_id == base_id)
            .order_by(BaseContact.id).limit(limit).offset(offset)
        )).scalars().all()
        contacts = [
            {"id": c.id, "phone": c.phone, "name": c.name, "company": c.company}
            for c in rows
        ]
    return {"total": total, "limit": limit, "offset": offset, "contacts": contacts}


async def delete_base_contact(contact_id: int) -> bool:
    async with session_scope() as s:
        c = await s.get(BaseContact, contact_id)
        if not c:
            return False
        await s.delete(c)
        return True


async def copy_base_to_campaign(base_id: int, campaign_id: int) -> int:
    """Копирует контакты базы в рабочие строки clients кампании."""
    async with session_scope() as s:
        contacts = (await s.execute(
            select(BaseContact).where(BaseContact.base_id == base_id)
        )).scalars().all()
        objs = [
            Client(
                campaign_id=campaign_id,
                phone=c.phone,
                name=c.name,
                company=c.company,
                route=resolve(c.phone).route.value,
            )
            for c in contacts
        ]
        s.add_all(objs)
        return len(objs)


# === Операции над БД ===

async def create_campaign(
    name: str,
    scenario_id: str = "default",
    algo_version: str = "v2",
    voice_config: dict | None = None,
    call_window_start: int = 0,
    call_window_end: int = 24,
    max_concurrent: int = 0,
    base_id: int | None = None,
) -> int:
    async with session_scope() as s:
        camp = Campaign(
            name=name,
            scenario_id=scenario_id,
            algo_version=algo_version,
            voice_config=json.dumps(voice_config or {}, ensure_ascii=False),
            call_window_start=call_window_start,
            call_window_end=call_window_end,
            max_concurrent=max_concurrent,
            base_id=base_id,
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
                "voice_config": _safe_json(c.voice_config),
                "call_window_start": c.call_window_start,
                "call_window_end": c.call_window_end,
                "max_concurrent": c.max_concurrent,
            }
            for c in camps
        ]


def _safe_json(text: str) -> dict:
    try:
        return json.loads(text or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


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
    duration: int = 0,
    ended_at: float | None = None,
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
        if duration:
            values["duration"] = int(duration)
        if ended_at is not None:
            values["ended_at"] = ended_at
        if next_attempt_at is not None:
            values["next_attempt_at"] = next_attempt_at
        await s.execute(update(Client).where(Client.id == client_id).values(**values))


# === Статистика / дашборд ===

# Статусы, означающие завершённую попытку дозвона (для дозваниваемости).
_DIALED_STATUSES = (
    ClientStatus.DONE.value,
    ClientStatus.NO_ANSWER.value,
    ClientStatus.BUSY.value,
    ClientStatus.FAILED.value,
)
_QUALIFICATIONS = ("interested", "callback", "not_interested", "unknown")


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


async def campaign_stats(campaign_id: int) -> dict:
    """Расширенная статистика по кампании для дашборда.

    Возвращает счётчики по статусам и по квалификации, ключевые проценты
    (результативность, дозваниваемость), среднюю длительность и число попыток,
    воронку и временные ряды (звонки по дням и по часу суток).
    """
    async with session_scope() as s:
        # Счётчики по статусу набора
        status_rows = (await s.execute(
            select(Client.status, func.count())
            .where(Client.campaign_id == campaign_id)
            .group_by(Client.status)
        )).all()
        by_status = {status: cnt for status, cnt in status_rows}

        # Счётчики по квалификации (учитываем только реально завершённые разговоры)
        qual_rows = (await s.execute(
            select(Client.client_status, func.count())
            .where(
                Client.campaign_id == campaign_id,
                Client.status == ClientStatus.DONE.value,
            )
            .group_by(Client.client_status)
        )).all()
        by_qualification = {q: 0 for q in _QUALIFICATIONS}
        for q, cnt in qual_rows:
            by_qualification[q] = by_qualification.get(q, 0) + cnt

        # Средняя длительность разговора (по завершённым с известной длительностью)
        avg_dur, done_with_dur = (await s.execute(
            select(func.avg(Client.duration), func.count())
            .where(
                Client.campaign_id == campaign_id,
                Client.status == ClientStatus.DONE.value,
                Client.duration > 0,
            )
        )).one()

        # Среднее число попыток по завершённым попыткам дозвона
        avg_attempts = (await s.execute(
            select(func.avg(Client.attempts)).where(
                Client.campaign_id == campaign_id,
                Client.status.in_(_DIALED_STATUSES),
            )
        )).scalar_one()

        # Временные ряды: время завершения разговоров
        ended_rows = (await s.execute(
            select(Client.ended_at).where(
                Client.campaign_id == campaign_id,
                Client.ended_at.is_not(None),
            )
        )).all()

    total = sum(by_status.values())
    done = by_status.get(ClientStatus.DONE.value, 0)
    dialed = sum(by_status.get(st, 0) for st in _DIALED_STATUSES)
    interested = by_qualification.get("interested", 0)
    callback = by_qualification.get("callback", 0)

    # Звонки по дням и по часу суток
    by_day: dict[str, int] = {}
    by_hour = [0] * 24
    for (ended_at,) in ended_rows:
        lt = time.localtime(ended_at)
        day = time.strftime("%Y-%m-%d", lt)
        by_day[day] = by_day.get(day, 0) + 1
        by_hour[lt.tm_hour] += 1
    calls_by_day = [{"day": d, "count": by_day[d]} for d in sorted(by_day)]

    return {
        "campaign_id": campaign_id,
        "total": total,
        "by_status": by_status,
        "by_qualification": by_qualification,
        "dialed": dialed,
        "answered": done,
        "interested": interested,
        # Результативность = доля заинтересованных от всей базы
        "success_rate": _pct(interested, total),
        # Конверсия = доля заинтересованных от дозвонившихся
        "conversion_rate": _pct(interested, done),
        # Дозваниваемость = доля дозвонившихся от завершённых попыток
        "answer_rate": _pct(done, dialed),
        "avg_duration": round(float(avg_dur or 0.0), 1),
        "avg_attempts": round(float(avg_attempts or 0.0), 2),
        "done_with_duration": done_with_dur,
        "funnel": {
            "total": total,
            "dialed": dialed,
            "answered": done,
            "interested": interested,
            "callback": callback,
        },
        "calls_by_day": calls_by_day,
        "calls_by_hour": by_hour,
    }


async def list_clients(
    campaign_id: int,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Список клиентов кампании (с пагинацией и фильтром по статусу набора)."""
    async with session_scope() as s:
        conds = [Client.campaign_id == campaign_id]
        if status:
            conds.append(Client.status == status)
        total = (await s.execute(
            select(func.count()).where(*conds)
        )).scalar_one()
        rows = (await s.execute(
            select(Client)
            .where(*conds)
            .order_by(Client.id)
            .limit(limit)
            .offset(offset)
        )).scalars().all()
        clients = [
            {
                "id": c.id,
                "phone": c.phone,
                "name": c.name,
                "company": c.company,
                "status": c.status,
                "client_status": c.client_status,
                "attempts": c.attempts,
                "duration": c.duration,
                "call_id": c.call_id,
                "recording_url": c.recording_url,
                "summary": c.summary,
            }
            for c in rows
        ]
    return {"total": total, "limit": limit, "offset": offset, "clients": clients}


async def dashboard_summary() -> dict:
    """Сводка по всем кампаниям для верхнего уровня дашборда."""
    async with session_scope() as s:
        campaigns_total = (await s.execute(
            select(func.count()).select_from(Campaign)
        )).scalar_one()
        campaigns_running = (await s.execute(
            select(func.count()).where(Campaign.status == CampaignStatus.RUNNING.value)
        )).scalar_one()

        status_rows = (await s.execute(
            select(Client.status, func.count()).group_by(Client.status)
        )).all()
        by_status = {status: cnt for status, cnt in status_rows}

        qual_rows = (await s.execute(
            select(Client.client_status, func.count())
            .where(Client.status == ClientStatus.DONE.value)
            .group_by(Client.client_status)
        )).all()
        by_qualification = {q: 0 for q in _QUALIFICATIONS}
        for q, cnt in qual_rows:
            by_qualification[q] = by_qualification.get(q, 0) + cnt

        avg_dur = (await s.execute(
            select(func.avg(Client.duration)).where(
                Client.status == ClientStatus.DONE.value,
                Client.duration > 0,
            )
        )).scalar_one()

        ended_rows = (await s.execute(
            select(Client.ended_at).where(Client.ended_at.is_not(None))
        )).all()

    total = sum(by_status.values())
    done = by_status.get(ClientStatus.DONE.value, 0)
    dialed = sum(by_status.get(st, 0) for st in _DIALED_STATUSES)
    interested = by_qualification.get("interested", 0)

    by_day: dict[str, int] = {}
    for (ended_at,) in ended_rows:
        day = time.strftime("%Y-%m-%d", time.localtime(ended_at))
        by_day[day] = by_day.get(day, 0) + 1
    calls_by_day = [{"day": d, "count": by_day[d]} for d in sorted(by_day)]

    return {
        "campaigns_total": campaigns_total,
        "campaigns_running": campaigns_running,
        "clients_total": total,
        "calls_done": done,
        "interested": interested,
        "success_rate": _pct(interested, total),
        "answer_rate": _pct(done, dialed),
        "avg_duration": round(float(avg_dur or 0.0), 1),
        "by_status": by_status,
        "by_qualification": by_qualification,
        "calls_by_day": calls_by_day,
    }
