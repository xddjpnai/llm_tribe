"""Учёт LLM-расхода (Redis). Общий потолок НЕ enforce'ится — его отслеживает
владелец сам. Единственная рамка для агента — на ОДНО действие (клампинг
max_tokens в app.py). Здесь только накопительный счётчик для /v1/budget."""
from __future__ import annotations


class Accounting:
    def __init__(self, redis_client):
        self.r = redis_client

    def record(self, cost: float) -> None:
        self.r.incrbyfloat("llm:total", cost)

    def total(self) -> float:
        return round(float(self.r.get("llm:total") or 0.0), 4)
