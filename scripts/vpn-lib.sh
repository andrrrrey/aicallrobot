#!/usr/bin/env bash
#
# Общие функции управления VPN-туннелем L2TP/IPsec до сети заказчика.
# Файл СОРСИТСЯ из res-vpn-up.sh и vpn-watchdog.sh — здесь нет автозапуска.
#
# Туннель состоит из ДВУХ слоёв (важно для диагностики):
#   1) IPsec/IKE (strongSwan)  — шифрованный канал; поднимается `ipsec up`.
#   2) L2TP/PPP (xl2tpd→ppp0)   — сам туннель и интерфейс ppp0; поднимается
#      командой "c <lac>" в управляющий сокет xl2tpd.
# IPsec может быть ESTABLISHED, а ppp0 при этом отсутствовать (ESP «0 bytes») —
# тогда маршрута в 192.168.0.0/24 нет и SIP-регистрация падает с
# "Network is unreachable". Поэтому здоровьем туннеля считаем именно
# наличие ppp0 + маршрута, а не статус IPsec.

# --- Конфигурация (переопределяется через окружение) ---
VPN_CONN="${VPN_CONN:-res-l2tp}"
L2TP_LAC="${L2TP_LAC:-res}"
VPN_ROUTE="${VPN_ROUTE:-192.168.0.0/24}"
PPP_IFACE="${PPP_IFACE:-ppp0}"
AI_ROBOT_DIR="${AI_ROBOT_DIR:-/opt/ai-robot}"
XL2TPD_CONTROL="${XL2TPD_CONTROL:-/var/run/xl2tpd/l2tp-control}"
PPP_WAIT_SECS="${PPP_WAIT_SECS:-20}"

# Окно доступа заказчика: ПН–ПТ 08:00–22:00 по Красноярску (GMT+7, без DST).
ACCESS_TZ_OFFSET_HOURS="${ACCESS_TZ_OFFSET_HOURS:-7}"
ACCESS_HOUR_START="${ACCESS_HOUR_START:-8}"
ACCESS_HOUR_END="${ACCESS_HOUR_END:-22}"   # правая граница НЕ включается

VPN_LOG_TAG="${VPN_LOG_TAG:-vpn}"

vlog() { echo "[$VPN_LOG_TAG] $*"; }
verr() { echo "[$VPN_LOG_TAG] $*" >&2; }

require_root() {
  if [ "$(id -u)" != "0" ]; then
    verr "запускать от root (sudo)"
    return 1
  fi
}

# Текущее время в зоне доступа (GMT+7). Выставляет KR_DOW (1=Пн..7=Вс) и KR_HOUR.
_access_now() {
  local now_utc kr
  now_utc="$(date -u +%s)"
  kr=$(( now_utc + ACCESS_TZ_OFFSET_HOURS * 3600 ))
  KR_DOW="$(date -u -d "@$kr" +%u)"
  KR_HOUR="$(date -u -d "@$kr" +%H)"
  KR_HOUR=$((10#$KR_HOUR))   # 08 -> 8 (без восьмеричной трактовки)
}

in_access_window() {
  _access_now
  [ "$KR_DOW" -ge 1 ] && [ "$KR_DOW" -le 5 ] \
    && [ "$KR_HOUR" -ge "$ACCESS_HOUR_START" ] && [ "$KR_HOUR" -lt "$ACCESS_HOUR_END" ]
}

ipsec_established() {
  ipsec status "$VPN_CONN" 2>/dev/null | grep -q ESTABLISHED
}

ppp_up() {
  ip -4 addr show "$PPP_IFACE" >/dev/null 2>&1
}

route_present() {
  ip route show "$VPN_ROUTE" 2>/dev/null | grep -q "dev $PPP_IFACE"
}

# Здоровье туннеля = есть ppp0 И маршрут в подсеть заказчика через него.
tunnel_healthy() {
  ppp_up && route_present
}

ppp_addr() {
  ip -4 -o addr show "$PPP_IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1
}

# Поднимает IPsec-слой ТОЛЬКО если он ещё не установлен. !=0 при неудаче.
ensure_ipsec() {
  if ipsec_established; then
    vlog "IPsec ($VPN_CONN) уже ESTABLISHED — пропускаю"
    return 0
  fi
  vlog "IPsec up ($VPN_CONN)…"
  ipsec up "$VPN_CONN"
}

# Дёргает L2TP-сессию (ОДНА попытка PPP-логина!) и ждёт появления ppp0.
# Вызывать только когда ppp0 отсутствует — иначе плодит лишние логины.
bring_up_l2tp() {
  if [ ! -S "$XL2TPD_CONTROL" ] && [ ! -p "$XL2TPD_CONTROL" ]; then
    verr "управляющий сокет xl2tpd не найден: $XL2TPD_CONTROL (xl2tpd запущен?)"
    return 1
  fi
  vlog "L2TP-сессия ($L2TP_LAC)…"
  echo "c $L2TP_LAC" > "$XL2TPD_CONTROL"

  vlog "жду $PPP_IFACE (до ${PPP_WAIT_SECS}с)…"
  local i
  for i in $(seq 1 "$PPP_WAIT_SECS"); do
    ppp_up && return 0
    sleep 1
  done
  verr "$PPP_IFACE не появился за ${PPP_WAIT_SECS}с"
  return 1
}

ensure_route() {
  ip route replace "$VPN_ROUTE" dev "$PPP_IFACE"
  vlog "маршрут $VPN_ROUTE → $PPP_IFACE"
}

# Синхронизирует SIP_LOCAL_IP с адресом ppp0 и пересоздаёт контейнер при изменении.
# Делегирует существующему sync-sip-local-ip.sh (идемпотентен).
sync_sip_ip() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  PPP_IFACE="$PPP_IFACE" AI_ROBOT_DIR="$AI_ROBOT_DIR" "$here/sync-sip-local-ip.sh"
}
