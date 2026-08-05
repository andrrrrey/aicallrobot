#!/usr/bin/env bash
#
# Watchdog VPN-туннеля к АТС заказчика. Периодически проверяет здоровье туннеля
# (ppp0 + маршрут в 192.168.0.0/24) и — В ОКНЕ ДОСТУПА — аккуратно восстанавливает
# его, синхронизируя SIP_LOCAL_IP робота. Ставится на systemd timer (см.
# scripts/vpn-watchdog.timer) или cron.
#
# Зачем: strongSwan может держать IPsec ESTABLISHED, а L2TP/ppp0 при этом
# отвалиться — тогда `ipsec statusall` «зелёный», но робот не дозванивается
# ("Network is unreachable"). Watchdog смотрит именно на ppp0+маршрут.
#
# ⚠️ БЕЗОПАСНОСТЬ (критично!): у заказчика 4 неудачные попытки VPN-логина
# блокируют всю /24 нашего IP. Поэтому watchdog:
#   • не делает попыток чаще MIN_ATTEMPT_INTERVAL_SECS;
#   • после MAX_FAILURES неудачных попыток ПОДРЯД включает «предохранитель»
#     (breaker) и прекращает автопопытки до ручного сброса;
#   • вне окна ПН–ПТ 08:00–22:00 GMT+7 попыток не делает (это не «неудача»);
#   • при успешном подъёме сбрасывает счётчик неудач и предохранитель.
#
# Разовый прогон (от root):  sudo ./scripts/vpn-watchdog.sh
#
# Env: MAX_FAILURES(=3)  MIN_ATTEMPT_INTERVAL_SECS(=300)
#      STATE_DIR(=/var/lib/ai-robot-vpn)  + все переменные из vpn-lib.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VPN_LOG_TAG="vpn-watchdog"
# shellcheck source=vpn-lib.sh
source "$SCRIPT_DIR/vpn-lib.sh"

MAX_FAILURES="${MAX_FAILURES:-3}"
MIN_ATTEMPT_INTERVAL_SECS="${MIN_ATTEMPT_INTERVAL_SECS:-300}"
STATE_DIR="${STATE_DIR:-/var/lib/ai-robot-vpn}"
FAIL_FILE="$STATE_DIR/consecutive_failures"
LAST_ATTEMPT_FILE="$STATE_DIR/last_attempt_epoch"
BREAKER_FILE="$STATE_DIR/breaker"

require_root
mkdir -p "$STATE_DIR"

_read_int() {
  local v
  v="$(cat "$1" 2>/dev/null || true)"
  case "$v" in
    ''|*[!0-9]*) echo 0 ;;
    *) echo "$v" ;;
  esac
}

on_success() {
  rm -f "$FAIL_FILE" "$BREAKER_FILE" 2>/dev/null || true
}

on_failure() {
  local n
  n=$(( $(_read_int "$FAIL_FILE") + 1 ))
  echo "$n" > "$FAIL_FILE"
  verr "попытка восстановления не удалась (подряд неудач: $n/$MAX_FAILURES)"
  if [ "$n" -ge "$MAX_FAILURES" ]; then
    {
      echo "Предохранитель включён $(date -u +%FT%TZ): $n неудачных попыток подряд."
      echo "Автопопытки остановлены во избежание блокировки /24 заказчиком."
      echo "Проверьте PPP-логин/пароль, xl2tpd и окно доступа, затем сбросьте:"
      echo "  rm $BREAKER_FILE $FAIL_FILE"
    } > "$BREAKER_FILE"
    verr "ПРЕДОХРАНИТЕЛЬ ВКЛЮЧЁН — автопопытки остановлены до ручного сброса ($BREAKER_FILE)"
  fi
}

# --- 1. Туннель здоров? Штатный путь. ---
if tunnel_healthy; then
  # Держим SIP_LOCAL_IP в согласии с ppp0 (адрес мог смениться после реконнекта).
  sync_sip_ip || verr "sync SIP_LOCAL_IP не удался (не критично)"
  on_success
  exit 0
fi

verr "туннель НЕ здоров: ppp0=$(ppp_up && echo up || echo down), маршрут=$(route_present && echo ok || echo missing)"

# --- 2. Предохранитель включён? ---
if [ -f "$BREAKER_FILE" ]; then
  verr "предохранитель активен — попыток не делаю. Сброс: rm $BREAKER_FILE $FAIL_FILE"
  exit 3
fi

# --- 3. В окне доступа? Вне окна попытки бессмысленны и не считаются неудачей. ---
if ! in_access_window; then
  vlog "вне окна доступа (Красноярск: день $KR_DOW, час $KR_HOUR) — жду окна"
  exit 0
fi

# --- 4. Не слишком часто? ---
now="$(date -u +%s)"
last="$(_read_int "$LAST_ATTEMPT_FILE")"
if [ "$last" -gt 0 ] && [ $(( now - last )) -lt "$MIN_ATTEMPT_INTERVAL_SECS" ]; then
  vlog "с прошлой попытки прошло $(( now - last ))с (<${MIN_ATTEMPT_INTERVAL_SECS}) — жду"
  exit 0
fi
echo "$now" > "$LAST_ATTEMPT_FILE"

# --- 5. Восстановление ---
vlog "восстанавливаю туннель…"

# ppp0 есть, но нет маршрута — это НЕ логин, чиним маршрут без переподключения.
if ppp_up; then
  if ensure_route && tunnel_healthy; then
    vlog "маршрут восстановлен без переподключения"
    sync_sip_ip || verr "sync SIP_LOCAL_IP не удался (не критично)"
    on_success
    exit 0
  fi
  on_failure
  exit 1
fi

# ppp0 отсутствует — поднимаем L2TP с ЭСКАЛАЦИЕЙ по числу уже случившихся неудач.
# Каждая ветка завершается одной попыткой `c res` (= один PPP-логин), поэтому за
# все попытки до предохранителя логинов будет не больше MAX_FAILURES.
fails="$(_read_int "$FAIL_FILE")"
if [ "$fails" -ge 2 ]; then
  # 3-я попытка: полный бок стека — пересоздать IPsec + перезапустить xl2tpd.
  vlog "эскалация: bounce IPsec + перезапуск xl2tpd (неудач: $fails)"
  if ! bounce_ipsec; then
    verr "IPsec bounce не удался"
    on_failure
    exit 1
  fi
  restart_l2tp_stack
elif [ "$fails" -ge 1 ]; then
  # 2-я попытка: сбросить залипшую L2TP-сессию.
  vlog "эскалация: перезапуск xl2tpd (неудач: $fails)"
  ensure_ipsec || { verr "IPsec не поднялся"; on_failure; exit 1; }
  restart_l2tp_stack
else
  # 1-я попытка: просто убедиться, что IPsec поднят, и дёрнуть L2TP.
  ensure_ipsec || { verr "IPsec не поднялся"; on_failure; exit 1; }
fi

if ! bring_up_l2tp; then
  on_failure
  exit 1
fi

ensure_route
sync_sip_ip || verr "sync SIP_LOCAL_IP не удался (не критично)"

if tunnel_healthy; then
  vlog "туннель восстановлен"
  on_success
  exit 0
fi

on_failure
exit 1
