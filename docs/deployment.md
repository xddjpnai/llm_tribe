# Разворачивание на чистом VPS с нуля

От аренды сервера до первого запущенного раунда агентов. Все траты (сервер + LLM)
идут против общего лимита $250 — держи это в голове на каждом шаге.

## 0. Выбор и аренда сервера

Целевая конфигурация: **8 vCPU / 32 GB RAM / 300–500 GB NVMe SSD**.

**Провайдер (актуально на июль 2026):**
- **Hetzner CX** (shared vCPU) — рекомендуемый старт. В июне 2026 Hetzner резко поднял
  цены на выделенные ядра (CCX, +113–176%), а shared (CX) и ARM (CAX) — намного меньше
  (~30%). Наша нагрузка большую часть времени ждёт ответов внешних LLM API, а не жжёт
  CPU, поэтому переплата за выделенные ядра не оправдана.
- **Hetzner CAX** (ARM64) — может быть ещё дешевле. Весь стек имеет arm64-сборки
  (Redpanda, ClickHouse, Redis, Grafana, python:3.12-slim, tesseract, fastembed/onnx),
  но проверь это на этапе заказа, а не считай гарантией. Если берёшь ARM — собери
  образы на самом сервере (`docker compose build` соберёт под arch хоста).
- Альтернативы против vendor lock-in: **OVH / Scaleway / Contabo**.
- ⚠️ Цены волатильны — **сверься с актуальным калькулятором провайдера**, не с цифрами
  «на глаз». Ориентир для shared 8/32: **$30–50/мес**; впиши реальную цену в
  `configs/budget.yaml → server.monthly_cost_usd`, чтобы accrual считался честно.

Возьми Ubuntu 24.04 LTS.

## 1. Базовая настройка сервера

```bash
ssh root@<VPS_IP>

# не-root пользователь
adduser tribe && usermod -aG sudo tribe
rsync --archive --chown=tribe:tribe ~/.ssh /home/tribe    # перенести ключ
# дальше работаем под tribe: ssh tribe@<VPS_IP>

# swap (страховка от упора в память под пиками OCR/эмбеддингов)
sudo fallocate -l 8G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# firewall: наружу — только SSH. Grafana и оркестратор слушают 127.0.0.1
# и доступны через ssh-туннель, публичных портов у них нет.
sudo ufw allow OpenSSH && sudo ufw --force enable
```

**Docker + compose:**
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker tribe    # перелогинься, чтобы применилось
```

## 2. Секреты и доступы

**Telegram-бот:**
1. Создай бота у **@BotFather** → получишь `TELEGRAM_BOT_TOKEN`.
2. Узнай свой numeric user id: напиши **@userinfobot** → это `TELEGRAM_ADMIN_USER_ID`
   (единственный, кому бот подчиняется — guard auth).

**API-ключи провайдеров:** заведи ключи на DeepSeek Platform, Z.ai/GLM, Moonshot/Kimi,
Anthropic. **Search API:** Brave Search API (или Tavily — тогда `SEARCH_PROVIDER=tavily`).

## 3. Код и конфигурация

```bash
git clone <repo_url> llm_tribe && cd llm_tribe
cp .env.example .env
nano .env    # впиши все ключи, TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_USER_ID,
             # CLICKHOUSE_PASSWORD и GRAFANA_ADMIN_PASSWORD (задай крепкие)
```

Проверь `docker compose config` — конфиг валиден, секреты подхватились:
```bash
docker compose config >/dev/null && echo OK
```

## 4. Шаг 0 — мини-эвал перед фиксацией моделей

Не фиксируй модели «на глаз» — прогони эвал на целевом домене (см. `eval/README.md`):
```bash
cd eval && python3 -m venv .venv && .venv/bin/pip install httpx anthropic pyyaml
export ZAI_API_KEY=... DEEPSEEK_API_KEY=... MOONSHOT_API_KEY=... ANTHROPIC_API_KEY=...
.venv/bin/python run_eval.py            # стоит центы; таблица качество/стоимость
cd ..
```
По итогам поправь `configs/model_routing.yaml` (primary/fallback ролей) — **имена
моделей меняются только там, в коде агентов их нет**. Заодно сверь цены с актуальными
прайсами и впиши в тот же файл (budget-guard считает расход по ним).

## 5. Сборка и запуск

```bash
docker compose build          # на ARM соберёт под arm64
docker compose up -d
docker compose ps             # все healthy?
docker compose logs -f orchestrator budget-guard   # смотрим старт
```

При старте оркестратор сам: инициализирует git-репозиторий в общем `workspace`
(ветки агентов, git archive арбитра) и грузит 5 сид-задач из `configs/tasks/` в
очередь. ClickHouse применяет схему из `observability/clickhouse/init/`.

## 6. Проверка контура

```bash
# статус коллегии (оркестратор слушает 127.0.0.1:8001)
./scripts/status.sh          # очередь, агенты, остаток бюджета

# Telegram: напиши боту /help — должен ответить меню (только тебе).
#   /status /budget /journal — проверь, что контур управления жив.

# Grafana — через ssh-туннель с ноутбука:
#   ssh -L 3000:localhost:3000 tribe@<VPS_IP>
#   затем http://localhost:3000 (admin / GRAFANA_ADMIN_PASSWORD)
#   дашборд «llm-tribe — overview»: расход по агентам, вердикты, аномалии.
```

## 7. Первый раунд

Задачи уже в очереди — оркестратор раздаёт их свободным агентам (cap масштабируется
качеством: на старте у всех 0.5). Наблюдай:
```bash
docker compose logs -f agent-1 agent-2 agent-3 arbiter
./scripts/status.sh          # задачи должны идти queued → assigned → submitted → solved/unsolved
```
Через ~час активности journal-сервис сгенерирует первые LLM-саммари — читаются
ботом (`/journal <task_id>`) или как markdown в volume `journal_data`.

## 8. Эксплуатация

- **Добавить задачу:** боту `/addtask kind=maximize cap=8 <постановка>` (валидация
  по твоему user id).
- **Kill-switch:** `./scripts/kill.sh stop` (все) / `./scripts/kill.sh pause agent-2`
  (один) / `./scripts/kill.sh resume`. Или боту `/stop`, `/pause agent-2`.
- **Бюджет:** `/budget` в боте или `GET 127.0.0.1:8001/v1/status`. budget-guard
  сам шлёт алерт в бот при переходе порогов (warn 50% / throttle 80% / hard_stop 95%)
  и жёстко тормозит/останавливает LLM-вызовы у порогов.
- **Обновить routing после нового эвала:** правь `configs/model_routing.yaml` →
  `docker compose restart budget-guard orchestrator`.

## 9. Обслуживание

- Retention: ClickHouse TTL 30 дней (трейсы/аудит) и 90 (вердикты), Redpanda 3–7 дней
  на топиках — диск не пухнет. NVMe 300–500 GB с запасом.
- Бэкап: периодически `docker run --rm -v llm-tribe_workspace:/w -v $PWD:/b alpine
  tar czf /b/workspace_$(date +%F).tgz -C /w .` (результаты агентов) и volume
  `journal_data`. ClickHouse — при желании через `clickhouse-backup`.
- Обновление кода: `git pull && docker compose build && docker compose up -d`.
  Self-mod агентов собирает **кандидат-образы**, но не разворачивает их сам —
  свап делается тобой контролируемо (тег образа приходит в журнал/бот).
