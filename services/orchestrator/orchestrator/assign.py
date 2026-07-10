"""Чистая логика распределения. Механика конкуренции: доля бюджета задачи,
достающаяся агенту, масштабируется его накопленным качеством (guard: дефицит
ресурса — часть механики, а не только внешний лимит).

Держим отдельно от Redis/шины, чтобы тестировать без инфраструктуры.
"""
from __future__ import annotations


def quality_share(quality: float, min_share: float) -> float:
    """Множитель cap'а по качеству агента (0..1). Пол min_share — чтобы отстающий
    агент не падал в ноль и мог выбраться. Топовый (q→1) получает полный cap."""
    q = max(0.0, min(1.0, quality))
    return max(min_share, q)


def task_cap_for(agent_quality: float, base_cap: float, min_share: float,
                 remaining_budget: float) -> float:
    """Итоговый cap на задачу для конкретного агента: база × доля-по-качеству,
    но не больше остатка общего бюджета (жёсткий потолок)."""
    cap = base_cap * quality_share(agent_quality, min_share)
    return round(min(cap, max(0.0, remaining_budget)), 4)


def update_rolling_quality(prev: float, new_q: float, alpha: float = 0.4) -> float:
    """EMA качества агента по вердиктам: свежий результат весит alpha."""
    return round((1 - alpha) * prev + alpha * max(0.0, min(1.0, new_q)), 4)
