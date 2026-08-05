#!/usr/bin/env bash
#
# Поднимает VPN-туннель L2TP/IPsec до сети заказчика (Asterisk 192.168.0.110):
# при необходимости поднимает IPsec, затем L2TP-сессию (создаётся ppp0),
# добавляет split-tunnel маршрут и синхронизирует SIP_LOCAL_IP робота с ppp0.
#
# Это РАЗОВЫЙ ручной инструмент: делает ровно одну попытку логина и не циклит.
# Для автоматического поддержания туннеля используйте scripts/vpn-watchdog.sh.
#
# Предполагает, что strongSwan/xl2tpd уже настроены по docs/DEVOPS-VPN-SETUP.md
# (разделы 4.1–4.5): conn `res-l2tp` в /etc/ipsec.conf, lac `res` в xl2tpd.conf.
#
# ⚠️ ДВА КРИТИЧНЫХ ОГРАНИЧЕНИЯ (docs/DEVOPS-VPN-SETUP.md, раздел 3):
#   1. 4 неудачные попытки VPN-логина → заказчик блокирует ВСЮ /24 нашего IP.
#      Поэтому — ровно одна попытка, без ретраев.
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VPN_LOG_TAG="vpn-up"
# shellcheck source=vpn-lib.sh
source "$SCRIPT_DIR/vpn-lib.sh"

require_root

# Уже здоров? — только досинхронизируем адрес и выходим.
if tunnel_healthy; then
  vlog "$PPP_IFACE поднят и маршрут на месте — синхронизирую SIP_LOCAL_IP"
  sync_sip_ip
  exit 0
fi

# ppp0 есть, но пропал маршрут — не трогаем L2TP-логин, только маршрут.
if ppp_up; then
  vlog "$PPP_IFACE есть, но маршрут $VPN_ROUTE отсутствует — добавляю маршрут"
  ensure_route
  sync_sip_ip
  exit 0
fi

# ppp0 отсутствует — нужен полный подъём. Строго в окне доступа.
if ! in_access_window; then
  verr "вне окна доступа ПН–ПТ 08:00–22:00 GMT+7 (Красноярск: день $KR_DOW, час $KR_HOUR)."
  verr "туннель в это время не поднимется — не пытаюсь (риск блокировки /24)."
  exit 1
fi

if ! ensure_ipsec; then
  verr "'ipsec up $VPN_CONN' не удался. НЕ повторяю автоматически."
  verr "Диагностика: sudo ipsec statusall; sudo journalctl -u strongswan* -n 50"
  exit 1
fi

if ! bring_up_l2tp; then
  verr "L2TP/$PPP_IFACE не поднялся. Частые причины: неверный PPP-логин/пароль,"
  verr "require-mschap-v2, не запущен xl2tpd, либо мы вне окна доступа."
  verr "Смотрите: sudo tail -n 80 /var/log/syslog | grep -Ei 'pppd|l2tp|xl2tpd|charon'"
  exit 1
fi

ensure_route
ip -4 addr show "$PPP_IFACE" | grep inet || true
sync_sip_ip
vlog "готово. Туннель поднят, SIP_LOCAL_IP синхронизирован."
