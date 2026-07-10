#!/usr/bin/env bash
# Общий kill-switch (guard #8): мгновенно поставить на паузу/остановить агента
# или всех сразу. Обязательный ручной override при аномалии/перерасходе.
#
#   ./scripts/kill.sh stop            # остановить всех
#   ./scripts/kill.sh pause           # пауза всех
#   ./scripts/kill.sh resume          # снять паузу
#   ./scripts/kill.sh stop agent-2    # остановить одного агента
#
# Оркестратор слушает 127.0.0.1:8001 (проброшен только на localhost VPS).
set -euo pipefail

ACTION="${1:-stop}"
TARGET="${2:-all}"
URL="${ORCH_URL:-http://127.0.0.1:8001}"

case "$ACTION" in
  stop|pause|resume) ;;
  *) echo "usage: $0 {stop|pause|resume} [all|agent-id]" >&2; exit 2 ;;
esac

curl -fsS -X POST "$URL/v1/kill" \
  -H 'Content-Type: application/json' \
  -d "{\"target\":\"$TARGET\",\"action\":\"$ACTION\"}"
echo
