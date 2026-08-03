#!/usr/bin/env bash
#
# Поднимает VPN-туннель L2TP/IPsec до сети заказчика (Asterisk 192.168.0.110),
# добавляет split-tunnel маршрут и синхронизирует SIP_LOCAL_IP робота с адресом
# ppp0 (см. scripts/sync-sip-local-ip.sh).
#
# Предполагает, что strongSwan/xl2tpd уже настроены по docs/DEVOPS-VPN-SETUP.md
# (разделы 4.1–4.5): conn `res-l2tp` в /etc/ipsec.conf, lac `res` в xl2tpd.conf.
#
# ⚠️ ДВА КРИТИЧНЫХ ОГРАНИЧЕНИЯ (docs/DEVOPS-VPN-SETUP.md, раздел 3):
#   1. 4 неудачные попытки логина → заказчик блокирует ВСЮ /24 нашего IP.
#      Поэтому скрипт делает РОВНО ОДНУ попытку и НЕ повторяет при ошибке.
#   2. Туннель поднимается только ПН–ПТ 08:00–22:00 GMT+7 (Красноярск).
#      Вне окна ppp0 не появится — это не повод перезапускать в цикле.
#
# Использование (от root):
#   sudo ./scripts/res-vpn-up.sh
#
# Переопределяемые параметры (env):
#   VPN_CONN=res-l2tp   L2TP_LAC=res   VPN_ROUTE=192.168.0.0/24   PPP_IFACE=ppp0
#   AI_ROBOT_DIR=/opt/ai-robot

set -euo pipefail

CONN="${VPN_CONN:-res-l2tp}"
L2TP_LAC="${L2TP_LAC:-res}"
ROUTE="${VPN_ROUTE:-192.168.0.0/24}"
IFACE="${PPP_IFACE:-ppp0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[vpn-up] $*"; }

if [ "$(id -u)" != "0" ]; then
  echo "[vpn-up] запускать от root (sudo)" >&2
  exit 1
fi

# Уже поднят? — тогда просто досинхронизируем адрес и выходим.
if ip -4 addr show "$IFACE" >/dev/null 2>&1; then
  log "$IFACE уже поднят — пропускаю установку туннеля, синхронизирую адрес"
  ip route replace "$ROUTE" dev "$IFACE"
  PPP_IFACE="$IFACE" "$SCRIPT_DIR/sync-sip-local-ip.sh"
  exit 0
fi

log "IPsec up ($CONN) — одна попытка, без ретраев (риск блокировки /24)…"
if ! ipsec up "$CONN"; then
  echo "[vpn-up] 'ipsec up $CONN' не удался. НЕ повторяю автоматически." >&2
  echo "[vpn-up] Сверьте PSK/логин/пароль и окно доступа. Диагностика:" >&2
  echo "[vpn-up]   sudo ipsec statusall; sudo journalctl -u strongswan-starter -n 50" >&2
  exit 1
fi

log "L2TP-сессия ($L2TP_LAC)…"
echo "c $L2TP_LAC" > /var/run/xl2tpd/l2tp-control

log "жду появления $IFACE (до 20с)…"
for _ in $(seq 1 20); do
  ip -4 addr show "$IFACE" >/dev/null 2>&1 && break
  sleep 1
done
if ! ip -4 addr show "$IFACE" >/dev/null 2>&1; then
  echo "[vpn-up] $IFACE не появился. Частые причины: неверный логин/пароль (pppd)," >&2
  echo "[vpn-up] require-mschap-v2, либо мы вне окна ПН–ПТ 08:00–22:00 GMT+7." >&2
  echo "[vpn-up] Смотрите: sudo tail -n 80 /var/log/syslog | grep -Ei 'pppd|l2tp|charon'" >&2
  exit 1
fi

log "маршрут $ROUTE → $IFACE"
ip route replace "$ROUTE" dev "$IFACE"
ip -4 addr show "$IFACE" | grep inet || true

# Синхронизируем SIP_LOCAL_IP и пересоздаём контейнер ai-robot.
PPP_IFACE="$IFACE" "$SCRIPT_DIR/sync-sip-local-ip.sh"
log "готово. Туннель поднят, SIP_LOCAL_IP синхронизирован."
