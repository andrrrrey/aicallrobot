"""Формат набора номеров и классификация маршрута для АТС заказчика.

Заказчик описал план нумерации (при наборе с внутренних экстеншенов):

    1xx                       — внутренние номера
    [29]xxxxxx                — местные номера зоны 7391 через «Телезон»
    +79XXXXXXXXX              — мобильные (DEF) через t2
    [78]9XXXXXXXXX            — мобильные (DEF) через t2
    +7[345678]XXXXXXXXX       — ABC зоновые/междугородние через t2
    [78][345678]XXXXXXXXX     — ABC зоновые/междугородние через t2
    +710XXXXXXXXXX            — ABC/DEF через «Телезон»
    [78]10XXXXXXXXXX          — ABC/DEF через «Телезон»

Для инициирования звонка через их HTTP-API ``res24.php`` параметр ``to``
принимает **только цифры** (пример из документации: ``to=81234567890``).
Поэтому:

* локальные номера зоны 7391 набираем как есть (7 цифр, начинается на 2 или 9);
* остальные (мобильные/междугородние) приводим к ``8XXXXXXXXXX`` (11 цифр).

Классификация маршрута нужна **нам** для ограничения параллелизма: у транка t2
всего 1 одновременное соединение, у «Телезона» на местные — до 30. ``res24.php``
про лимит канала не сообщает, поэтому считаем маршрут сами по формату номера.
"""

import re
from dataclasses import dataclass
from enum import Enum


class Route(str, Enum):
    LOCAL = "local"        # местные номера зоны 7391 через «Телезон»
    T2 = "t2"              # мобильные/междугородние через t2 (SIP-GSM)
    INTERNAL = "internal"  # внутренние экстеншены 1xx
    UNKNOWN = "unknown"    # не удалось классифицировать


@dataclass
class DialTarget:
    """Результат разбора номера для набора."""
    to: str          # строка для параметра res24 `to` (только цифры)
    route: Route     # маршрут (для лимитов параллелизма)
    valid: bool      # можно ли набирать


def _digits(phone: str) -> str:
    """Оставляет только цифры (убирает +, скобки, пробелы, дефисы)."""
    return re.sub(r"\D", "", phone or "")


def classify_route(phone: str) -> Route:
    """Определяет маршрут по формату номера."""
    d = _digits(phone)

    # Внутренние 1xx
    if len(d) == 3 and d[0] == "1":
        return Route.INTERNAL

    # Местные номера зоны 7391: 7 цифр, начинается на 2 или 9
    if len(d) == 7 and d[0] in ("2", "9"):
        return Route.LOCAL

    # Национальные (11 цифр с кодом страны 7/8, либо 10 без кода) → t2
    if (len(d) == 11 and d[0] in ("7", "8")) or len(d) == 10:
        return Route.T2

    return Route.UNKNOWN


def resolve(phone: str, national_prefix: str = "8") -> DialTarget:
    """Приводит номер к формату набора res24 `to` и определяет маршрут.

    Args:
        phone: номер клиента в произвольном формате (+7…, 8…, 7…, с разделителями).
        national_prefix: префикс для национальных номеров в res24 (по умолчанию 8).
    """
    d = _digits(phone)
    route = classify_route(phone)

    if route == Route.LOCAL:
        return DialTarget(to=d, route=route, valid=True)

    if route == Route.INTERNAL:
        return DialTarget(to=d, route=route, valid=True)

    if route == Route.T2:
        # Приводим к национальному формату <prefix> + 10 цифр
        if len(d) == 11 and d[0] in ("7", "8"):
            core = d[1:]
        else:  # len == 10
            core = d
        return DialTarget(to=f"{national_prefix}{core}", route=route, valid=True)

    return DialTarget(to=d, route=Route.UNKNOWN, valid=False)
