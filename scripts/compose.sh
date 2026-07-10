#!/usr/bin/env bash
# Обёртка docker compose, передающая единственный файл кредов как источник
# переменных. Используй вместо голого `docker compose`:
#   ./scripts/compose.sh up -d
#   ./scripts/compose.sh logs -f orchestrator
#   ./scripts/compose.sh down
set -euo pipefail
cd "$(dirname "$0")/.."
CREDS="secrets/credentials.env"
if [ ! -f "$CREDS" ]; then
  echo "нет $CREDS — скопируй secrets/credentials.env.example и заполни (chmod 600)" >&2
  exit 1
fi
exec docker compose --env-file "$CREDS" "$@"
