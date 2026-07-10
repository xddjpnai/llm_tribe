"""Фильтр входящих событий шины -> текст уведомления админу. Чистая функция,
тестируется офлайн. Пушим только ключевые события (иначе шум): вердикты по
задачам, срабатывания budget-guard, аномалии, активацию kill-switch."""
from __future__ import annotations

from typing import Any

# действия из journal.events, которые стоят уведомления
_NOTIFY_ACTIONS = {"budget_state", "anomaly", "kill", "killswitch", "selfmod_candidate_ready"}


def should_notify(topic: str, event: dict[str, Any]) -> str | None:
    if topic == "tasks.verdicts":
        v = event.get("verdict")
        icon = "✅" if v == "solved" else "❌"
        return (f"{icon} задача {event.get('task_id')} — {v}\n"
                f"агент: {event.get('agent_id')} | качество: {event.get('quality')}\n"
                f"причина: {event.get('reason', '')[:300]}")

    if topic == "journal.events":
        action = event.get("action")
        if action not in _NOTIFY_ACTIONS:
            return None
        return (f"⚠️ {action}: {event.get('detail', '')[:300]}\n"
                f"источник: {event.get('agent_id', '?')}")

    if topic == "control.commands":
        # эхо активации kill-switch (в т.ч. из скрипта, не только из бота)
        return (f"🛑 команда управления: {event.get('action')} -> {event.get('target')}")

    return None
