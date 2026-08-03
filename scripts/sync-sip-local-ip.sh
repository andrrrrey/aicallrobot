#!/usr/bin/env bash
#
# Синхронизирует SIP_LOCAL_IP в .env с текущим адресом VPN-интерфейса (ppp0)
# и пересоздаёт контейнер ai-robot, чтобы новое значение применилось.
#
# Зачем: L2TP выдаёт адрес ppp0 динамически (сегодня .232, завтра .217), а
# pjsua биндит SIP-сокет именно на SIP_LOCAL_IP. Если адрес разъедется —
# bind() падает с "Cannot assign requested address", SIP-агент не стартует и
# /api/v1/test-call отдаёт 503. Этот скрипт держит .env в согласии с ppp0.
#
# ВАЖНО: `docker compose restart` НЕ перечитывает .env (env зашивается при
# создании контейнера), поэтому здесь используется `up -d` (пересоздание).
#
# Использование:
#   ./scripts/sync-sip-local-ip.sh                # взять адрес из ppp0
#   PPP_IFACE=ppp1 ./scripts/sync-sip-local-ip.sh # другой интерфейс
#   AI_ROBOT_DIR=/opt/ai-robot ./scripts/sync-sip-local-ip.sh
#
# Как ppp/ip-up.d-хук pppd вызывает скрипт с именем интерфейса в $1 — он тоже
# учитывается автоматически.

set -euo pipefail

PROJECT_DIR="${AI_ROBOT_DIR:-/opt/ai-robot}"
ENV_FILE="${AI_ROBOT_ENV:-$PROJECT_DIR/.env}"
# Имя интерфейса: PPP_IFACE > аргумент $1 (ip-up.d) > ppp0
IFACE="${PPP_IFACE:-${1:-ppp0}}"

log() { echo "[sync-sip] $*"; }

if [ ! -f "$ENV_FILE" ]; then
  echo "[sync-sip] .env не найден: $ENV_FILE (задайте AI_ROBOT_DIR/AI_ROBOT_ENV)" >&2
  exit 1
fi

ip_addr="$(ip -4 -o addr show "$IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1)"
if [ -z "${ip_addr:-}" ]; then
  echo "[sync-sip] интерфейс $IFACE не поднят — VPN не установлен, синхронизировать нечего" >&2
  exit 1
fi

current="$(grep -E '^SIP_LOCAL_IP=' "$ENV_FILE" | head -n1 | cut -d= -f2- || true)"
if [ "$current" = "$ip_addr" ]; then
  log "SIP_LOCAL_IP уже = $ip_addr — контейнер не трогаем"
  exit 0
fi

if grep -qE '^SIP_LOCAL_IP=' "$ENV_FILE"; then
  sed -i "s/^SIP_LOCAL_IP=.*/SIP_LOCAL_IP=${ip_addr}/" "$ENV_FILE"
else
  printf '\nSIP_LOCAL_IP=%s\n' "$ip_addr" >> "$ENV_FILE"
fi
log "SIP_LOCAL_IP: '${current:-<нет>}' → '$ip_addr'"

# Пересоздаём контейнер, чтобы новый SIP_LOCAL_IP попал в окружение.
# restart не годится — он не перечитывает .env.
cd "$PROJECT_DIR"
docker compose up -d ai-robot
log "контейнер ai-robot пересоздан с SIP_LOCAL_IP=$ip_addr"
log "проверьте регистрацию: docker compose logs -f ai-robot | grep -iE 'pjsua|зарегистр'"
