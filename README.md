# llm-tribe — автономная исследовательская коллегия агентов

Мульти-агентная система для автономного решения исследовательских задач в домене
program search / алгоритмического открытия. Один VPS (8 vCPU / 32 GB), docker-compose,
все LLM — через hosted API за budget-guard прокси. Общий бюджет $250 (LLM + сервер).

## Структура репозитория

```
.
├── docker-compose.yml        # весь стек: агенты, инфра, защитные сервисы
├── .env.example              # шаблон секретов (API-ключи, Telegram)
├── configs/
│   ├── model_routing.yaml    # роль → модель + fallback-цепочки + цены (для budget-guard)
│   ├── budget.yaml           # лимиты: общий $250, per-task, per-agent, пороги троттлинга
│   └── tasks/                # сид-очередь исследовательских задач (yaml на задачу)
├── services/
│   ├── orchestrator/         # LangGraph: очередь задач (state machine) + kill-switch API
│   ├── budget_guard/         # FastAPI-прокси перед всеми LLM API, учёт $ в реальном времени
│   ├── agent/                # образ агента-исследователя ("голый" ReAct-луп), N инстансов
│   ├── arbiter/              # агент-арбитр: вердикт "решена / не решена" по критериям
│   ├── selfmod_api/          # приём патчей от агентов → тесты в изоляции → пересборка
│   ├── search_tool/          # контролируемый внешний поиск (allowlist + квоты запросов)
│   ├── comms_bot/            # Telegram-бот: уведомления, приём задач, пауза/kill-switch
│   ├── journal/              # бортовой журнал: LLM-саммари → markdown/БД, выдача через бота
│   └── cpu_models/           # self-hosted CPU: OCR + эмбеддинги (ONNX), без budget-guard
├── observability/
│   ├── clickhouse/init/      # DDL: трейсы LLM-вызовов, git-diff'ы, аудит действий
│   └── grafana/              # provisioning + дашборды (расход бюджета, прогресс, аномалии)
├── eval/                     # шаг 0: мини-эвал моделей-кандидатов перед фиксацией routing
├── workspace/                # (volume) общий git-репозиторий агентов, ветка на агента
└── docs/
    └── deployment.md         # разворачивание на чистом VPS с нуля
```

## Два типа задач

- **Очередь дискретных задач** (исследование): state machine в оркестраторе —
  `queued → assigned → in_progress → submitted → (solved | unsolved)`. Вердикт
  выносит арбитр по объективным критериям; при исчерпании бюджета задачи — `unsolved`.
- **Фоновые сервисы** (связь, журнал): долгоживущие процессы без состояния "завершено".

## Ключевые инварианты безопасности

1. Все платные LLM-вызовы — только через budget-guard (агенты не имеют ключей провайдеров).
2. docker.sock смонтирован только в selfmod-api; агентам недоступен.
3. Приватные папки агентов — отдельные Docker volumes.
4. Сеть агентов — internal (без egress); внешний мир только через search-tool/budget-guard.
5. Ресурсные лимиты (cpu/mem/pids) на каждый контейнер агента.
6. Каждое действие агента пишется в ClickHouse с таймстампом и agent-ID.
7. Fallback между провайдерами задан в configs/model_routing.yaml.
8. Kill-switch: эндпоинт оркестратора + команда в Telegram-боте.
