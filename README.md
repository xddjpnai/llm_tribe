# llm-tribe

Минимальное зерно самопереписывающихся LLM-агентов на одном VPS. Агенты стартуют
голыми (примитивы + self-mod) и сами строят себе журнал, связь с оператором и приём
задач — это их 3 стартовые задачи. Дальше решают задачи, приходящие через
построенный ими же канал. Мы даём только РЕСУРСЫ; остальное агенты строят сами.

## Предзадано (ресурсы) / защищено

Предзадано — минимальная инфра:
- `agent` (×3) — голый ReAct-луп + примитивы + self-mod; с egress и токеном Telegram.
- `budget-guard` — единственный доступ к LLM (ключи только здесь); клампит `max_tokens`
  на вызов = рамка на одно действие. Общий расход отслеживает владелец сам.
- `selfmod-api` — патч → тест в песочнице → применение. Единственный с docker.sock.
- `sage` — мудрец/судья общины: при `submit_result` независимо воспроизводит артефакт
  из ветки агента + LLM-оценка (другой вендор); агент не может сам объявить задачу
  решённой. Неизменяем агентами (protect.py).
- `redis` — общее состояние (события, очередь задач, трейсы).
- `secrets/credentials.env` — креды и владельцы. `configs/initial_tasks.yaml` — 3 задачи.

Защищено от агентов:
- **kill** — на уровне ХОСТА (`scripts/kill.sh` = docker stop агентов); агент не отменит.
- **креды** — `secrets/` не монтируется агентам; selfmod отклоняет патчи к `secrets/`,
  `budget_guard/`, `selfmod_api/` (`selfmod/protect.py`).
- **песочница перед применением** — любой патч тестируется изолированно, чтобы агент
  не убил себя.

## Примитивы агента

`run_python` (есть сеть) · `read_file` · `write_file` · `list_dir` · `git_commit` ·
`propose_self_modification` · `submit_result`. Всё сверх — агент пишет себе сам.

## Ручки

- budget-guard: `POST /v1/chat` (роль→модель→fallback, клампинг max_tokens),
  `GET /v1/budget` (накопитель + рамка на вызов).
- selfmod-api: `POST /v1/patch` — `{agent_id, target: workspace|agent, diff}` →
  `{accepted, tests_passed, rebuilt, logs}`; защищённые пути отклоняются.
- sage: `POST /v1/judge` — `{task_id, statement, summary, artifact_ref, branch}` →
  `{verdict: solved|unsolved, quality, reproducible, reason}`.

## Redis-конвенции (их использует агент-построенный журнал/канал)

- список `events` — события (online, tool-вызовы, вердикты); пишут агент и мудрец.
- список `tasks` — очередь новых задач (`blpop` распределяет по агентам); сюда
  агент-построенный приём кладёт постановки от оператора.
- ключ `claim:<id>` — клейм стартовой задачи (чтобы не делать дважды).

## Конфиги

    configs/model_routing.yaml   роль → модель + цена + fallback (budget-guard)
    configs/budget.yaml          рамка на один вызов (max_tokens/стоимость)
    configs/initial_tasks.yaml   3 стартовые задачи

## Bring-up

    cp secrets/credentials.env.example secrets/credentials.env   # заполнить, chmod 600
    ./scripts/compose.sh up -d
    ./scripts/status.sh                    # контейнеры + накопленный расход
    ./scripts/kill.sh [all|agent-N|resume] # защищённый kill (host-level)
