"""Минимальная телеметрия: события в stdout-лог + Redis-список `events`
(агент-построенный журнал может его консьюмить). Никакой внешней шины/БД —
их агенты строят себе сами при необходимости."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("agent.events")


class Bus:
    def __init__(self, agent_id: str, redis_url: str | None = None):
        self.agent_id = agent_id
        self._r = None
        if redis_url:
            try:
                import redis  # noqa: PLC0415

                self._r = redis.from_url(redis_url, decode_responses=True)
            except Exception as e:  # noqa: BLE001
                log.warning("redis недоступен, события только в лог: %s", e)

    def emit(self, topic: str, payload: dict[str, Any]) -> None:
        payload = {"ts": time.time(), "agent_id": self.agent_id, "topic": topic, **payload}
        line = json.dumps(payload, ensure_ascii=False)
        log.info("[%s] %s", topic, line)
        if self._r is not None:
            try:
                self._r.rpush("events", line)
                self._r.ltrim("events", -10000, -1)   # держим последние 10k
            except Exception as e:  # noqa: BLE001
                log.warning("redis rpush failed: %s", e)

    def audit(self, *, task_id: str | None, action: str, detail: str, cost_usd: float = 0.0) -> None:
        self.emit("audit", {"task_id": task_id or "", "action": action,
                            "detail": detail[:2000], "cost_usd": cost_usd})

    def flush(self) -> None:
        pass
