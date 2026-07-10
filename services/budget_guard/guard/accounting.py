"""Учёт бюджета в реальном времени (Redis). Два потока расхода, общий лимит.

  llm:total            — накопленные траты на LLM API ($)
  server:start_ts      — момент старта учёта аренды (accrual считается лениво)
  agent:{id}:{date}    — суточный расход агента ($)
  task:{id}            — накопленный расход на задачу ($)

state = f(total_spent / total_budget): ok < warn < throttle < hard_stop.
Инкременты атомарны (INCRBYFLOAT), поэтому конкурентные запросы агентов не гонятся.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .config import BudgetLimits


@dataclass
class BudgetSnapshot:
    llm_spent: float
    server_spent: float
    total_spent: float
    total_budget: float
    fraction: float
    state: str            # "ok" | "warn" | "throttle" | "hard_stop"


class Accounting:
    def __init__(self, redis_client, limits: BudgetLimits):
        self.r = redis_client
        self.limits = limits
        # зафиксировать старт accrual сервера один раз
        if not self.r.get("server:start_ts"):
            self.r.set("server:start_ts", str(time.time()))

    # --------------------------- accrual сервера ---------------------------
    def server_spent(self) -> float:
        start = float(self.r.get("server:start_ts") or time.time())
        days = max(0.0, (time.time() - start) / 86400.0)
        return round(days * (self.limits.server_monthly_usd / 30.0), 4)

    # --------------------------- чтение снимка -----------------------------
    def snapshot(self) -> BudgetSnapshot:
        llm = float(self.r.get("llm:total") or 0.0)
        server = self.server_spent()
        total = llm + server
        frac = total / self.limits.total_budget_usd if self.limits.total_budget_usd else 1.0
        if frac >= self.limits.hard_stop:
            state = "hard_stop"
        elif frac >= self.limits.throttle:
            state = "throttle"
        elif frac >= self.limits.warn:
            state = "warn"
        else:
            state = "ok"
        return BudgetSnapshot(round(llm, 4), server, round(total, 4),
                              self.limits.total_budget_usd, round(frac, 4), state)

    def agent_daily(self, agent_id: str) -> float:
        key = f"agent:{agent_id}:{time.strftime('%Y-%m-%d')}"
        return float(self.r.get(key) or 0.0)

    def task_spent(self, task_id: str) -> float:
        return float(self.r.get(f"task:{task_id}") or 0.0)

    # ------------------------ per-task cap (регистрирует оркестратор) ------------------------
    def set_task_cap(self, task_id: str, cap_usd: float) -> None:
        # cap живёт дольше задачи (TTL сутки), чтобы не потеряться при паузах
        self.r.set(f"taskcap:{task_id}", str(cap_usd))
        self.r.expire(f"taskcap:{task_id}", 60 * 60 * 24)

    def get_task_cap(self, task_id: str | None, default: float) -> float:
        """Фактический cap задачи (конкурентно масштабированный оркестратором) или
        дефолт из budget.yaml, если оркестратор его не зарегистрировал."""
        if not task_id:
            return default
        v = self.r.get(f"taskcap:{task_id}")
        return float(v) if v else default

    # --------------------------- проверки допуска --------------------------
    def check_admission(self, agent_id: str, task_id: str | None,
                        task_cap: float) -> tuple[bool, str, float]:
        """Можно ли ВООБЩЕ делать запрос. Возвращает (ok, reason, retry_after_sec).
        reason при отказе: hard_stop | task_cap | agent_cap; при троттлинге ok=False
        с reason='throttle' и retry_after>0."""
        snap = self.snapshot()
        if snap.state == "hard_stop":
            return False, "hard_stop", 0.0
        if task_id and self.task_spent(task_id) >= task_cap:
            return False, "task_cap", 0.0
        if self.agent_daily(agent_id) >= self.limits.per_agent_daily_cap_usd:
            return False, "agent_cap", 0.0
        if snap.state == "throttle":
            # мягкий тормоз: пускаем, но просим агента подождать (разрежаем поток)
            return False, "throttle", 5.0
        return True, "ok", 0.0

    # --------------------------- запись расхода ----------------------------
    def record(self, agent_id: str, task_id: str | None, cost: float) -> BudgetSnapshot:
        self.r.incrbyfloat("llm:total", cost)
        day_key = f"agent:{agent_id}:{time.strftime('%Y-%m-%d')}"
        self.r.incrbyfloat(day_key, cost)
        self.r.expire(day_key, 60 * 60 * 48)   # суточные ключи живут 2 суток
        if task_id:
            self.r.incrbyfloat(f"task:{task_id}", cost)
        return self.snapshot()

    def per_request_cap(self) -> float:
        return self.limits.per_request_cap_usd
