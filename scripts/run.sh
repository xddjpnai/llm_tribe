#!/usr/bin/env bash
# ОДНА КНОПКА: проверяет docker, секреты, собирает и поднимает стек, ждёт healthy.
# Идемпотентен — безопасно запускать повторно (up -d --build ничего не ломает).
#   ./scripts/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."

say()  { printf '\n== %s\n' "$*"; }
fail() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

# ---------- 1. docker + compose ----------
if ! command -v docker >/dev/null 2>&1; then
  say "docker не найден — ставлю через get.docker.com (нужен Linux и права root/sudo)"
  command -v curl >/dev/null 2>&1 || fail "нет curl — установи: apt-get install -y curl"
  curl -fsSL https://get.docker.com | sh || fail "автоустановка docker не удалась — https://docs.docker.com/engine/install/"
fi
if ! docker info >/dev/null 2>&1; then
  # демон не запущен или нет прав у пользователя
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl enable --now docker >/dev/null 2>&1 || true
  fi
  docker info >/dev/null 2>&1 || fail \
    "docker-демон недоступен. Либо запусти демон, либо дай себе права: sudo usermod -aG docker \$USER && перелогинься (или запусти этот скрипт через sudo)"
fi
docker compose version >/dev/null 2>&1 || fail "docker compose v2 не найден — обнови Docker (пакет docker-compose-plugin)"

# ---------- 2. секреты ----------
CREDS="secrets/credentials.env"
if [ ! -f "$CREDS" ]; then
  cp secrets/credentials.env.example "$CREDS"
  chmod 600 "$CREDS"
  say "создал $CREDS из примера"
  echo "Заполни в нём все поля и запусти скрипт снова. Где взять значения:"
  echo "  TELEGRAM_BOT_TOKEN — у @BotFather в Telegram (/newbot)"
  echo "  TELEGRAM_OWNER_IDS — твой numeric id у @userinfobot"
  echo "  ZAI_API_KEY        — https://z.ai (API keys)"
  echo "  DEEPSEEK_API_KEY   — https://platform.deepseek.com"
  echo "  XAI_API_KEY        — https://console.x.ai"
  exit 1
fi
chmod 600 "$CREDS" || true

hint() {
  case "$1" in
    ZAI_API_KEY)        echo "ключ Z.ai: https://z.ai" ;;
    DEEPSEEK_API_KEY)   echo "ключ DeepSeek: https://platform.deepseek.com" ;;
    XAI_API_KEY)        echo "ключ xAI: https://console.x.ai" ;;
    TELEGRAM_BOT_TOKEN) echo "токен бота: @BotFather в Telegram, команда /newbot" ;;
    TELEGRAM_OWNER_IDS) echo "твой numeric Telegram id: напиши @userinfobot" ;;
  esac
}
missing=0
for k in ZAI_API_KEY DEEPSEEK_API_KEY XAI_API_KEY \
         TELEGRAM_BOT_TOKEN TELEGRAM_OWNER_IDS; do
  v="$(grep -E "^${k}=" "$CREDS" | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
  if [ -z "$v" ]; then
    [ "$missing" = 0 ] && say "в $CREDS не заполнены обязательные поля:"
    echo "  $k — $(hint "$k")"
    missing=1
  fi
done
[ "$missing" = 0 ] || { echo; echo "Заполни их и запусти ./scripts/run.sh снова."; exit 1; }

# ---------- 3. сборка и запуск ----------
say "собираю образы и поднимаю стек (первый раз занимает несколько минут)"
./scripts/compose.sh up -d --build

# ---------- 4. ждём healthy ----------
say "жду, пока сервисы станут healthy (до 300s)"
DEADLINE=$(( $(date +%s) + 300 ))
for s in redis budget-guard selfmod-api sage; do
  while :; do
    cid="$(./scripts/compose.sh ps -q "$s" || true)"
    st="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo starting)"
    if [ "$st" = "healthy" ]; then
      echo "  $s: healthy"
      break
    fi
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
      echo "  $s: НЕ стал healthy (сейчас: $st)" >&2
      ./scripts/compose.sh ps
      fail "смотри логи: ./scripts/compose.sh logs $s"
    fi
    sleep 3
  done
done

# ---------- 5. итог ----------
say "стек поднят"
./scripts/compose.sh ps
cat <<'EOF'

Дальше:
  ./scripts/compose.sh logs -f agent-1     # смотреть, что делает агент
  ./scripts/status.sh                      # контейнеры + накопленный LLM-расход
  ./scripts/kill.sh [all|agent-N|resume]   # аварийная остановка агентов (host-level)

Агенты начали стартовые задачи: журнал, Telegram-бот, приём задач. Открой своего
бота в Telegram и нажми Start — иначе он не сможет написать тебе первым.
EOF
