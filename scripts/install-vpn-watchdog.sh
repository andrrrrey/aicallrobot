#!/usr/bin/env bash
#
# Устанавливает systemd timer + service для vpn-watchdog: ставит юниты,
# делает скрипты исполняемыми и запускает таймер. Идемпотентен.
#
# Использование (от root):
#   sudo ./scripts/install-vpn-watchdog.sh
#
# Переопределить настройки watchdog (MAX_FAILURES и т.д.) можно, создав файл
#   /etc/default/ai-robot-vpn   — например:
#     MAX_FAILURES=2
#     MIN_ATTEMPT_INTERVAL_SECS=300
#     AI_ROBOT_DIR=/opt/ai-robot

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" != "0" ]; then
  echo "[install] запускать от root (sudo)" >&2
  exit 1
fi

chmod +x "$SCRIPT_DIR"/vpn-watchdog.sh "$SCRIPT_DIR"/res-vpn-up.sh \
         "$SCRIPT_DIR"/sync-sip-local-ip.sh 2>/dev/null || true

cp "$SCRIPT_DIR/vpn-watchdog.service" /etc/systemd/system/vpn-watchdog.service
cp "$SCRIPT_DIR/vpn-watchdog.timer"   /etc/systemd/system/vpn-watchdog.timer

systemctl daemon-reload
systemctl enable --now vpn-watchdog.timer

echo "[install] готово."
systemctl status vpn-watchdog.timer --no-pager || true
echo
echo "Логи watchdog:   journalctl -u vpn-watchdog.service -f"
echo "Разовый прогон:  systemctl start vpn-watchdog.service"
echo "Состояние:       systemctl list-timers vpn-watchdog.timer"
