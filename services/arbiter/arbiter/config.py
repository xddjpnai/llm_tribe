"""Конфигурация арбитра."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    budget_guard_url: str
    kafka_brokers: str
    clickhouse_url: str | None
    workspace: str = "/workspace"
    role: str = "arbiter"
    # Порог качества отчёта (0..1), ниже которого задача не может быть "решена"
    # даже при воспроизводимости. Настраиваемо.
    quality_threshold: float = 0.6
    repro_timeout_sec: int = 120

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            budget_guard_url=os.environ["BUDGET_GUARD_URL"],
            kafka_brokers=os.environ["KAFKA_BROKERS"],
            clickhouse_url=os.environ.get("CLICKHOUSE_URL"),
            quality_threshold=float(os.environ.get("ARBITER_QUALITY_THRESHOLD", "0.6")),
        )
