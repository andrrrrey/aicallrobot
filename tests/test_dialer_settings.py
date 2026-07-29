#!/usr/bin/env python3
"""Тесты менеджера настроек диалера/антиспама (persist + валидация).

Проверяют: дефолты берутся из Settings, некорректный ввод приводится к типу и
отсекается (отрицательные → 0, слишком частый опрос клампится), значения
переживают перезагрузку, а derived-аксессоры считают корректно.

Запуск: python -m tests.test_dialer_settings  (или pytest)
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Изолированный файл настроек до импорта менеджера
_TMP = tempfile.mkdtemp()
os.environ["DIALER_SETTINGS_FILE"] = os.path.join(_TMP, "dialer_settings.json")

from app.services.dialer_settings import DialerSettingsManager  # noqa: E402


def test_defaults_from_settings():
    m = DialerSettingsManager()
    cfg = m.get()
    assert cfg["route_limit_t2"] == 1
    assert cfg["route_limit_local"] == 30
    assert cfg["dial_min_interval_sec"] == 12.0
    assert cfg["dial_daily_limit_per_route"] == 0
    # Аксессоры
    assert m.number_cooldown_sec == 24.0 * 3600.0
    assert m.min_interval_sec == 12.0


def test_coercion_and_clamping():
    m = DialerSettingsManager()
    saved = m.save({
        "dial_min_interval_sec": -5,       # отрицательное → 0
        "dialer_poll_interval": 0.1,       # слишком часто → клампится к 1.0
        "route_limit_t2": "2",             # строка → int
        "dial_daily_limit_per_route": 100,
        "not_a_field": 999,                # мусор игнорируется
    })
    assert saved["dial_min_interval_sec"] == 0.0
    assert saved["dialer_poll_interval"] == 1.0
    assert saved["route_limit_t2"] == 2
    assert saved["dial_daily_limit_per_route"] == 100
    assert "not_a_field" not in saved


def test_partial_merge_keeps_other_fields():
    m = DialerSettingsManager()
    m.save({"route_limit_local": 15})
    m.save({"dial_jitter_sec": 3})
    cfg = m.get()
    assert cfg["route_limit_local"] == 15   # не сброшено вторым save
    assert cfg["dial_jitter_sec"] == 3.0


def test_persistence_across_reload():
    m = DialerSettingsManager()
    m.save({"route_limit_t2": 4, "dial_number_cooldown_hours": 2})
    # Новый экземпляр читает с диска
    m2 = DialerSettingsManager()
    assert m2.route_limit_t2 == 4
    assert m2.number_cooldown_sec == 2 * 3600.0
    # И файл действительно содержит значения
    with open(os.environ["DIALER_SETTINGS_FILE"], encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["route_limit_t2"] == 4


def test_bad_values_are_ignored_not_crash():
    m = DialerSettingsManager()
    before = m.get()["route_limit_t2"]
    saved = m.save({"route_limit_t2": "not-a-number"})
    assert saved["route_limit_t2"] == before  # осталось прежним, без исключения


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("All dialer settings tests passed")


if __name__ == "__main__":
    _run_all()
