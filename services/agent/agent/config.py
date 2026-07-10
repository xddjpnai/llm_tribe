"""Конфигурация агента из окружения (docker-compose передаёт значения)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    agent_id: str
    role: str
    budget_guard_url: str
    selfmod_api_url: str
    search_tool_url: str
    redis_url: str
    kafka_brokers: str
    workspace: str = "/workspace"
    private: str = "/private"
    # Потолок шагов ReAct-луппа на задачу — страховка от зацикливания
    # (жёсткий стоп по деньгам делает budget-guard, это дополнительный предел).
    max_steps: int = 60

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            agent_id=os.environ["AGENT_ID"],
            role=os.environ["AGENT_ROLE"],
            budget_guard_url=os.environ["BUDGET_GUARD_URL"],
            selfmod_api_url=os.environ["SELFMOD_API_URL"],
            search_tool_url=os.environ["SEARCH_TOOL_URL"],
            redis_url=os.environ["REDIS_URL"],
            kafka_brokers=os.environ["KAFKA_BROKERS"],
            max_steps=int(os.environ.get("AGENT_MAX_STEPS", "60")),
        )
