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
#   • при отказе аутентификации (MS-CHAP E=691) ждёт AUTH_BACKOFF_SECS, а после
#     MAX_AUTH_FAILURES таких отказов подряд включает «предохранитель» (breaker)
#     и прекращает автопопытки до ручного сброса — только 691 тратит лимит;
#   • сбои БЕЗ 691 (сервер заказчика не отвечает по IKE/L2TP, таймаут) лимит
#     логинов не тратят: после MAX_FAILURES таких сбоев подряд watchdog не
#     останавливается, а пробует раз в NET_BACKOFF_SECS, пока сервер не оживёт.
#     Исключение — если логи pppd недоступны (691 не отличить): тогда после
#     MAX_FAILURES неудач подряд включается предохранитель, как раньше;
#   • вне окна ПН–ПТ 08:10–22:00 GMT+7 попыток не делает (это не «неудача»);
#   • при успешном подъёме сбрасывает счётчик неудач и предохранитель.
#
# Уведомления в Telegram (необязательно, TG_BOT_TOKEN + TG_CHAT_ID в
# /etc/default/ai-robot-vpn): туннель лежит в окне доступа дольше
# NOTIFY_DOWN_AFTER_SECS, включился предохранитель, туннель восстановлен.
#
# Разовый прогон (от root):  sudo ./scripts/vpn-watchdog.sh
#
# Env: MAX_FAILURES(=3)  MIN_ATTEMPT_INTERVAL_SECS(=300)
#      MAX_AUTH_FAILURES(=2)  AUTH_BACKOFF_SECS(=1800)  NET_BACKOFF_SECS(=1800)
#      NOTIFY_DOWN_AFTER_SECS(=900)  TG_BOT_TOKEN  TG_CHAT_ID
#      STATE_DIR(=/var/lib/ai-robot-vpn)  + все переменные из vpn-lib.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VPN_LOG_TAG="vpn-watchdog"
# shellcheck source=vpn-lib.sh
source "$SCRIPT_DIR/vpn-lib.sh"

MAX_FAILURES="${MAX_FAILURES:-3}"
MIN_ATTEMPT_INTERVAL_SECS="${MIN_ATTEMPT_INTERVAL_SECS:-300}"
MAX_AUTH_FAILURES="${MAX_AUTH_FAILURES:-2}"
AUTH_BACKOFF_SECS="${AUTH_BACKOFF_SECS:-1800}"
NET_BACKOFF_SECS="${NET_BACKOFF_SECS:-1800}"
NOTIFY_DOWN_AFTER_SECS="${NOTIFY_DOWN_AFTER_SECS:-900}"
SIP_RESTART_INTERVAL_SECS="${SIP_RESTART_INTERVAL_SECS:-300}"
STATE_DIR="${STATE_DIR:-/var/lib/ai-robot-vpn}"
FAIL_FILE="$STATE_DIR/consecutive_failures"
AUTH_FAIL_FILE="$STATE_DIR/consecutive_auth_failures"
LAST_ATTEMPT_FILE="$STATE_DIR/last_attempt_epoch"
LAST_RESTART_FILE="$STATE_DIR/last_container_restart"
BREAKER_FILE="$STATE_DIR/breaker"
DOWN_SINCE_FILE="$STATE_DIR/down_since_epoch"
DOWN_NOTIFIED_FILE="$STATE_DIR/down_notified"

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
  if [ -f "$DOWN_NOTIFIED_FILE" ]; then
    local since mins
    since="$(_read_int "$DOWN_SINCE_FILE")"
    mins=$(( ($(date -u +%s) - since) / 60 ))
    tg_notify "✅ VPN-туннель к АТС восстановлен (простой в окне доступа ~${mins} мин)."
  fi
  rm -f "$FAIL_FILE" "$AUTH_FAIL_FILE" "$BREAKER_FILE" \
        "$DOWN_SINCE_FILE" "$DOWN_NOTIFIED_FILE" 2>/dev/null || true
}

trip_breaker() {
  {
    echo "Предохранитель включён $(date -u +%FT%TZ): $1."
    echo "Автопопытки остановлены во избежание блокировки /24 заказчиком."
    echo "Проверьте PPP-логин/пароль у заказчика, затем сбросьте:"
    echo "  rm $BREAKER_FILE $FAIL_FILE $AUTH_FAIL_FILE"
  } > "$BREAKER_FILE"
  verr "ПРЕДОХРАНИТЕЛЬ ВКЛЮЧЁН ($1) — автопопытки остановлены до ручного сброса ($BREAKER_FILE)"
  tg_notify "⛔ VPN: предохранитель включён — $1. Автопопытки остановлены, нужен ручной сброс после проверки у заказчика."
}

# $1 (необязательно) — эпоха начала попытки: по ней ищем в логах pppd отказ
# аутентификации (691), чтобы отличить его от прочих сбоев.
on_failure() {
  local n auth
  n=$(( $(_read_int "$FAIL_FILE") + 1 ))
  echo "$n" > "$FAIL_FILE"
  # Счётчик 691 сбрасывается ТОЛЬКО успешным подъёмом: сбои другого рода между
  # отказами пароля его не обнуляют, иначе чередование «691, таймаут, 691…»
  # тратило бы лимит заказчика без предохранителя.
  auth="$(_read_int "$AUTH_FAIL_FILE")"
  if [ -n "${1:-}" ] && ppp_auth_failed_since "$1"; then
    auth=$(( auth + 1 ))
    echo "$auth" > "$AUTH_FAIL_FILE"
    verr "PPP-логин отклонён сервером (E=691 bad username or password), с последнего успеха: $auth/$MAX_AUTH_FAILURES; следующая попытка не раньше чем через ${AUTH_BACKOFF_SECS}с"
  fi
  verr "попытка восстановления не удалась (подряд неудач: $n)"
  if [ "$auth" -ge "$MAX_AUTH_FAILURES" ]; then
    trip_breaker "$auth отказов аутентификации (E=691) с последнего успешного подключения"
  elif [ "$n" -ge "$MAX_FAILURES" ] && [ "$auth" -eq 0 ]; then
    if ppp_logs_readable; then
      verr "сервер заказчика не отвечает (отказов пароля нет, лимит логинов не тратится) — дальше пробую раз в ${NET_BACKOFF_SECS}с"
    else
      trip_breaker "$n неудачных попыток подряд, а логи pppd недоступны (не отличить отказ пароля)"
    fi
  fi
}

# --- 1. Туннель здоров? Штатный путь. ---
if tunnel_healthy; then
  # Сменился ли адрес ppp0 (тогда sync ниже сам пересоздаст контейнер)?
  ip_changed=0
  [ "$(env_sip_ip)" = "$(ppp_addr)" ] || ip_changed=1
  # Держим SIP_LOCAL_IP в согласии с ppp0 (адрес мог смениться после реконнекта).
  sync_sip_ip || verr "sync SIP_LOCAL_IP не удался (не критично)"
  on_success

  # Туннель жив, но SIP-агент мог умереть при старте (bind при отсутствовавшем
  # ppp0). Если адрес НЕ менялся (sync контейнер не трогал) и агент не готов —
  # перезапускаем контейнер, но не чаще SIP_RESTART_INTERVAL_SECS (без циклов).
  if [ "$ip_changed" = "0" ] && ! sip_ready; then
    now="$(date -u +%s)"
    lastr="$(_read_int "$LAST_RESTART_FILE")"
    if [ $(( now - lastr )) -ge "$SIP_RESTART_INTERVAL_SECS" ]; then
      verr "туннель жив, но SIP не зарегистрирован — перезапускаю контейнер"
      echo "$now" > "$LAST_RESTART_FILE"
      restart_container || verr "restart ai-robot не удался"
    else
      vlog "SIP не готов, но контейнер перезапускали $(( now - lastr ))с назад — жду"
    fi
  fi
  exit 0
fi

verr "туннель НЕ здоров: ppp0=$(ppp_up && echo up || echo down), маршрут=$(route_present && echo ok || echo missing)"

# --- 2. Предохранитель включён? ---
if [ -f "$BREAKER_FILE" ]; then
  verr "предохранитель активен — попыток не делаю. Сброс: rm $BREAKER_FILE $FAIL_FILE $AUTH_FAIL_FILE"
  exit 3
fi

# --- 3. В окне доступа? Вне окна попытки бессмысленны и не считаются неудачей. ---
if ! in_access_window; then
  # Вечером заказчик сам рвёт туннель — это не простой, отсчёт не ведём.
  rm -f "$DOWN_SINCE_FILE" "$DOWN_NOTIFIED_FILE" 2>/dev/null || true
  vlog "вне окна доступа (Красноярск: день $KR_DOW, час $KR_HOUR) — жду окна"
  exit 0
fi

# Простой в окне доступа: один раз уведомляем, если он затянулся.
now="$(date -u +%s)"
[ -f "$DOWN_SINCE_FILE" ] || echo "$now" > "$DOWN_SINCE_FILE"
down_since="$(_read_int "$DOWN_SINCE_FILE")"
if [ ! -f "$DOWN_NOTIFIED_FILE" ] && [ $(( now - down_since )) -ge "$NOTIFY_DOWN_AFTER_SECS" ]; then
  touch "$DOWN_NOTIFIED_FILE"
  tg_notify "⚠️ VPN-туннель к АТС лежит уже $(( (now - down_since) / 60 )) мин — робот не может звонить. Watchdog продолжает попытки."
fi

# --- 4. Не слишком часто? После 691 или серии сбоев — длинная пауза. ---
last="$(_read_int "$LAST_ATTEMPT_FILE")"
min_interval="$MIN_ATTEMPT_INTERVAL_SECS"
if [ "$(_read_int "$AUTH_FAIL_FILE")" -gt 0 ]; then
  min_interval="$AUTH_BACKOFF_SECS"
elif [ "$(_read_int "$FAIL_FILE")" -ge "$MAX_FAILURES" ]; then
  min_interval="$NET_BACKOFF_SECS"
fi
if [ "$last" -gt 0 ] && [ $(( now - last )) -lt "$min_interval" ]; then
  vlog "с прошлой попытки прошло $(( now - last ))с (<${min_interval}) — жду"
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
# Каждая ветка завершается одной попыткой `c res` (= один PPP-логин); неудачных
# логинов (691) до предохранителя будет не больше MAX_AUTH_FAILURES.
fails="$(_read_int "$FAIL_FILE")"
if [ "$fails" -ge 2 ]; then
  # 3-я и последующие попытки: полный бок стека — пересоздать IPsec + перезапустить xl2tpd.
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
  on_failure "$now"
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
