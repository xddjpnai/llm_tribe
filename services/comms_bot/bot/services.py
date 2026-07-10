"""Обёртки над control-plane сервисами (оркестратор, журнал, budget-guard) для
ИЗМЕНЯЕМОГО слоя бота. Намеренно НЕТ метода kill — kill доступен только в
защищённом ядре (bot/protected.py), которое ходит в оркестратор напрямую.
Так изменяемый слой/LLM-роутер физически не может остановить систему."""
from __future__ import annotations

import httpx


class Services:
    def __init__(self, orchestrator_url: str, journal_url: str, budget_guard_url: str):
        self._orch = orchestrator_url.rstrip("/")
        self._journal = journal_url.rstrip("/")
        self._guard = budget_guard_url.rstrip("/")
        self._http = httpx.Client(timeout=30.0)

    def add_task(self, statement: str, kind: str, cap_usd: float | None) -> str:
        body = {"statement": statement, "kind": kind}
        if cap_usd is not None:
            body["cap_usd"] = cap_usd
        r = self._http.post(f"{self._orch}/v1/tasks", json=body)
        r.raise_for_status()
        return r.json().get("task_id", "?")

    def status(self) -> dict:
        r = self._http.get(f"{self._orch}/v1/status")
        r.raise_for_status()
        return r.json()

    def journal(self, task_id: str | None) -> str:
        params = {"task_id": task_id} if task_id else {}
        r = self._http.get(f"{self._journal}/v1/journal", params=params)
        r.raise_for_status()
        return r.json().get("markdown", "(журнал пуст)")

    def budget(self) -> dict:
        r = self._http.get(f"{self._guard}/v1/budget")
        r.raise_for_status()
        return r.json()
