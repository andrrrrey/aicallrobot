"""Диалер: фоновый обзвон клиентов из кампаний через АТС заказчика.

Соблюдает лимиты одновременных соединений **по маршруту** (у транка t2 — 1 линия,
у «Телезона» на местные — до 30) и общий лимит ``max_concurrent_calls``. На
неответ/занято планирует перезвон с возрастающей паузой до ``max_retries``.

Звонок ведёт SIP-агент (робот как экстеншен). Метод ``originate`` блокирует поток
на всё время разговора, поэтому каждый звонок запускается отдельной asyncio-задачей;
по завершении результат записывается в БД, а счётчик активных линий маршрута
освобождается.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from loguru import logger

# Москва — постоянный UTC+3 (без перехода на летнее время с 2014 г.). Используем
# фиксированное смещение, чтобы не зависеть от tzdata в slim-образе.
MSK_TZ = timezone(timedelta(hours=3))

from app.core.config import get_settings
from app.services import registry, campaign_service
from app.services.models import ClientStatus, CampaignStatus
from app.services.telephony.dialplan import resolve, Route
from app.services.telephony.agent import sip_agent


class Dialer:
    def __init__(self):
        self._settings = get_settings()
        self._task: asyncio.Task | None = None
        self._running_campaigns: set[int] = set()
        # Активные линии по маршруту — трактуем как лимит транка в целом
        self._active_by_route: dict[str, int] = {}
        # Активные звонки по кампании — для per-campaign лимита одновременных звонков
        self._active_by_campaign: dict[int, int] = {}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()

    def _route_limit(self, route: str) -> int:
        if route == Route.T2.value:
            return self._settings.route_limit_t2
        if route == Route.LOCAL.value:
            return self._settings.route_limit_local
        return 0  # неизвестный/внутренний маршрут не обзваниваем

    def _active_total(self) -> int:
        return sum(self._active_by_route.values())

    # --- Управление кампаниями ---

    async def start_campaign(self, campaign_id: int):
        await campaign_service.set_campaign_status(campaign_id, CampaignStatus.RUNNING)
        self._running_campaigns.add(campaign_id)
        self._ensure_loop()
        logger.info(f"Dialer: кампания {campaign_id} запущена")

    async def stop_campaign(self, campaign_id: int):
        self._running_campaigns.discard(campaign_id)
        await campaign_service.set_campaign_status(campaign_id, CampaignStatus.PAUSED)
        logger.info(f"Dialer: кампания {campaign_id} остановлена")

    def _ensure_loop(self):
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def shutdown(self):
        self._stop.set()
        if self._task:
            self._task.cancel()

    # --- Основной цикл ---

    async def _run(self):
        logger.info("Dialer loop started")
        try:
            while not self._stop.is_set():
                for campaign_id in list(self._running_campaigns):
                    try:
                        await self._tick_campaign(campaign_id)
                    except Exception as e:
                        logger.error(f"Dialer tick failed (campaign {campaign_id}): {e}")
                await asyncio.sleep(self._settings.dialer_poll_interval)
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Dialer loop stopped")

    async def _tick_campaign(self, campaign_id: int):
        camp = await campaign_service.get_campaign(campaign_id)
        if not camp or camp.status != CampaignStatus.RUNNING.value:
            self._running_campaigns.discard(campaign_id)
            return
        if not self._in_call_window(camp):
            return

        voice_config = _safe_json(camp.voice_config)

        # Лимит одновременных звонков кампании (0 = глобальный лимит)
        camp_limit = camp.max_concurrent if camp.max_concurrent and camp.max_concurrent > 0 \
            else self._settings.max_concurrent_calls

        # По каждому маршруту берём столько клиентов, сколько позволяет лимит
        for route in (Route.LOCAL.value, Route.T2.value):
            async with self._lock:
                route_free = self._route_limit(route) - self._active_by_route.get(route, 0)
                global_free = self._settings.max_concurrent_calls - self._active_total()
                camp_free = camp_limit - self._active_by_campaign.get(campaign_id, 0)
                slots = max(0, min(route_free, global_free, camp_free))
                if slots <= 0:
                    continue
                claimed = await campaign_service.claim_due_clients(campaign_id, slots, route=route)
                self._active_by_route[route] = self._active_by_route.get(route, 0) + len(claimed)
                self._active_by_campaign[campaign_id] = \
                    self._active_by_campaign.get(campaign_id, 0) + len(claimed)

            for client in claimed:
                asyncio.create_task(self._dial_client(camp, client, voice_config))

    def _in_call_window(self, camp) -> bool:
        """Проверяет, попадает ли текущее московское время в окно обзвона.

        Часы окна (call_window_start/end) задаются по МСК независимо от таймзоны
        сервера.
        """
        start, end = camp.call_window_start, camp.call_window_end
        if start == 0 and end == 24:
            return True
        hour = datetime.now(MSK_TZ).hour
        return start <= hour < end

    # --- Один звонок ---

    async def _dial_client(self, camp, client: dict, voice_config: dict):
        client_id = client["id"]
        phone = client["phone"]
        route = client["route"]
        target = resolve(phone, national_prefix=self._settings.dial_national_prefix)

        try:
            if not target.valid:
                await campaign_service.mark_result(client_id, ClientStatus.FAILED)
                return

            session = await registry.call_manager.start_call(
                phone_number=phone,
                scenario_id=camp.scenario_id,
                algo_version=camp.algo_version,
            )
            scenario = registry.scenario_manager.get_scenario(camp.scenario_id)

            # Приветствие + инициализация состояния движка
            if camp.algo_version == "v2":
                greeting = registry.script_v2_engine.greeting(session.call_id).get("robot_text", "")
            else:
                greeting = scenario.greeting or ""

            result = await sip_agent.originate(
                call_id=session.call_id,
                session=session,
                scenario=scenario,
                number=target.to,
                greeting=greeting,
                voice_config=voice_config,
            )

            await self._record_result(client_id, session.call_id, result)
        except Exception as e:
            logger.error(f"Dial failed for client {client_id} ({phone}): {e}")
            await self._schedule_retry_or_fail(client_id, attempts_hint=None)
        finally:
            async with self._lock:
                self._active_by_route[route] = max(0, self._active_by_route.get(route, 0) - 1)
                self._active_by_campaign[camp.id] = \
                    max(0, self._active_by_campaign.get(camp.id, 0) - 1)

    async def _record_result(self, client_id: int, call_id: str, result):
        if result.status == "answered":
            uniqueid = getattr(result, "uniqueid", "") or ""
            recording_url = await self._fetch_recording_url(uniqueid)
            await campaign_service.mark_result(
                client_id, ClientStatus.DONE, call_id=call_id,
                client_status=result.client_status, summary=result.summary,
                duration=int(getattr(result, "duration", 0) or 0),
                ended_at=time.time(),
                asterisk_uniqueid=uniqueid,
                recording_url=recording_url,
            )
        else:
            # no_answer / busy / failed → перезвон или провал
            await self._schedule_retry_or_fail(client_id, failed_status=result.status)

    async def _fetch_recording_url(self, uniqueid: str) -> str:
        """Best-effort: получить URL записи разговора из CDR АТС (res24).

        Требует боевого окружения (живая АТС + заполненный uniqueid). При любой
        ошибке или отсутствии данных возвращает пустую строку — запись просто не
        показывается в дашборде.
        """
        if not uniqueid:
            return ""
        try:
            from app.services.telephony.res24_client import Res24Client
            client = Res24Client()
            try:
                cdr = await client.cdr(uniqueid)
                if cdr and cdr.get("record"):
                    return client.recording_url(cdr["record"])
            finally:
                await client.aclose()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"recording_url fetch failed (uniqueid={uniqueid}): {e}")
        return ""

    async def _schedule_retry_or_fail(self, client_id: int, failed_status: str = "failed",
                                      attempts_hint=None):
        """Планирует перезвон, если не исчерпаны попытки; иначе помечает провал."""
        from app.services.db import session_scope
        from app.services.models import Client
        async with session_scope() as s:
            c = await s.get(Client, client_id)
            if c is None:
                return
            if c.attempts >= self._settings.max_retries:
                c.status = ClientStatus.FAILED.value
            else:
                c.status = ClientStatus.CALLBACK.value
                c.next_attempt_at = time.time() + self._settings.retry_backoff_base * c.attempts


def _safe_json(text: str) -> dict:
    try:
        return json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}


# Синглтон диалера
dialer = Dialer()
