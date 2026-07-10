"""Клиент budget-guard. Агент НИКОГДА не ходит к провайдерам напрямую — только
сюда (у него нет ключей провайдеров); budget-guard резолвит роль → модель."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class ChatResult:
    content: str | None
    tool_calls: list[dict[str, Any]]
    cost_usd: float
    model: str
    fell_back: bool


class LLMClient:
    def __init__(self, guard_url: str, agent_id: str, role: str, timeout: float = 300.0):
        self._url = guard_url.rstrip("/")
        self._agent_id = agent_id
        self._role = role
        self._http = httpx.Client(timeout=timeout)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        task_id: str | None,
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
    ) -> ChatResult:
        body: dict[str, Any] = {
            "agent_id": self._agent_id,
            "task_id": task_id,
            "role": self._role,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        resp = self._http.post(f"{self._url}/v1/chat", json=body)
        resp.raise_for_status()
        d = resp.json()
        return ChatResult(
            content=d.get("content"),
            tool_calls=d.get("tool_calls") or [],
            cost_usd=float(d.get("cost_usd", 0.0)),
            model=d.get("model", "?"),
            fell_back=bool(d.get("fell_back", False)),
        )

    def close(self) -> None:
        self._http.close()
