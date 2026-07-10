# Observability

Трейсинг и агрегаты (для графиков/аномалий) — отдельно от бортового журнала
(человекочитаемый нарратив, сервис `journal`).

## ClickHouse

DDL в `clickhouse/init/01_schema.sql` выполняется при первом старте контейнера.
Таблицы (все MergeTree, партиции по дню, TTL для скромного диска):

| Таблица | Кто пишет | Назначение |
|---|---|---|
| `llm_traces` | budget-guard | вызов LLM: токены/стоимость/модель/fallback |
| `audit` | selfmod-api, агенты, budget-guard | все действия агента + смены состояния бюджета |
| `git_diffs` | оркестратор/арбитр при мерже | diff-статистика коммитов агентов |
| `verdicts` | арбитр | итог по задаче (solved/unsolved, quality) — прогресс по целям |

Формат вставки: `INSERT INTO <table> FORMAT JSONEachRow` с полем `ts`
(epoch-секунды, Float64). `event_time` материализуется из `ts`.

## Grafana

- Datasource ClickHouse провижнится автоматически (пароль из `CLICKHOUSE_PASSWORD`,
  compose пробрасывает в контейнер). Нужен плагин `grafana-clickhouse-datasource`
  (ставится через `GF_INSTALL_PLUGINS`).
- Дашборд `llm-tribe — overview` (`dashboards/json/tribe_overview.json`): расход
  LLM всего и по агентам, решено/не решено, fallback'и провайдеров, попытки
  self-modification, смены состояния бюджета (аномалии), вердикты по задачам.
- Полный расход (LLM + accrual сервера) живёт в budget-guard `GET /v1/budget` —
  ClickHouse видит только LLM-поток, т.к. accrual сервера не событие, а функция
  времени. Стат-панель это отмечает в заголовке.

Доступ: Grafana слушает только `127.0.0.1:3000` на VPS — с ноутбука через
`ssh -L 3000:localhost:3000 user@vps`, затем http://localhost:3000.
