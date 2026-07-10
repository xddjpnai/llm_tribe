# Разворачивание на чистом VPS с нуля

От аренды сервера до первого запущенного раунда агентов. Budget-guard считает
только LLM-расход против страховочного потолка $100 (configs/budget.yaml) — при его
достижении останавливает LLM-вызовы и пингует тебя. Аренду сервера и пополнение
API оплачиваешь ты сам; в потолок они не входят.

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
  это твой прямой расход (budget-guard его не учитывает — считается только LLM).

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
2. Узнай свой numeric user id: напиши **@userinfobot** → это твой `TELEGRAM_OWNER_IDS`
   (владелец с полным доступом; можно несколько через запятую).

**API-ключи провайдеров:** заведи ключи на DeepSeek Platform, Z.ai/GLM, Moonshot/Kimi,
Anthropic. **Search API:** Brave Search API (или Tavily — тогда `SEARCH_PROVIDER=tavily`).

## 3. Код и креды

Все секреты и список владельцев — в ОДНОМ файле `secrets/credentials.env`, который
редактируешь только ты (git-ignored, `chmod 600`, агентам недоступен):

```bash
git clone <repo_url> llm_tribe && cd llm_tribe
cp secrets/credentials.env.example secrets/credentials.env
nano secrets/credentials.env   # все ключи, TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_IDS (СВОЙ id),
                               # CLICKHOUSE_PASSWORD, GRAFANA_ADMIN_PASSWORD (крепкие)
chmod 600 secrets/credentials.env
```

Все команды `docker compose` дальше — через обёртку `./scripts/compose.sh`, она
передаёт этот файл кредов как источник переменных. Проверь, что конфиг валиден:
```bash
./scripts/compose.sh config >/dev/null && echo OK
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
./scripts/compose.sh build          # на ARM соберёт под arm64
./scripts/compose.sh up -d
./scripts/compose.sh ps             # все healthy?
./scripts/compose.sh logs -f orchestrator budget-guard   # смотрим старт
```

При старте оркестратор сам: инициализирует git-репозиторий в общем `workspace`
(ветки агентов, git archive арбитра) и грузит 5 сид-задач из `configs/tasks/` в
очередь. ClickHouse применяет схему из `observability/clickhouse/init/`.

## 6. Проверка контура

```bash
# статус коллегии (оркестратор слушает 127.0.0.1:8001)
./scripts/status.sh          # очередь, агенты, остаток бюджета

# Telegram: напиши боту обычным текстом «статус» или «что происходит?» — ответит
#   (только тебе как владельцу). Задачу тоже ставь текстом: «поставь задачу: …».
#   /kill — проверь kill-switch (защищённая команда). /user list — увидишь себя;
#   /user add <id> — поделиться доступом.

# Grafana — через ssh-туннель с ноутбука:
#   ssh -L 3000:localhost:3000 tribe@<VPS_IP>
#   затем http://localhost:3000 (admin / GRAFANA_ADMIN_PASSWORD)
#   дашборд «llm-tribe — overview»: расход по агентам, вердикты, аномалии.
```

## 7. Первый раунд

Задачи уже в очереди — оркестратор раздаёт их свободным агентам (cap масштабируется
качеством: на старте у всех 0.5). Наблюдай:
```bash
./scripts/compose.sh logs -f agent-1 agent-2 agent-3 arbiter
./scripts/status.sh          # задачи должны идти queued → assigned → submitted → solved/unsolved
```
Через ~час активности journal-сервис сгенерирует первые LLM-саммари — читаются
ботом (`/journal <task_id>`) или как markdown в volume `journal_data`.

## 8. Эксплуатация

- **Добавить задачу:** напиши боту текстом, напр. «поставь задачу: улучшить эвристику
  bin packing, бюджет $3» — LLM-роутер сам извлечёт постановку/kind/cap. Команды
  `/addtask` нет.
- **Поделиться доступом:** `/user add <telegram_id>` (только владелец), `/user list`,
  `/user remove <id>`. Себя (владельца из кредов) убрать нельзя.
- **Kill-switch:** `./scripts/kill.sh stop` (все) / `./scripts/kill.sh pause agent-2`
  (один) / `./scripts/kill.sh resume`. Или боту `/kill`, `/pause agent-2`, `/resume`.
- **Бюджет:** `/budget` в боте или `GET 127.0.0.1:8001/v1/status`. budget-guard
  сам шлёт алерт владельцам при переходе порогов (warn 50% / throttle 75% /
  hard_stop 90%) и тормозит/останавливает LLM-вызовы. При hard_stop подними
  `total_budget_usd` в configs/budget.yaml (или `BUDGET_TOTAL_USD` в кредах) и
  `./scripts/compose.sh restart budget-guard`.
- **Обновить routing после нового эвала:** правь `configs/model_routing.yaml` →
  `./scripts/compose.sh restart budget-guard orchestrator`.

## 9. Обслуживание

- Retention: ClickHouse TTL 30 дней (трейсы/аудит) и 90 (вердикты), Redpanda 3–7 дней
  на топиках — диск не пухнет. NVMe 300–500 GB с запасом.
- Бэкап: периодически `docker run --rm -v llm-tribe_workspace:/w -v $PWD:/b alpine
  tar czf /b/workspace_$(date +%F).tgz -C /w .` (результаты агентов), а также volume'ы
  `journal_data` и `authz_data` (список участников /user). ClickHouse — при желании
  через `clickhouse-backup`.
- Обновление кода: `git pull && ./scripts/compose.sh build && ./scripts/compose.sh up -d`.
  Self-mod агентов собирает **кандидат-образы**, но не разворачивает их сам —
  свап делается тобой контролируемо (тег образа приходит в журнал/бот). Защищённые
  пути (kill/user/auth/креды/деньги) агенты пропатчить не могут вообще.
