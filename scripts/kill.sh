#!/usr/bin/env bash
# Kill-switch на уровне ХОСТА: docker stop контейнеров агентов. Агент не может это
# отменить из своего контейнера — это защищённая остановка (guard).
#   ./scripts/kill.sh            # стоп всех агентов
#   ./scripts/kill.sh agent-2    # стоп одного
#   ./scripts/kill.sh resume     # снова запустить всех агентов
set -euo pipefail
cd "$(dirname "$0")/.."
AGENTS="agent-1 agent-2 agent-3"
case "${1:-all}" in
  resume)  exec ./scripts/compose.sh start $AGENTS ;;
  all)     exec ./scripts/compose.sh stop $AGENTS ;;
  agent-*) exec ./scripts/compose.sh stop "$1" ;;
  *) echo "usage: $0 [all|agent-N|resume]" >&2; exit 2 ;;
esac
