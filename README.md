# llm-tribe

Автономная коллегия LLM-агентов на одном VPS (docker-compose). Домен: program
search / алгоритмическое открытие. Агенты стартуют голыми и наращивают
инструментарий через self-modification. Все платные LLM-вызовы — через budget-guard.

## Сервисы

| Сервис | Роль | Ручки |
|---|---|---|
| orchestrator | очередь задач (state machine), распределение, kill-switch | `/v1/tasks` `/v1/kill` `/v1/status` (127.0.0.1:8001) |
| budget-guard | единая точка ко всем LLM API: учёт $, капы, fallback | `/v1/chat` `/v1/task_cap` `/v1/budget` |
| agent (×3) | ReAct-луп + примитивы + self-mod | Kafka consumer |
| arbiter | вердикт solved/unsolved (воспроизводимость + качество) | Kafka consumer |
| selfmod-api | патч → тест в изоляции → применение; защита путей | `/v1/patch` |
| search-tool | внешний поиск (allowlist + квота) — единственный egress агентов | `/v1/search` |
| comms-bot | Telegram: свободный текст, `/kill`, `/user` | Telegram + Kafka |
| journal | LLM-нарратив по задаче/агенту | `/v1/journal` |

Инфра: redpanda (шина), redis (счётчики/blackboard), clickhouse+grafana (трейсы/дашборд).
Контракты между сервисами: `docs/contracts.md`.

## Файлы

    configs/model_routing.yaml     роль → модель + цена + fallback (для budget-guard)
    configs/budget.yaml            капы и пороги (считается только LLM)
    configs/search_allowlist.yaml  домены + квота search-tool
    configs/tasks/*.yaml           сид-очередь исследовательских задач
    secrets/credentials.env        ВСЕ секреты + TELEGRAM_OWNER_IDS (chmod 600, агентам недоступен)
    eval/                          шаг 0: мини-эвал моделей до деплоя (отдельно от рантайма)
    observability/                 ClickHouse DDL + Grafana provisioning
    scripts/compose.sh|kill.sh|status.sh

## Примитивы агента

`run_python` `read_file` `write_file` `list_dir` `git_commit` `search_literature`
`propose_self_modification` `submit_result`. Всё сверх этого агент строит себе сам
через `propose_self_modification` (патч → тест в изоляции → применение).

## Инварианты

1. LLM только через budget-guard; ключей провайдеров у агентов нет.
2. docker.sock смонтирован только в selfmod-api.
3. Приватные папки агентов — отдельные volumes.
4. agents_net internal (без egress); наружу только через search-tool/budget-guard.
5. Лимиты cpu/mem/pids на каждый контейнер агента.
6. Каждое действие агента → ClickHouse (audit).
7. Fallback между провайдерами — в model_routing.yaml.
8. Kill-switch: orchestrator `/v1/kill` + бот `/kill` + `scripts/kill.sh`.
9. selfmod-api отклоняет патчи к защищённым путям (kill/user/auth/креды/деньги) — `selfmod/protect.py`.
10. Секреты и владельцы — в secrets/credentials.env; агентам недоступен.

## Типы задач

- Дискретные (research): `queued → assigned → in_progress → submitted → solved|unsolved`.
- Фоновые (comms-bot, journal): долгоживущие, не «завершаются».

## Bring-up

    cp secrets/credentials.env.example secrets/credentials.env   # заполнить, chmod 600
    ./scripts/compose.sh up -d
    ./scripts/status.sh                                          # очередь / агенты / бюджет

Управление — боту свободным текстом («поставь задачу: …», «статус»). Защищённые
команды только явно: `/kill [target]`, `/user add|remove|list`.
