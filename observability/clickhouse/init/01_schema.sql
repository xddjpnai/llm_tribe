-- Схема наблюдаемости llm-tribe. Выполняется при инициализации контейнера ClickHouse.
-- Код сервисов пишет JSONEachRow-строками с полем ts = epoch-секунды (Float64);
-- event_time материализуется из ts для фильтров времени в Grafana.

-- Трейс каждого LLM-вызова (пишет budget-guard): промпт-мета/токены/стоимость/модель.
CREATE TABLE IF NOT EXISTS llm_traces
(
    ts            Float64,
    event_time    DateTime64(3) MATERIALIZED toDateTime64(ts, 3),
    agent_id      LowCardinality(String),
    task_id       String,
    model         LowCardinality(String),
    input_tokens  UInt32,
    output_tokens UInt32,
    cost_usd      Float64,
    fell_back     UInt8
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(event_time)
ORDER BY (event_time, agent_id)
TTL toDateTime(event_time) + INTERVAL 30 DAY;   -- скромный retention, диск не пухнет

-- Полный аудит действий агента (guard #6): git-commit, selfmod, сообщения и т.п.
CREATE TABLE IF NOT EXISTS audit
(
    ts        Float64,
    event_time DateTime64(3) MATERIALIZED toDateTime64(ts, 3),
    agent_id  LowCardinality(String),
    task_id   String,
    action    LowCardinality(String),
    detail    String,
    cost_usd  Float64 DEFAULT 0
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(event_time)
ORDER BY (event_time, agent_id)
TTL toDateTime(event_time) + INTERVAL 30 DAY;

-- Git-diff каждого коммита агента (пишет оркестратор/арбитр при мерже ветки).
CREATE TABLE IF NOT EXISTS git_diffs
(
    ts          Float64,
    event_time  DateTime64(3) MATERIALIZED toDateTime64(ts, 3),
    agent_id    LowCardinality(String),
    task_id     String,
    branch      String,
    commit_sha  String,
    files_changed UInt32,
    insertions  UInt32,
    deletions   UInt32,
    summary     String
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(event_time)
ORDER BY (event_time, agent_id)
TTL toDateTime(event_time) + INTERVAL 30 DAY;

-- Вердикты арбитра по задачам — источник прогресса по целям.
CREATE TABLE IF NOT EXISTS verdicts
(
    ts        Float64,
    event_time DateTime64(3) MATERIALIZED toDateTime64(ts, 3),
    task_id   String,
    agent_id  LowCardinality(String),
    verdict   LowCardinality(String),   -- 'solved' | 'unsolved'
    quality   Float64,                  -- 0..1 оценка отчёта/воспроизводимости
    reason    String
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(event_time)
ORDER BY (event_time, task_id)
TTL toDateTime(event_time) + INTERVAL 90 DAY;   -- вердикты храним дольше (история опыта)
