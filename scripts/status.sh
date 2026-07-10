#!/usr/bin/env bash
# Быстрый снимок состояния коллегии (очередь, агенты, остаток бюджета).
set -euo pipefail
URL="${ORCH_URL:-http://127.0.0.1:8001}"
curl -fsS "$URL/v1/status" | python3 -m json.tool
