"""Менеджер настроек диалера и антиспам-темпа (persist в JSON, правится из админки).

Зачем отдельный слой поверх ``Settings``: параметры обзвона — лимиты одновременных
линий, паузы между наборами, дневной лимит — оператор должен подстраивать «на лету»
без правки ``.env`` и перезапуска контейнера. Поэтому фактические значения хранятся
в JSON-файле (``dialer_settings_file``), а значения из ``Settings`` служат лишь
дефолтами при первом запуске.

Диалер читает значения через этот менеджер на каждом тике, поэтому изменения из
админки применяются сразу, без рестарта.
"""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from app.core.config import get_settings


# Схема настроек: ключ → (тип, минимум). Максимум не ограничиваем жёстко —
# валидация только на «не мусор и не отрицательное».
_INT_KEYS = ("max_concurrent_calls", "route_limit_t2", "route_limit_local",
             "max_retries", "dial_daily_limit_per_route")
_FLOAT_KEYS = ("dialer_poll_interval", "retry_backoff_base", "dial_min_interval_sec",
               "dial_jitter_sec", "dial_number_cooldown_hours")


def _defaults() -> dict:
    """Дефолты берём из ``Settings`` (env/.env), чтобы старое поведение сохранялось."""
    s = get_settings()
    return {
        "max_concurrent_calls": s.max_concurrent_calls,
        "route_limit_t2": s.route_limit_t2,
        "route_limit_local": s.route_limit_local,
        "max_retries": s.max_retries,
        "retry_backoff_base": s.retry_backoff_base,
        "dialer_poll_interval": s.dialer_poll_interval,
        "dial_min_interval_sec": s.dial_min_interval_sec,
        "dial_jitter_sec": s.dial_jitter_sec,
        "dial_daily_limit_per_route": s.dial_daily_limit_per_route,
        "dial_number_cooldown_hours": s.dial_number_cooldown_hours,
    }


class DialerSettingsManager:
    """Хранит и отдаёт настройки диалера; персистит их в JSON-файл."""

    def __init__(self):
        self._path = Path(get_settings().dialer_settings_file)
        self._config: dict = {}
        self._load()

    def _load(self):
        base = _defaults()
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # Оставляем только известные ключи, поверх дефолтов
                base.update({k: v for k, v in loaded.items() if k in base})
                logger.info(f"Dialer settings loaded from {self._path}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to load dialer settings: {e}")
        self._config = base

    def get(self) -> dict:
        return self._config.copy()

    def _coerce(self, raw: dict) -> dict:
        """Приводит входные значения к нужным типам и отсекает отрицательные."""
        out = self._config.copy()
        for k in _INT_KEYS:
            if k in raw and raw[k] is not None:
                try:
                    out[k] = max(0, int(raw[k]))
                except (TypeError, ValueError):
                    continue
        for k in _FLOAT_KEYS:
            if k in raw and raw[k] is not None:
                try:
                    out[k] = max(0.0, float(raw[k]))
                except (TypeError, ValueError):
                    continue
        # Защита от вырожденного значения: слишком частый опрос грузит БД/АТС.
        if out["dialer_poll_interval"] < 1.0:
            out["dialer_poll_interval"] = 1.0
        return out

    def save(self, raw: dict) -> dict:
        """Мержит переданные поля поверх текущих, валидирует и сохраняет на диск."""
        self._config = self._coerce(raw or {})
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, indent=2)
            logger.info("Dialer settings saved")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to save dialer settings: {e}")
        return self._config.copy()

    # Типизированные аксессоры (диалер зовёт их на каждом тике)
    def _i(self, key: str) -> int:
        return int(self._config.get(key, 0))

    def _f(self, key: str) -> float:
        return float(self._config.get(key, 0.0))

    @property
    def max_concurrent_calls(self) -> int: return self._i("max_concurrent_calls")
    @property
    def route_limit_t2(self) -> int: return self._i("route_limit_t2")
    @property
    def route_limit_local(self) -> int: return self._i("route_limit_local")
    @property
    def max_retries(self) -> int: return self._i("max_retries")
    @property
    def retry_backoff_base(self) -> float: return self._f("retry_backoff_base")
    @property
    def poll_interval(self) -> float: return self._f("dialer_poll_interval")
    @property
    def min_interval_sec(self) -> float: return self._f("dial_min_interval_sec")
    @property
    def jitter_sec(self) -> float: return self._f("dial_jitter_sec")
    @property
    def daily_limit_per_route(self) -> int: return self._i("dial_daily_limit_per_route")
    @property
    def number_cooldown_sec(self) -> float: return self._f("dial_number_cooldown_hours") * 3600.0


# Синглтон
dialer_settings = DialerSettingsManager()
