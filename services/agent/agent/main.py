"""Точка входа агента: слушает назначения задач, гоняет луп, сдаёт результат.

Поток:
  tasks.assignments (для моего agent_id) → run_task → tasks.submissions → арбитр.
Плюс подписка на control.commands (пауза/стоп — kill-switch, guard #8).
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
from pathlib import Path

import httpx

from .config import Config
from .events import Bus
from .graph import run_task
from .llm import LLMClient
from .tools import ToolContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("agent.main")

_paused = threading.Event()
_stopped = threading.Event()


def _control_listener(cfg: Config, bus: Bus) -> None:
    """Реагирует на control.commands: pause/resume/stop для этого агента или для all."""
    try:
        from kafka import KafkaConsumer
    except Exception as e:  # noqa: BLE001
        log.warning("нет kafka-consumer для control: %s", e)
        return
    consumer = KafkaConsumer(
        "control.commands",
        bootstrap_servers=cfg.kafka_brokers.split(","),
        value_deserializer=lambda v: json.loads(v.decode()),
        group_id=f"control-{cfg.agent_id}",
        auto_offset_reset="latest",
    )
    for msg in consumer:
        cmd = msg.value
        if cmd.get("target") not in ("all", cfg.agent_id):
            continue
        action = cmd.get("action")
        log.warning("control command: %s", cmd)
        if action == "pause":
            _paused.set()
        elif action == "resume":
            _paused.clear()
        elif action == "stop":
            _stopped.set()
            _paused.set()


def _make_toolctx(cfg: Config, task_id: str | None, http: httpx.Client, bus: Bus) -> ToolContext:
    return ToolContext(
        agent_id=cfg.agent_id, task_id=task_id,
        workspace=Path(cfg.workspace), private=Path(cfg.private),
        search_tool_url=cfg.search_tool_url, selfmod_api_url=cfg.selfmod_api_url,
        cpu_models_url=cfg.cpu_models_url, http=http, audit=bus.audit,
    )


def main() -> None:
    cfg = Config.from_env()
    clickhouse_url = os.environ.get("CLICKHOUSE_URL")
    bus = Bus(cfg.agent_id, cfg.kafka_brokers, clickhouse_url)
    llm = LLMClient(cfg.budget_guard_url, cfg.agent_id, cfg.role)
    http = httpx.Client(timeout=60.0)

    signal.signal(signal.SIGTERM, lambda *_: _stopped.set())
    threading.Thread(target=_control_listener, args=(cfg, bus), daemon=True).start()

    from kafka import KafkaConsumer

    consumer = KafkaConsumer(
        "tasks.assignments",
        bootstrap_servers=cfg.kafka_brokers.split(","),
        value_deserializer=lambda v: json.loads(v.decode()),
        group_id=f"agent-{cfg.agent_id}",   # у каждого агента своя группа: видит назначения себе
        auto_offset_reset="earliest",
        enable_auto_commit=True,
    )
    log.info("agent %s (role=%s) готов, слушаю назначения", cfg.agent_id, cfg.role)
    bus.emit("journal.events", {"action": "agent_online", "detail": f"role={cfg.role}"})

    for msg in consumer:
        if _stopped.is_set():
            break
        task = msg.value
        if task.get("agent_id") != cfg.agent_id:
            continue
        if _paused.is_set():
            log.info("на паузе, пропускаю назначение %s", task.get("task_id"))
            continue

        task_id = task.get("task_id")
        log.info("взял задачу %s (cap=$%s)", task_id, task.get("cap_usd"))
        bus.audit(task_id=task_id, action="task_start", detail=task.get("statement", "")[:500])
        tctx = _make_toolctx(cfg, task_id, http, bus)
        try:
            state = run_task(task, llm, tctx, bus, cfg.max_steps)
        except Exception as e:  # noqa: BLE001
            log.exception("задача %s упала", task_id)
            bus.emit("tasks.submissions", {"task_id": task_id, "agent_id": cfg.agent_id,
                     "summary": f"agent crashed: {e}", "artifact_ref": "", "branch": "",
                     "failed": True})
            continue

        bus.audit(task_id=task_id, action="task_end",
                  detail=f"stop={state.stop_reason} cost=${state.total_cost:.4f}",
                  cost_usd=state.total_cost)
        sub = state.submission or {"summary": f"no submission ({state.stop_reason})",
                                   "artifact_path": "", "branch": f"agent/{cfg.agent_id}"}
        bus.emit("tasks.submissions", {
            "task_id": task_id, "agent_id": cfg.agent_id,
            "summary": sub["summary"], "artifact_ref": sub.get("artifact_path", ""),
            "branch": sub.get("branch", ""), "stop_reason": state.stop_reason,
            "cost_usd": round(state.total_cost, 4),
        })
        bus.flush()

    log.info("agent %s завершается", cfg.agent_id)
    llm.close()
    bus.flush()


if __name__ == "__main__":
    main()
