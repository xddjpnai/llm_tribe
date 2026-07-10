"""Тонкий клиент budget-guard для арбитра (роль=arbiter). Дублируется намеренно:
у сервисов раздельные docker build-контексты, общий пакет усложнил бы сборку."""
from __future__ import annotations

import httpx


class LLMClient:
    def __init__(self, guard_url: str, role: str = "arbiter", timeout: float = 300.0):
        self._url = guard_url.rstrip("/")
        self._role = role
        self._http = httpx.Client(timeout=timeout)

    def chat(self, messages: list[dict], *, max_tokens: int = 2048) -> tuple[str, float]:
        """Возвращает (текст, стоимость). Арбитр вызывается редко — раз на задачу."""
        r = self._http.post(f"{self._url}/v1/chat", json={
            "agent_id": "arbiter", "task_id": None, "role": self._role,
            "messages": messages, "max_tokens": max_tokens,
        })
        r.raise_for_status()
        d = r.json()
        return (d.get("content") or ""), float(d.get("cost_usd", 0.0))
