"""Точка входа агента. Никакой внешней оркестрации: агент читает стартовые задачи
из seed-файла, работает их через ReAct-луп + self-mod, затем продолжает задачами,
которые приходят через канал, построенный им самим (конвенция: Redis-список `tasks`).

Kill — на уровне хоста (scripts/kill.sh = docker stop), в процессе не обрабатывается:
агент не может отменить собственную остановку.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
import redis
import yaml

from .config import Config
from .events import Bus
from .graph import run_task
from .llm import LLMClient
from .tools import ToolContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("agent.main")


def _toolctx(cfg: Config, task_id: str | None, http: httpx.Client, bus: Bus) -> ToolContext:
    return ToolContext(
        agent_id=cfg.agent_id, task_id=task_id,
        workspace=Path(cfg.workspace), private=Path(cfg.private),
        selfmod_api_url=cfg.selfmod_api_url, http=http, audit=bus.audit,
    )


def _load_initial(path: str) -> list[dict]:
    try:
        data = yaml.safe_load(open(path))
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        log.warning("нет стартовых задач (%s): %s", path, e)
        return []


def _run_one(task: dict, cfg: Config, llm: LLMClient, http: httpx.Client, bus: Bus) -> None:
    task_id = task.get("id") or task.get("task_id") or f"task-{int(time.time())}"
    bus.audit(task_id=task_id, action="task_start", detail=(task.get("statement") or "")[:500])
    ctx = _toolctx(cfg, task_id, http, bus)
    try:
        state = run_task(task, llm, ctx, bus, cfg.max_steps)
    except Exception as e:  # noqa: BLE001
        log.exception("задача %s упала", task_id)
        bus.audit(task_id=task_id, action="task_error", detail=str(e))
        return
    bus.audit(task_id=task_id, action="task_end",
              detail=f"stop={state.stop_reason} cost=${state.total_cost:.4f}",
              cost_usd=state.total_cost)


def main() -> None:
    cfg = Config.from_env()
    bus = Bus(cfg.agent_id, cfg.redis_url)
    llm = LLMClient(cfg.budget_guard_url, cfg.agent_id, cfg.role)
    http = httpx.Client(timeout=60.0)
    r = redis.from_url(cfg.redis_url, decode_responses=True)
    bus.emit("agent", {"action": "online"})
    log.info("agent %s online", cfg.agent_id)

    # 1) Стартовые задачи (построить журнал / связь / приём задач). Клейм через
    #    Redis SETNX, чтобы несколько агентов не делали одно и то же дважды.
    for task in _load_initial(cfg.initial_tasks):
        tid = task.get("id") or "seed"
        if r.set(f"claim:{tid}", cfg.agent_id, nx=True, ex=86400):
            log.info("agent %s взял стартовую задачу %s", cfg.agent_id, tid)
            _run_one(task, cfg, llm, http, bus)

    # 2) Дальше — задачи, которые агент принимает через ПОСТРОЕННЫЙ ИМ канал.
    #    Конвенция: постановки кладутся в Redis-список `tasks` (blpop распределяет
    #    их между агентами — один элемент одному агенту).
    log.info("agent %s: стартовые задачи отработаны; слушаю очередь `tasks`", cfg.agent_id)
    while True:
        item = r.blpop("tasks", timeout=30)
        if item is None:
            continue
        try:
            task = json.loads(item[1])
        except Exception:  # noqa: BLE001
            task = {"id": None, "statement": item[1]}
        _run_one(task, cfg, llm, http, bus)


if __name__ == "__main__":
    main()
