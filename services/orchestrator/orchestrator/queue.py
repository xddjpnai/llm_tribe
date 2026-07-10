"""Очередь исследовательских задач как state machine поверх Redis (переживает
рестарт). Переходы явные:

  queued → assigned → in_progress → submitted → (solved | unsolved)

Фоновые задачи (связь/журнал) сюда НЕ попадают — они долгоживущие сервисы.
"""
from __future__ import annotations

import json
import time
import uuid

STATES = ("queued", "assigned", "in_progress", "submitted", "solved", "unsolved")
_ALLOWED = {
    "queued": {"assigned"},
    "assigned": {"in_progress", "submitted"},
    "in_progress": {"submitted"},
    "submitted": {"solved", "unsolved"},
    "solved": set(),
    "unsolved": set(),
}


class TaskQueue:
    def __init__(self, redis_client):
        self.r = redis_client

    # ------------------------------- создание/чтение -------------------------------
    def create_task(self, statement: str, kind: str, cap_usd: float | None,
                    task_id: str | None = None, source: str = "api") -> str:
        tid = task_id or "task-" + uuid.uuid4().hex[:10]
        self.r.hset(f"task:{tid}", mapping={
            "id": tid, "state": "queued", "statement": statement, "kind": kind,
            "cap_usd": "" if cap_usd is None else str(cap_usd),
            "assigned_to": "", "quality": "", "source": source,
            "created_at": str(time.time()),
        })
        self.r.sadd("tasks:all", tid)
        self.r.rpush("tasks:pending", tid)
        return tid

    def get(self, tid: str) -> dict:
        return self.r.hgetall(f"task:{tid}")

    def seeded(self, seed_id: str) -> bool:
        """Идемпотентность сид-загрузки: не создавать одну и ту же сид-задачу дважды."""
        return bool(self.r.sismember("tasks:seeded", seed_id))

    def mark_seeded(self, seed_id: str) -> None:
        self.r.sadd("tasks:seeded", seed_id)

    # ------------------------------- переходы -------------------------------
    def _set_state(self, tid: str, new: str) -> bool:
        cur = self.r.hget(f"task:{tid}", "state")
        if cur is None:
            return False
        if new not in _ALLOWED.get(cur, set()):
            return False
        self.r.hset(f"task:{tid}", "state", new)
        return True

    def next_pending(self) -> str | None:
        return self.r.lpop("tasks:pending")

    def requeue(self, tid: str) -> None:
        """Вернуть задачу в очередь (например, агент на паузе — назначение отменено)."""
        self.r.hset(f"task:{tid}", mapping={"state": "queued", "assigned_to": ""})
        self.r.rpush("tasks:pending", tid)

    def assign(self, tid: str, agent_id: str, cap_usd: float) -> bool:
        if not self._set_state(tid, "assigned"):
            return False
        self.r.hset(f"task:{tid}", mapping={"assigned_to": agent_id, "cap_usd": str(cap_usd)})
        self.r.hset("agents:busy", agent_id, tid)
        return True

    def mark_in_progress(self, tid: str) -> None:
        self._set_state(tid, "in_progress")

    def mark_submitted(self, tid: str) -> None:
        self._set_state(tid, "submitted")

    def finalize(self, tid: str, verdict: str, quality: float) -> None:
        # submitted -> solved|unsolved; допускаем и assigned/in_progress -> для устойчивости
        cur = self.r.hget(f"task:{tid}", "state")
        if cur in ("assigned", "in_progress"):
            self.r.hset(f"task:{tid}", "state", "submitted")
        target = "solved" if verdict == "solved" else "unsolved"
        self.r.hset(f"task:{tid}", mapping={"state": target, "quality": str(quality)})
        agent = self.r.hget(f"task:{tid}", "assigned_to")
        if agent:
            self.r.hset("agents:busy", agent, "")   # освобождаем агента

    # ------------------------------- агенты -------------------------------
    def agent_busy_task(self, agent_id: str) -> str:
        return self.r.hget("agents:busy", agent_id) or ""

    def idle_agents(self, all_agents: list[str]) -> list[str]:
        return [a for a in all_agents if not self.agent_busy_task(a)]

    def agent_quality(self, agent_id: str) -> float:
        v = self.r.hget("agents:quality", agent_id)
        return float(v) if v else 0.5   # старт с нейтрального 0.5

    def set_agent_quality(self, agent_id: str, q: float) -> None:
        self.r.hset("agents:quality", agent_id, str(q))

    # ------------------------------- статус -------------------------------
    def status(self, all_agents: list[str]) -> dict:
        counts: dict[str, int] = {s: 0 for s in STATES}
        for tid in self.r.smembers("tasks:all"):
            st = self.r.hget(f"task:{tid}", "state")
            if st in counts:
                counts[st] += 1
        agents = {a: {"busy_task": self.agent_busy_task(a) or None,
                      "quality": self.agent_quality(a)} for a in all_agents}
        return {"queue": counts, "pending_len": self.r.llen("tasks:pending"), "agents": agents}

    # ------------------------------- kill-switch флаги -------------------------------
    def set_flag(self, name: str, value: bool) -> None:
        self.r.set(f"orch:{name}", "1" if value else "0")

    def get_flag(self, name: str) -> bool:
        return self.r.get(f"orch:{name}") == "1"
