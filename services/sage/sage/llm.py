"""Клиент budget-guard для мудреца (роль sage — другой вендор, чем у агентов,
чтобы судья не был предвзят к их же модели)."""
from __future__ import annotations

import httpx


class LLMClient:
    def __init__(self, guard_url: str, role: str = "sage", timeout: float = 300.0):
        self._url = guard_url.rstrip("/")
        self._role = role
        self._http = httpx.Client(timeout=timeout)

    def chat(self, messages: list[dict], max_tokens: int = 2048) -> tuple[str, float]:
        r = self._http.post(f"{self._url}/v1/chat", json={
            "agent_id": "sage", "task_id": None, "role": self._role,
            "messages": messages, "max_tokens": max_tokens})
        r.raise_for_status()
        d = r.json()
        return (d.get("content") or ""), float(d.get("cost_usd", 0.0))
