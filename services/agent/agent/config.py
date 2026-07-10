"""Конфигурация агента из окружения (docker-compose передаёт значения)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    agent_id: str
    role: str                    # роль для budget-guard (резолвится в модель)
    budget_guard_url: str
    selfmod_api_url: str
    redis_url: str
    initial_tasks: str           # путь к seed-файлу с 3 стартовыми задачами
    workspace: str = "/workspace"
    private: str = "/private"
    # потолок шагов ReAct-луппа на задачу — страховка от зацикливания
    # (жёсткий стоп по деньгам делает budget-guard)
    max_steps: int = 60

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            agent_id=os.environ["AGENT_ID"],
            role=os.environ.get("AGENT_ROLE", "researcher"),
            budget_guard_url=os.environ["BUDGET_GUARD_URL"],
            selfmod_api_url=os.environ["SELFMOD_API_URL"],
            redis_url=os.environ["REDIS_URL"],
            initial_tasks=os.environ.get("INITIAL_TASKS", "/seed/initial_tasks.yaml"),
            max_steps=int(os.environ.get("AGENT_MAX_STEPS", "60")),
        )
