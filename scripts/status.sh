#!/usr/bin/env bash
# Состояние контейнеров + расход бюджета.
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/compose.sh ps
echo "--- budget ---"
docker exec "$(./scripts/compose.sh ps -q budget-guard)" \
  python3 -c "import urllib.request,json; print(json.load(urllib.request.urlopen('http://localhost:8080/v1/budget')))" \
  2>/dev/null || echo "budget-guard недоступен"
