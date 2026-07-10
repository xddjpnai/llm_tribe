"""orchestrator: внешние рамки коллегии — очередь задач (state machine),
распределение по агентам с конкуренцией за бюджет, kill-switch.

Фоновые потоки:
  - assignment: свободным агентам раздаёт pending-задачи (cap ~ качество агента)
  - submissions: tasks.submissions -> mark_submitted
  - verdicts:    tasks.verdicts -> finalize + обновление качества агента

HTTP: /v1/tasks (добавить), /v1/kill (kill-switch), /v1/status.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import threading
import time
from typing import Optional

import httpx
import redis
import yaml
from fastapi import FastAPI
from pydantic import BaseModel

from .assign import task_cap_for, update_rolling_quality
from .queue import TaskQueue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("orchestrator")

app = FastAPI(title="orchestrator")

_r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
q = TaskQueue(_r)
_http = httpx.Client(timeout=15.0)
GUARD_URL = os.environ.get("BUDGET_GUARD_URL", "http://budget-guard:8080")

_routing = yaml.safe_load(open(os.environ.get("MODEL_ROUTING", "/configs/model_routing.yaml")))
_budget = yaml.safe_load(open(os.environ.get("BUDGET_CONFIG", "/configs/budget.yaml")))
AGENTS = list(_routing.get("agents", {}).keys())
BASE_CAP = float(_budget["llm"]["per_task_default_cap_usd"])
TOTAL_BUDGET = float(_budget["total_budget_usd"])
COMP = _budget.get("competition", {"enabled": True, "min_share": 0.15})
MIN_SHARE = float(COMP.get("min_share", 0.15))


def _producer():
    from kafka import KafkaProducer

    return KafkaProducer(bootstrap_servers=os.environ["KAFKA_BROKERS"].split(","),
                         value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode())


_prod = None


def emit(topic: str, payload: dict) -> None:
    global _prod
    try:
        if _prod is None:
            _prod = _producer()
        _prod.send(topic, payload)
        _prod.flush(timeout=3)
    except Exception as e:  # noqa: BLE001
        log.warning("emit %s failed: %s", topic, e)


def _remaining_budget() -> float:
    try:
        b = _http.get(f"{GUARD_URL}/v1/budget").json()
        return max(0.0, TOTAL_BUDGET - float(b.get("spent_total_usd", 0.0)))
    except Exception:  # noqa: BLE001
        return TOTAL_BUDGET   # budget-guard недоступен — не блокируем распределение


# --------------------------------- HTTP ---------------------------------

class TaskIn(BaseModel):
    statement: str
    kind: str = "open"
    cap_usd: Optional[float] = None


class KillIn(BaseModel):
    target: str = "all"        # "all" | agent-id
    action: str = "pause"      # "pause" | "resume" | "stop"


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/tasks")
def add_task(t: TaskIn) -> dict:
    tid = q.create_task(t.statement, t.kind, t.cap_usd, source="api")
    emit("journal.events", {"ts": time.time(), "agent_id": "orchestrator",
         "task_id": tid, "action": "task_queued", "detail": t.statement[:200]})
    return {"task_id": tid}


@app.post("/v1/kill")
def kill(k: KillIn) -> dict:
    if k.target == "all":
        if k.action == "stop":
            q.set_flag("stopped", True)
        elif k.action == "pause":
            q.set_flag("paused", True)
        elif k.action == "resume":
            q.set_flag("paused", False)
            q.set_flag("stopped", False)
    # эхо команды агентам (они сами реагируют на pause/resume/stop)
    emit("control.commands", {"ts": time.time(), "target": k.target, "action": k.action})
    emit("journal.events", {"ts": time.time(), "agent_id": "orchestrator",
         "action": "killswitch", "detail": f"{k.action} -> {k.target}"})
    return {"ok": True, "target": k.target, "action": k.action,
            "paused": q.get_flag("paused"), "stopped": q.get_flag("stopped")}


@app.get("/v1/status")
def status() -> dict:
    st = q.status(AGENTS)
    st["paused"] = q.get_flag("paused")
    st["stopped"] = q.get_flag("stopped")
    st["remaining_budget_usd"] = round(_remaining_budget(), 2)
    return st


# --------------------------------- фоновые потоки ---------------------------------

def _load_seeds() -> None:
    pattern = os.environ.get("SEED_TASKS_GLOB", "/configs/tasks/*.yaml")
    for path in sorted(glob.glob(pattern)):
        try:
            spec = yaml.safe_load(open(path))
        except Exception as e:  # noqa: BLE001
            log.warning("сид %s не читается: %s", path, e)
            continue
        if not spec or "statement" not in spec:
            continue
        seed_id = spec.get("id") or os.path.basename(path)
        if q.seeded(seed_id):
            continue
        tid = q.create_task(spec["statement"], spec.get("kind", "open"),
                            spec.get("cap_usd"), source=f"seed:{seed_id}")
        q.mark_seeded(seed_id)
        log.info("сид-задача %s -> %s", seed_id, tid)


def _assignment_loop() -> None:
    while True:
        try:
            if not q.get_flag("stopped") and not q.get_flag("paused"):
                remaining = _remaining_budget()
                for agent in q.idle_agents(AGENTS):
                    tid = q.next_pending()
                    if tid is None:
                        break
                    cap = task_cap_for(q.agent_quality(agent), BASE_CAP, MIN_SHARE, remaining)
                    if cap <= 0:
                        q.requeue(tid)          # бюджет исчерпан — вернуть в очередь
                        break
                    task = q.get(tid)
                    if q.assign(tid, agent, cap):
                        emit("tasks.assignments", {"task_id": tid, "agent_id": agent,
                             "cap_usd": cap, "statement": task.get("statement", ""),
                             "kind": task.get("kind", "open")})
                        emit("journal.events", {"ts": time.time(), "agent_id": "orchestrator",
                             "task_id": tid, "action": "task_assigned",
                             "detail": f"{agent} cap=${cap} (q={q.agent_quality(agent)})"})
                        remaining -= cap
        except Exception as e:  # noqa: BLE001
            log.warning("assignment loop error: %s", e)
        time.sleep(5)


def _submissions_loop() -> None:
    from kafka import KafkaConsumer

    while True:
        try:
            c = KafkaConsumer("tasks.submissions",
                              bootstrap_servers=os.environ["KAFKA_BROKERS"].split(","),
                              value_deserializer=lambda v: json.loads(v.decode()),
                              group_id="orchestrator-sub", auto_offset_reset="earliest")
            for msg in c:
                q.mark_submitted(msg.value.get("task_id"))
        except Exception as e:  # noqa: BLE001
            log.warning("submissions loop error (retry 5s): %s", e)
            time.sleep(5)


def _verdicts_loop() -> None:
    from kafka import KafkaConsumer

    while True:
        try:
            c = KafkaConsumer("tasks.verdicts",
                              bootstrap_servers=os.environ["KAFKA_BROKERS"].split(","),
                              value_deserializer=lambda v: json.loads(v.decode()),
                              group_id="orchestrator-verdict", auto_offset_reset="earliest")
            for msg in c:
                v = msg.value
                tid = v.get("task_id")
                agent = v.get("agent_id")
                quality = float(v.get("quality") or 0.0)
                q.finalize(tid, v.get("verdict", "unsolved"), quality)
                if agent:
                    new_q = update_rolling_quality(q.agent_quality(agent), quality)
                    q.set_agent_quality(agent, new_q)
                # запись вердикта в ClickHouse (verdicts) — источник прогресса
                _verdict_trace(v)
        except Exception as e:  # noqa: BLE001
            log.warning("verdicts loop error (retry 5s): %s", e)
            time.sleep(5)


def _verdict_trace(v: dict) -> None:
    url = os.environ.get("CLICKHOUSE_URL")
    if not url:
        return
    row = {"ts": time.time(), "task_id": v.get("task_id", ""), "agent_id": v.get("agent_id", ""),
           "verdict": v.get("verdict", ""), "quality": float(v.get("quality") or 0.0),
           "reason": (v.get("reason") or "")[:500]}
    try:
        _http.post(url, params={"query": "INSERT INTO verdicts FORMAT JSONEachRow"},
                   content=json.dumps(row, ensure_ascii=False), timeout=10)
    except Exception as e:  # noqa: BLE001
        log.warning("verdict trace failed: %s", e)


@app.on_event("startup")
def _startup() -> None:
    _load_seeds()
    for fn in (_assignment_loop, _submissions_loop, _verdicts_loop):
        threading.Thread(target=fn, daemon=True).start()
    log.info("orchestrator готов, агенты: %s", AGENTS)
