"""ИЗМЕНЯЕМЫЙ слой (агенты вправе эволюционировать). Понимает СВОБОДНЫЙ ТЕКСТ:
пишешь боту обычным сообщением — он распознаёт намерение (поставить задачу,
спросить статус/журнал/бюджет) и делает. Slash-шорткаты (/status, /journal,
/budget, /help) оставлены как удобство; /addtask убран — просто опиши задачу.

Сюда попадают ТОЛЬКО аутентифицированные пользователи и ТОЛЬКО не-защищённые
запросы: auth, /kill и /user обрабатывает bot/protected.py ДО этого модуля.

БЕЗОПАСНОСТЬ: этот слой НЕ умеет останавливать/убивать/паузить агентов и
управлять доступом. Роутер намеренно не имеет таких действий, а services здесь
не содержит kill — kill доступен ТОЛЬКО как защищённая команда /kill.
"""
from __future__ import annotations

import json
import re

HELP = (
    "Просто напиши мне обычным текстом, что нужно — я пойму. Примеры:\n"
    "• «поставь задачу: найти cap set побольше для n=4..7, бюджет $4»\n"
    "• «что сейчас происходит?» / «статус»\n"
    "• «что было по задаче task-123?»\n"
    "• «сколько потрачено?»\n\n"
    "Защищённые команды (только явно, не текстом):\n"
    "/kill [all|agent-1] · /pause · /resume — остановка агентов\n"
    "/user add|remove <id> | list — доступ (только владелец)"
)

_ROUTER_SYSTEM = (
    "You are the intent router of a research-collegium Telegram bot. Map the user's "
    "free-text message to ONE action and extract parameters. Reply with ONLY a JSON "
    "object, no prose.\n"
    "Actions:\n"
    "  add_task — user wants to queue a research task. Extract: statement (clean task "
    "text), kind (one of exact|maximize|open, default open), cap (float USD if the user "
    "named a budget, else null).\n"
    "  status  — user asks about system/queue/agents state.\n"
    "  journal — user asks what happened / the log; extract task_id if mentioned else null.\n"
    "  budget  — user asks about spend/budget.\n"
    "  help    — user asks what they can do.\n"
    "  none    — anything else, OR ANY request to stop/pause/kill agents or manage user "
    "access. You MUST NOT and CANNOT stop/kill/pause anything or add/remove users; for "
    "such requests use action 'none' and put a short note telling them to use the "
    "explicit /kill or /user command.\n"
    'Reply JSON: {"action":"...","statement":"...","kind":"open","cap":null,'
    '"task_id":null,"note":"..."}. Answer notes in Russian.'
)


def handle_message(text: str, user_id: int, services, llm) -> str | None:
    """Обрабатывает запрос аутентифицированного пользователя: slash-шорткат или
    свободный текст через LLM-роутер. Возвращает ответ или None."""
    t = text.strip()
    if t.startswith("/"):
        return _slash(t, services)
    return _route_freeform(t, services, llm)


def _slash(text: str, services) -> str | None:
    parts = text.split(maxsplit=1)
    cmd = parts[0].lstrip("/").lower().split("@")[0]
    rest = parts[1] if len(parts) > 1 else ""
    try:
        if cmd == "help":
            return HELP
        if cmd == "status":
            return _fmt_status(services.status())
        if cmd == "budget":
            return _fmt_budget(services.budget())
        if cmd == "journal":
            return services.journal(rest.strip() or None)
        if cmd == "addtask":
            return "команда /addtask больше не нужна — просто опиши задачу текстом."
        return f"неизвестная команда /{cmd}. " + HELP
    except Exception as e:  # noqa: BLE001
        return f"ошибка /{cmd}: {type(e).__name__}: {e}"


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _route_freeform(text: str, services, llm) -> str | None:
    reply = llm.chat([{"role": "system", "content": _ROUTER_SYSTEM},
                      {"role": "user", "content": text}])
    if reply is None:
        return ("сейчас не могу разобрать запрос (бюджет LLM исчерпан или троттлинг). "
                "Пока доступны шорткаты: /status, /journal, /budget, /help.")
    intent = _parse_json(reply)
    action = (intent.get("action") or "").lower()
    try:
        if action == "add_task":
            statement = (intent.get("statement") or "").strip()
            if not statement:
                return "не понял, какую задачу поставить — опиши подробнее."
            kind = intent.get("kind") if intent.get("kind") in ("exact", "maximize", "open") else "open"
            cap = intent.get("cap")
            cap = float(cap) if isinstance(cap, (int, float)) else None
            task_id = services.add_task(statement, kind, cap)
            return f"✅ задача в очереди: {task_id} (kind={kind}, cap={cap or 'default'})\n{statement}"
        if action == "status":
            return _fmt_status(services.status())
        if action == "journal":
            return services.journal((intent.get("task_id") or "").strip() or None)
        if action == "budget":
            return _fmt_budget(services.budget())
        if action == "help":
            return HELP
        # none / неизвестно — вернуть подсказку роутера (в т.ч. про /kill, /user)
        return intent.get("note") or ("не понял запрос. " + HELP)
    except Exception as e:  # noqa: BLE001
        return f"ошибка выполнения: {type(e).__name__}: {e}"


def _fmt_status(st: dict) -> str:
    q = st.get("queue", {})
    agents = st.get("agents", {})
    lines = [f"очередь: {q}", "агенты:"]
    for a, s in agents.items():
        lines.append(f"  {a}: {s}")
    return "\n".join(lines) if agents or q else str(st)


def _fmt_budget(b: dict) -> str:
    # только LLM-расход; аренда сервера не учитывается
    return (f"LLM-бюджет: ${b.get('spent_total_usd', 0):.2f} / ${b.get('budget_usd', 0):.0f} "
            f"({b.get('fraction', 0) * 100:.1f}%), state={b.get('state')}")
