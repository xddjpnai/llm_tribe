"""Минимальная телеметрия selfmod: в stdout-лог. Внешней шины/БД нет."""
from __future__ import annotations

import logging

log = logging.getLogger("selfmod.events")


def journal(agent_id: str, action: str, detail: str) -> None:
    log.info("[journal] %s %s: %s", agent_id, action, detail)


def audit(agent_id: str, task_id: str, action: str, detail: str) -> None:
    log.info("[audit] %s task=%s %s: %s", agent_id, task_id, action, detail)
