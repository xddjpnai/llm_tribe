"""Генерация нарратива-саммари по сырым событиям через budget-guard (роль journal,
дешёвая суммаризационная модель). build_prompt — чистая, тестируется офлайн."""
from __future__ import annotations

import json
from typing import Any

import httpx

_SYSTEM = (
    "You are the flight recorder of an autonomous multi-agent research system. "
    "Given a chronological list of raw events for one scope (the whole system, one "
    "agent, or one task), write a short human-readable narrative (3-6 sentences) of "
    "what happened and why: what was attempted, what worked or failed, and the current "
    "state. Be concrete, reference task/agent ids. This is read as a story, not a log. "
    "Answer in Russian."
)


def build_prompt(scope: str, events: list[dict[str, Any]]) -> list[dict[str, str]]:
    dump = "\n".join(json.dumps(e, ensure_ascii=False) for e in events)
    user = f"Scope: {scope}\nNew events ({len(events)}):\n{dump}\n\nWrite the narrative summary."
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def summarize(guard_url: str, scope: str, events: list[dict[str, Any]],
              http: httpx.Client | None = None) -> str:
    if not events:
        return ""
    client = http or httpx.Client(timeout=120.0)
    body = {
        "agent_id": "journal",       # фоновый сервис; per-agent кап не применяется к нему
        "task_id": None,
        "role": "journal",
        "messages": build_prompt(scope, events),
        "max_tokens": 500,
    }
    r = client.post(f"{guard_url.rstrip('/')}/v1/chat", json=body)
    if r.status_code in (402, 429):
        # бюджет исчерпан/троттлинг — не суммируем, вернём заглушку, курсор НЕ двигаем
        return ""
    r.raise_for_status()
    return r.json().get("content") or ""
