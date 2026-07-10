"""LLM-клиент бота для распознавания намерения из свободного текста. Идёт через
budget-guard (роль comms — дешёвая модель), расход учитывается. При исчерпании
бюджета/троттлинге возвращает None — бот деградирует, не падает.

Это ИЗМЕНЯЕМЫЙ слой: агенты вправе улучшать распознавание. Но контроль (auth,
/kill, /user) от этого не зависит — он в защищённом ядре ДО вызова роутера.
"""
from __future__ import annotations

import httpx


class LLMClient:
    def __init__(self, guard_url: str, timeout: float = 60.0):
        self._url = guard_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout)

    def chat(self, messages: list[dict], max_tokens: int = 500) -> str | None:
        try:
            r = self._http.post(f"{self._url}/v1/chat", json={
                "agent_id": "comms-bot", "task_id": None, "role": "comms",
                "messages": messages, "max_tokens": max_tokens,
            })
        except httpx.HTTPError:
            return None
        if r.status_code in (402, 429):      # бюджет исчерпан/троттлинг
            return None
        if r.status_code != 200:
            return None
        return r.json().get("content")
