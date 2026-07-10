"""ИЗМЕНЯЕМЫЕ обработчики команд (агенты вправе их эволюционировать).

Сюда попадают ТОЛЬКО уже аутентифицированные пользователи и ТОЛЬКО не-защищённые
команды — проверку участника и защищённые команды (/kill, /user) обрабатывает
bot/protected.py ДО вызова этого модуля. Поэтому здесь auth-гейта нет намеренно:
его нельзя ослабить, редактируя этот файл.
"""
from __future__ import annotations

import shlex

# help — информационный; агент может переформатировать. Защищённые команды всё равно
# работают независимо от того, упомянуты ли они здесь (их обрабатывает protected.py).
HELP = (
    "Команды:\n"
    "/status                    — состояние очереди и агентов\n"
    "/budget                    — расход бюджета (LLM + сервер)\n"
    "/journal [task_id]         — бортовой журнал\n"
    "/addtask [kind=exact|maximize|open] [cap=<usd>] <постановка>\n"
    "/kill [all|agent-1|...]    — kill-switch (стоп)\n"
    "/pause [all|agent-1|...]   — пауза\n"
    "/resume [all|agent-1|...]  — снять паузу\n"
    "/user add|remove <id> | list — управление доступом (только владелец)\n"
    "/help"
)


def handle_command(text: str, user_id: int, services) -> str | None:
    """Обрабатывает НЕ-защищённые команды аутентифицированного пользователя.
    Возвращает текст ответа или None."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lstrip("/").lower().split("@")[0]
    rest = parts[1] if len(parts) > 1 else ""
    try:
        if cmd == "help":
            return HELP
        if cmd == "addtask":
            return _add_task(rest, services)
        if cmd == "status":
            return _fmt_status(services.status())
        if cmd == "budget":
            return _fmt_budget(services.budget())
        if cmd == "journal":
            return services.journal(rest.strip() or None)
        return f"неизвестная команда: /{cmd}\n{HELP}"
    except Exception as e:  # noqa: BLE001 — вернуть пользователю, не падать
        return f"ошибка выполнения /{cmd}: {type(e).__name__}: {e}"


def _add_task(rest: str, services) -> str:
    if not rest.strip():
        return "нужна постановка: /addtask <текст>"
    kind, cap, statement_tokens = "open", None, []
    for tok in shlex.split(rest):
        if tok.startswith("kind=") and not statement_tokens:
            kind = tok[len("kind="):]
        elif tok.startswith("cap=") and not statement_tokens:
            try:
                cap = float(tok[len("cap="):])
            except ValueError:
                return f"cap должен быть числом: {tok}"
        else:
            statement_tokens.append(tok)
    statement = " ".join(statement_tokens).strip()
    if not statement:
        return "после опций нужна постановка задачи"
    if kind not in ("exact", "maximize", "open"):
        return f"kind должен быть exact|maximize|open, получено: {kind}"
    task_id = services.add_task(statement, kind, cap)
    return f"задача добавлена в очередь: {task_id} (kind={kind}, cap={cap or 'default'})"


def _fmt_status(st: dict) -> str:
    q = st.get("queue", {})
    agents = st.get("agents", {})
    lines = [f"очередь: {q}", "агенты:"]
    for a, s in agents.items():
        lines.append(f"  {a}: {s}")
    return "\n".join(lines) if agents or q else str(st)


def _fmt_budget(b: dict) -> str:
    return (f"бюджет: ${b.get('spent_total_usd', 0):.2f} / ${b.get('budget_usd', 0):.0f} "
            f"({b.get('fraction', 0) * 100:.1f}%), state={b.get('state')}\n"
            f"  LLM: ${b.get('llm_spent_usd', 0):.2f} | сервер: ${b.get('server_spent_usd', 0):.2f}")
