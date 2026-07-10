# Внутренние контракты сервисов

Единый источник правды для HTTP/событийных интерфейсов между сервисами.
Каждый сервис кодируется против этих контрактов, а не против чужого кода.

## budget-guard (реализация — шаг 4)

Единственная точка входа ко всем платным LLM API. Агенты/арбитр/журнал
запрашивают **роль**, а не имя модели; guard резолвит роль → модель по
`configs/model_routing.yaml`, подставляет ключ провайдера, считает стоимость,
проверяет капы (`configs/budget.yaml`), при недоступности провайдера делает
fallback и логирует факт.

### POST /v1/chat
Запрос:
```json
{
  "agent_id": "agent-1",         // кто платит (для per-agent капа и трейса)
  "task_id": "task-abc",         // на какую задачу списывается (для per-task капа); null для фоновых
  "role": "researcher",          // роль из model_routing.yaml
  "messages": [ {"role": "...", "content": "..."} ],
  "tools": [ ... ],              // опц., OpenAI-формат tool specs
  "tool_choice": "auto",         // опц.
  "max_tokens": 4096,
  "temperature": 0.7             // опц.; для anthropic-моделей игнорируется guard'ом
}
```
Ответ 200:
```json
{
  "content": "текст ответа или null",
  "tool_calls": [ {"id":"...","function":{"name":"...","arguments":"{...}"}} ],
  "usage": {"input_tokens": 0, "output_tokens": 0},
  "cost_usd": 0.0031,
  "model": "glm-5.2",            // фактически ответившая модель
  "fell_back": false,            // true если сработал fallback
  "budget": {"spent_total_usd": 12.4, "task_spent_usd": 1.1, "state": "ok"}
}
```
Коды ошибок (guard не кидает исключение в агента, а отдаёт статус):
- `429` + `{"retry_after_sec": N, "reason": "throttle"}` — порог throttle, притормозить.
- `402` + `{"reason": "task_cap" | "agent_cap" | "hard_stop"}` — лимит исчерпан,
  дальнейшие вызовы для этой задачи/агента запрещены → агент завершает работу.

### POST /v1/task_cap
Оркестратор регистрирует фактический (конкурентно масштабированный) cap задачи
до старта агента: `{"task_id":"...","cap_usd": 8.0}` → `{"ok": true, ...}`.
budget-guard enforce'ит именно это значение в admission-контроле /v1/chat; если
cap не зарегистрирован — берётся `per_task_default_cap_usd` из budget.yaml.
budget-guard остаётся авторитетом: капу от агента он не доверяет.

### GET /v1/budget
Снимок для дашбордов/оркестратора: `{spent_total_usd, llm_spent_usd,
server_spent_usd, per_agent: {...}, per_task: {...}, state}`.

## selfmod-api (реализация — шаг 5)

### POST /v1/patch
```json
{"agent_id":"agent-1","description":"...","target":"agent"|"workspace",
 "diff":"unified diff"}
```
Ответ: `{"accepted": bool, "patch_id":"...", "tests_passed": bool,
"logs":"...", "rebuilt": bool}`. Патч применяется в изолированном раннере,
гоняются тесты, при успехе — пересборка/применение, иначе откат.

## search-tool (реализация — шаг後)

### POST /v1/search
`{"agent_id":"...","query":"...","max_results":5}` →
`{"results":[{"title","url","snippet"}], "quota_remaining": N}`.
Только источники из allowlist; при исчерпании квоты — `429`.

## cpu-models (реализация — шаг 6a) — БЕСПЛАТНО, без budget-guard

- `POST /v1/embed` `{"texts":[...]}` → `{"vectors":[[...]], "dim":384}`
- `POST /v1/ocr` `{"image_b64":"..."}` → `{"text":"..."}`

## orchestrator (реализация — шаг 3, здесь)

- `POST /v1/tasks` — добавить задачу в очередь (вызывает comms-bot).
  `{"statement":"...","kind":"exact|maximize|open","cap_usd":10, "meta":{...}}`
  → `{"task_id":"..."}`.
- `POST /v1/kill` — kill-switch. `{"target":"all"|"agent-1","action":"pause"|"stop"}`.
- `GET /v1/status` — состояние очереди и агентов.

## Событийная шина (Redpanda / Kafka-совместимо)

Топики (короткий retention 3–7 дней):
- `tasks.assignments` — оркестратор → агенты: `{task_id, agent_id, cap_usd, statement, kind}`
- `tasks.submissions` — агент → арбитр: `{task_id, agent_id, summary, artifact_ref, branch}`
- `tasks.verdicts` — арбитр → оркестратор/бот: `{task_id, agent_id, verdict, quality, reason}`
- `journal.events` — все сервисы → журнал: человекочитаемые вехи
- `control.commands` — бот → оркестратор/агенты: пауза/стоп/kill

## Аудит (ClickHouse, схема — шаг 6)

Каждое действие агента (LLM-вызов, git-commit, попытка self-mod, сообщение)
пишется в таблицу `audit` с `ts, agent_id, task_id, action, detail, cost_usd`.
Трейс LLM-вызова (промпт/ответ/токены/стоимость) — в `llm_traces`.
