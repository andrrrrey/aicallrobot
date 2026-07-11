"""Клиент HTTP-API ``res24.php`` АТС заказчика.

Основной канал управления/мониторинга звонков (см. документацию заказчика):

* ``call``   — инициировать звонок (click-to-call: звонит ``from`` → набирает ``to``);
* ``status`` — список активных каналов (состояние, bridgeid, кодек, секунды);
* ``cdr``    — запись о звонке по uniqueid (disposition/duration + путь к записи);
* запись разговора доступна по ``/records/<record>.mp3``.

Ответы ``call``/``chanspy`` приходят в JSONP-обёртке ``asterisk_cb({...});``,
``status``/``cdr`` — «чистым» JSON. Парсер снимает обёртку в обоих случаях.
"""

from __future__ import annotations

import json
import re

import httpx
from loguru import logger

from app.core.config import get_settings

_JSONP_RE = re.compile(r"^\s*asterisk_cb\((.*)\)\s*;?\s*$", re.DOTALL)


def _parse_response(text: str) -> dict:
    """Снимает JSONP-обёртку ``asterisk_cb(...)`` и парсит JSON."""
    m = _JSONP_RE.match(text)
    payload = m.group(1) if m else text
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        logger.warning(f"res24: не удалось разобрать ответ: {text[:200]}")
        return {}


class Res24Client:
    def __init__(self):
        s = get_settings()
        self.base_url = s.res24_base_url.rstrip("/")
        self.login = s.res24_login
        self.secret = s.res24_secret
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def _get(self, action: str, **params) -> dict:
        query = {"_login": self.login, "_secret": self.secret, "_action": action, **params}
        url = f"{self.base_url}/api/res24.php"
        resp = await self._http().get(url, params=query)
        resp.raise_for_status()
        return _parse_response(resp.text)

    async def call(self, to: str, from_ext: str) -> dict:
        """Инициировать звонок: Asterisk звонит ``from_ext`` и набирает ``to``."""
        logger.info(f"res24 call: from={from_ext} to={to}")
        return await self._get("call", **{"from": from_ext, "to": to})

    async def status(self) -> list[dict]:
        """Список активных каналов."""
        data = await self._get("status")
        return data.get("data", []) if isinstance(data, dict) else []

    async def cdr(self, uniqueid: str) -> dict | None:
        """Запись о звонке по uniqueid (disposition/duration/record)."""
        data = await self._get("cdr", uniqueid=uniqueid)
        rows = data.get("data", []) if isinstance(data, dict) else []
        return rows[0] if rows else None

    def recording_url(self, record_path: str) -> str:
        """Строит URL записи разговора из поля ``record`` CDR."""
        if not record_path:
            return ""
        return f"{self.base_url}/records/{record_path}.mp3"

    async def find_call_channel(self, to_digits: str, from_ext: str) -> dict | None:
        """Ищет в status канал нашего звонка (по расширению робота и номеру `to`).

        Возвращает найденный канал (с uniqueid/linkedid/bridgeid/channelstatedesc)
        или None. Эвристика: канал, где наш экстеншен фигурирует как
        calleridnum/connectedlinenum, а `to`-цифры — в exten/dnid/connectedlinenum.
        Может потребовать подстройки под реальные ответы АТС.
        """
        channels = await self.status()
        for ch in channels:
            fields = " ".join(str(ch.get(k, "")) for k in (
                "calleridnum", "connectedlinenum", "exten", "dnid",
                "effectiveconnectedlinenum",
            ))
            if from_ext and from_ext in fields and to_digits and to_digits[-7:] in fields:
                return ch
        return None

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
