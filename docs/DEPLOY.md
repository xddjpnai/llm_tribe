# Развёртывание llm-tribe с нуля на чистом VPS

Пошаговая инструкция для НЕспециалиста. Копируй команды по одной, сверху вниз.
Где нужно подставить своё значение — написано `<...>`.

> Что мы поднимаем: 6 контейнеров (redis, budget-guard, selfmod-api, sage и три
> агента). Тяжёлой инфры (ClickHouse/Grafana/Kafka) больше нет — хватает
> скромного сервера. Агенты стартуют «голыми» и первые три задачи тратят на то,
> чтобы построить себе журнал, Telegram-бота и приём задач. **Это самая
> рискованная часть — см. раздел 9.**

---

## 1. Выбор и аренда VPS

Нужен обычный Linux-VPS. Ориентир: **~8 vCPU, 8–16 GB RAM, 40–80 GB SSD**,
Ubuntu 24.04 LTS. Меньше (4 vCPU / 8 GB) тоже заведётся, но сборка образов и
песочница self-mod будут медленнее.

- **Hetzner Cloud** — дёшево. Подойдут CX (Intel/AMD) или CAX (ARM). Например
  `CX42` (8 vCPU / 16 GB) или ARM `CAX31` (8 vCPU / 16 GB). ARM дешевле и всё в
  проекте под него собирается (базовые образы `python:3.12-slim` и
  `redis:7-alpine` мультиархитектурные).
- Заметка про цены: тарифы Hetzner CX/CAX периодически меняются и модели
  переименовывают — не ищи точное имя из этой инструкции, бери любой план с
  нужными характеристиками (≥8 vCPU, ≥8 GB RAM).
- Любой другой провайдер (Contabo, Netcube, DigitalOcean, Vultr…) тоже годится —
  важны только характеристики и Ubuntu/Debian.

При создании сервера:
- Образ: **Ubuntu 24.04**.
- Добавь свой SSH-ключ (если его нет — сгенерируй на своём компьютере:
  `ssh-keygen -t ed25519`, публичный ключ `~/.ssh/id_ed25519.pub` вставь в панели
  провайдера). Пароль-логин лучше отключить сразу.
- Регион — ближе к тебе (меньше задержка SSH). Egress наружу нужен (агенты ходят
  к LLM-провайдерам и в Telegram) — у обычных VPS он открыт по умолчанию.

Запиши IP-адрес сервера — дальше он `<SERVER_IP>`.

---

## 2. Первичная настройка сервера

Зайди по SSH под root:

```bash
ssh root@<SERVER_IP>
```

### 2.1. Не-root пользователь

Работать под root опасно. Создай обычного пользователя (пусть `tribe`) с sudo:

```bash
adduser --disabled-password --gecos "" tribe
usermod -aG sudo tribe
mkdir -p /home/tribe/.ssh
cp ~/.ssh/authorized_keys /home/tribe/.ssh/authorized_keys
chown -R tribe:tribe /home/tribe/.ssh
chmod 700 /home/tribe/.ssh
chmod 600 /home/tribe/.ssh/authorized_keys
```

Проверь, что заходит (в НОВОМ терминале, не закрывая текущий):

```bash
ssh tribe@<SERVER_IP>
```

Если зашло — дальше работаем под `tribe`. Root-сессию можно закрыть.

### 2.2. Firewall (ufw): наружу только SSH

```bash
sudo apt update
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw --force enable
sudo ufw status
```

Внутренние порты сервисов (8080/8090/8095, redis 6379) наружу НЕ открыты и не
должны быть — контейнеры общаются между собой по внутренней docker-сети. Наружу
торчит только SSH.

### 2.3. Swap (страховка по памяти)

Полезно, если возьмёшь сервер с 8 GB — сборка/пики не убьют процессы OOM'ом:

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

### 2.4. Установка Docker + compose-плагина

Официальный способ Docker (репозиторий Docker, а не устаревший `docker.io` из
Ubuntu):

```bash
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Разреши своему пользователю запускать docker без sudo:

```bash
sudo usermod -aG docker tribe
```

**Перелогинься** (`exit`, снова `ssh tribe@<SERVER_IP>`), чтобы группа применилась.
Проверь:

```bash
docker run --rm hello-world
docker compose version
```

Обе команды должны отработать без ошибок и без sudo.

---

## 3. Где взять секреты

Понадобится 6 значений. Собери их заранее.

### 3.1. Telegram Bot Token

1. В Telegram напиши **@BotFather** → `/newbot`.
2. Задай имя и username бота (username должен кончаться на `bot`).
3. BotFather пришлёт строку вида `123456789:AAG...` — это `TELEGRAM_BOT_TOKEN`.

### 3.2. Свой numeric Telegram id

1. Напиши боту **@userinfobot** (или @getmyid_bot) — пришлёт твой числовой `Id`.
2. Это `TELEGRAM_OWNER_IDS`. Если владельцев несколько — через запятую без
   пробелов: `11111111,22222222`.

### 3.3. Ключи LLM-провайдеров

Нужны все четыре (агенты и мудрец используют разных вендоров; sage намеренно
берёт другого вендора, чем агенты, чтобы судья не был предвзят):

| Провайдер | Где взять ключ | Переменная |
|-----------|----------------|------------|
| DeepSeek  | https://platform.deepseek.com → API keys | `DEEPSEEK_API_KEY` |
| Z.ai (GLM)| https://z.ai / https://docs.z.ai → API keys | `ZAI_API_KEY` |
| Moonshot (Kimi) | https://platform.moonshot.ai → API keys | `MOONSHOT_API_KEY` |
| Anthropic (Claude, для мудреца) | https://console.anthropic.com → API keys | `ANTHROPIC_API_KEY` |

На каждом провайдере надо будет пополнить баланс/подключить карту — это твой
расход на LLM (общего потолка в системе нет, следишь за ним сам, см. раздел 10).

> Модели и цены заданы в `configs/model_routing.yaml` и уже сверены с
> документацией провайдеров. Если провайдер сменит id модели или цену — правишь
> ТОЛЬКО этот файл, код не трогаешь.

---

## 4. Получить код и заполнить секреты

### 4.1. Склонировать репозиторий

```bash
cd ~
git clone <URL_РЕПОЗИТОРИЯ> llm_tribe
cd llm_tribe
```

(Если репозиторий приватный — сначала настрой доступ, либо просто скопируй папку
проекта на сервер через `scp -r`.)

### 4.2. Заполнить secrets/credentials.env

Это ЕДИНСТВЕННЫЙ файл с секретами. Он git-ignored, НЕ монтируется агентам, и
selfmod отклоняет любые патчи к `secrets/` — то есть агенты не могут достать
ключи.

```bash
cp secrets/credentials.env.example secrets/credentials.env
nano secrets/credentials.env
```

Заполни все поля (без кавычек, без пробелов вокруг `=`):

```
ZAI_API_KEY=<твой ключ z.ai>
DEEPSEEK_API_KEY=<твой ключ deepseek>
MOONSHOT_API_KEY=<твой ключ moonshot>
ANTHROPIC_API_KEY=<твой ключ anthropic>
TELEGRAM_BOT_TOKEN=<токен от BotFather>
TELEGRAM_OWNER_IDS=<твой numeric id>
```

Сохрани (в nano: `Ctrl+O`, `Enter`, `Ctrl+X`) и закрой права:

```bash
chmod 600 secrets/credentials.env
```

### 4.3. Почему запуск через ./scripts/compose.sh, а не голый docker compose

`scripts/compose.sh` — это обёртка, которая передаёт `secrets/credentials.env`
как `--env-file` в каждую команду compose. Голый `docker compose up` НЕ подставит
переменные (`${ZAI_API_KEY}` и т.д. останутся пустыми), и сервисы стартуют без
ключей. Всегда используй `./scripts/compose.sh <команда>` вместо
`docker compose <команда>`. Обёртка ещё и проверяет, что файл секретов вообще
существует, и падает с понятной ошибкой, если нет.

---

## 5. Сборка и запуск

```bash
./scripts/compose.sh build      # соберёт 6 образов, первый раз ~3–8 минут
./scripts/compose.sh up -d      # поднимет стек в фоне
```

---

## 6. Убедиться, что всё healthy

```bash
./scripts/compose.sh ps
```

Ожидаемая картина: `redis`, `budget-guard`, `selfmod-api`, `sage` — со статусом
`Up ... (healthy)`; три `agent-N` — просто `Up` (у агентов нет HTTP-порта, для них
healthcheck не заведён).

Проверь healthz-ручки сервисов напрямую (порты наружу закрыты — стучимся изнутри
контейнера):

```bash
for s in budget-guard:8080 selfmod-api:8090 sage:8095; do
  name=${s%:*}; port=${s#*:}
  cid=$(./scripts/compose.sh ps -q $name)
  echo -n "$name: "
  docker exec "$cid" python3 -c \
    "import urllib.request;print(urllib.request.urlopen('http://localhost:$port/healthz').read().decode())"
done
```

Каждая строка должна вернуть `{"status":"ok"}`.

Единый статус + накопленный расход:

```bash
./scripts/status.sh
```

В конце увидишь `{'llm_spent_usd': 0.0, 'frame_per_call': {...}}` — пока агенты
ничего не потратили.

Если какой-то сервис не `healthy` — смотри его лог:

```bash
./scripts/compose.sh logs --tail=50 budget-guard
```

Типичная причина — пустой/неверный ключ в `credentials.env`.

---

## 7. Что происходит при первом старте (внутренняя механика)

Понимать полезно, чтобы не пугаться логов:

1. **selfmod-api** на старте инициализирует общий том `workspace` как git-репо
   (`git init` + пустой первый коммит) и отдаёт его во владение агентам (uid
   10001). Без этого коммиты агентов и оценка мудреца сломались бы.
2. Каждый **agent-N** при старте пишет в Redis событие `online`, берёт по одной
   из трёх стартовых задач (клейм через Redis `claim:<id>`, чтобы не делать
   дважды) и начинает их решать через ReAct-луп.
3. Решение любой задачи идёт через **budget-guard** — единственную точку к LLM
   (ключи только там). Он клампит `max_tokens` до рамки на один вызов и при
   недоступности провайдера идёт по fallback-цепочке.
4. Когда агент считает задачу решённой (`submit_result`), её независимо
   оценивает **sage** (мудрец, другой вендор): воспроизводит артефакт из ветки
   агента и ставит вердикт. Сам агент объявить задачу решённой не может.

---

## 8. Как понять, что агенты «проросли»

Три стартовые задачи — построить себе журнал, Telegram-канал и приём задач.
«Проросли» = сами написали код бота и НАПИСАЛИ тебе в Telegram первыми.

### 8.1. Смотреть логи в реальном времени

```bash
./scripts/compose.sh logs -f agent-1
```

(`Ctrl+C` чтобы выйти; лог продолжает писаться.) Ищи строки про tool-вызовы
(`tool:run_python`, `tool:write_file`, `tool:git_commit`, `selfmod_attempt`).

### 8.2. Смотреть поток событий в Redis

```bash
cid=$(./scripts/compose.sh ps -q redis)
docker exec "$cid" redis-cli -n 0 lrange events -20 -1
```

Здесь видно `online`, tool-вызовы, вердикты мудреца. (Счётчик расхода
budget-guard живёт отдельно, в db 1: `redis-cli -n 1 get llm:total`.)

### 8.3. Главный признак — сообщение в Telegram

Дождись, пока агент построит бота и **сам напишет тебе первым** в Telegram (на
`TELEGRAM_OWNER_IDS`). Это и есть «проросли». До этого канала связи у тебя нет —
он часть того, что агенты строят.

Если долго тихо — смотри логи (8.1) и раздел 9.

---

## 9. Самая рискованная часть — самопостроение

**Честно:** заведётся ли всё «из коробки», зависит от того, справится ли модель
построить бота/журнал/приём с нуля через патчи. Это НЕ проверено на реальных
прогонах с живыми ключами — только офлайн-тестами и смоук-тестом инфраструктуры
(healthz, git-bootstrap, приём задач, песочница self-mod — всё это работает).

Первые запуски почти наверняка потребуют доводки. Что крутить:

- **`services/agent/agent/graph.py`** — `SYSTEM_PROMPT`. Здесь задаётся, как
  агент понимает свою задачу (строить детач-процессы через run_python, валидировать
  через self-mod перед тем как полагаться на код, что бот — на `TELEGRAM_BOT_TOKEN`
  из окружения). Если агент путается — уточняй формулировки здесь.
- **`configs/initial_tasks.yaml`** — тексты трёх стартовых задач. Если агент
  строит не то или буксует — переписывай постановки конкретнее.

После правки промптов/задач пересобери и перезапусти агентов:

```bash
./scripts/compose.sh up -d --build agent-1 agent-2 agent-3
```

Признаки, что что-то не так: в логах повторяющиеся `task_error`, `provider_fallback`
на всех моделях (значит ключи/провайдеры недоступны — проверь `credentials.env`),
или агент крутит шаги без прогресса до потолка `AGENT_MAX_STEPS`.

---

## 10. Эксплуатация

### 10.1. Kill-switch (аварийная остановка)

Остановка агентов на уровне ХОСТА — агент не может её отменить из контейнера:

```bash
./scripts/kill.sh            # стоп всех трёх агентов
./scripts/kill.sh agent-2    # стоп одного
./scripts/kill.sh resume     # снова запустить всех
```

budget-guard/selfmod-api/sage/redis при этом продолжают работать.

### 10.2. Давать новые задачи

Задачи ставятся через канал, который агенты построят себе в Telegram. **Команды
`/addtask` НЕТ** — пиши свободным текстом, если агент так построил приём (он
должен сам разобрать текст в постановку и положить в Redis-очередь `tasks`;
принимаются только сообщения от авторизованного оператора из `TELEGRAM_OWNER_IDS`).

Если приём ещё не построен, а задачу поставить надо срочно, можно положить её в
очередь вручную (обходя канал):

```bash
cid=$(./scripts/compose.sh ps -q redis)
docker exec "$cid" redis-cli -n 0 rpush tasks \
  '{"id":"my-task-1","statement":"<текст задачи>","kind":"open"}'
```

Свободный агент заберёт её через `blpop` (одна задача — одному агенту).

### 10.3. Следить за расходом

Общего потолка в системе НЕТ — следишь сам. Накопленный LLM-расход:

```bash
cid=$(./scripts/compose.sh ps -q budget-guard)
docker exec "$cid" python3 -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8080/v1/budget').read().decode())"
```

(То же показывает хвост `./scripts/status.sh`.) Плюс контролируй баланс/лимиты в
личных кабинетах провайдеров — это первичная защита от неожиданного счёта.

Рамка на ОДНО действие (клампинг `max_tokens` и порог стоимости для warning'а) —
в `configs/budget.yaml`.

---

## 11. Бэкап и обновление

### 11.1. Бэкап тома workspace

В томе `workspace` живёт весь код и инструменты, которые агенты написали себе.
Это самое ценное. Бэкап в tar:

```bash
docker run --rm \
  -v llm-tribe_workspace:/data:ro \
  -v "$PWD":/backup \
  alpine tar czf /backup/workspace-$(date +%F).tar.gz -C /data .
```

Получится файл `workspace-YYYY-MM-DD.tar.gz` в текущей папке — скопируй его к себе
(`scp tribe@<SERVER_IP>:~/llm_tribe/workspace-*.tar.gz .`).

Восстановление (в пустой том):

```bash
docker run --rm \
  -v llm-tribe_workspace:/data \
  -v "$PWD":/backup \
  alpine sh -c "cd /data && tar xzf /backup/workspace-<ДАТА>.tar.gz"
```

> Имя тома — `llm-tribe_workspace` (docker префиксует именем проекта из
> `docker-compose.yml`). Проверить точное имя: `docker volume ls | grep workspace`.

### 11.2. Обновление кода

```bash
cd ~/llm_tribe
git pull
./scripts/compose.sh up -d --build
```

`credentials.env` и том `workspace` при этом не трогаются — секреты и наработки
агентов сохраняются.

### 11.3. Полная остановка / удаление

```bash
./scripts/compose.sh down          # остановить всё, тома сохранить
./scripts/compose.sh down -v       # + УДАЛИТЬ тома (workspace, redis, private) — ОСТОРОЖНО
```

`down -v` сотрёт всё, что агенты построили. Сделай бэкап (11.1) перед этим.
