#!/usr/bin/env python3
"""Юнит-тесты формата набора и классификации маршрута (dialplan.py).

Запуск: python -m pytest tests/test_dialplan.py  (или python -m tests.test_dialplan)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.telephony.dialplan import resolve, classify_route, Route


def test_local_zone_7391():
    """Местные номера зоны 7391: 7 цифр, начинается на 2 или 9 → local, как есть."""
    for num in ("2123456", "9876543"):
        t = resolve(num)
        assert t.route == Route.LOCAL, num
        assert t.to == num, num
        assert t.valid


def test_mobile_def_various_formats():
    """Мобильные DEF в разных форматах → t2, приводятся к 8XXXXXXXXXX."""
    for num in ("+79161234567", "89161234567", "79161234567", "9161234567",
                "+7 (916) 123-45-67"):
        t = resolve(num)
        assert t.route == Route.T2, num
        assert t.to == "89161234567", num
        assert t.valid


def test_intercity_abc():
    """ABC междугородний (11 цифр) → t2, 8 + 10 цифр."""
    t = resolve("+74951234567")
    assert t.route == Route.T2
    assert t.to == "84951234567"
    assert t.valid


def test_national_prefix_configurable():
    """Префикс национального набора настраивается (например, 7 вместо 8)."""
    t = resolve("+79161234567", national_prefix="7")
    assert t.to == "79161234567"


def test_internal_extension():
    """Внутренние 1xx → internal."""
    t = resolve("117")
    assert t.route == Route.INTERNAL
    assert t.to == "117"
    assert t.valid


def test_unknown_invalid():
    """Непонятный номер → unknown, valid=False."""
    for num in ("1234", "5551234", "42"):
        t = resolve(num)
        assert t.route == Route.UNKNOWN, num
        assert not t.valid, num


def test_classify_route_direct():
    assert classify_route("2123456") == Route.LOCAL
    assert classify_route("89161234567") == Route.T2
    assert classify_route("117") == Route.INTERNAL
    assert classify_route("999") == Route.UNKNOWN


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  ❌ {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
