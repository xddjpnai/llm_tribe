"""Защита от самомодификации критичных путей. Список зашит в образ selfmod-api
(агенты его не монтируют и не патчат), поэтому агент не может ни изменить
защищённый код, ни снять саму защиту.

Правило: любой patch, чей diff касается защищённого пути, отклоняется ДО применения.
Защищаем то, что даёт контроль над системой и над доступом к ней.
"""
from __future__ import annotations

import re

# Подстроки путей, которые агентам запрещено менять (kill-switch, управление
# доступом, auth, цикл/клиент бота, деньги/ключи, сам механизм защиты, креды).
PROTECTED_PATHS = [
    "secrets/",                              # файл кредов
    "credentials",                           # креды по имени
    ".env",                                  # любые env-креды
    "comms_bot/bot/protected.py",            # ядро auth + /kill + /user
    "comms_bot/bot/app.py",                  # порядок проверок бота
    "comms_bot/bot/telegram.py",             # клиент TG (риск эксфильтрации)
    "orchestrator/orchestrator/app.py",      # бэкенд /v1/kill
    "budget_guard/",                         # деньги и ключи провайдеров
    "selfmod_api/",                          # сам механизм защиты
    "authz",                                 # хранилище участников
]

_PATH_RE = re.compile(r"^(?:\+\+\+|---)\s+(?:[ab]/)?(\S+)", re.MULTILINE)


def _paths_in_diff(diff: str) -> set[str]:
    paths = set()
    for m in _PATH_RE.finditer(diff):
        p = m.group(1)
        if p and p != "/dev/null":
            paths.add(p)
    return paths


def blocked_path(diff: str) -> str | None:
    """Возвращает первый защищённый путь, затронутый diff'ом, либо None."""
    for path in _paths_in_diff(diff):
        low = path.lower()
        for prot in PROTECTED_PATHS:
            if prot.lower() in low:
                return path
    return None
