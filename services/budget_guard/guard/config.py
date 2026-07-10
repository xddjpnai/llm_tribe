"""Загрузка configs/model_routing.yaml и configs/budget.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import yaml


@dataclass(frozen=True)
class ModelSpec:
    name: str
    provider: str
    price_in: float          # $/1M input tokens
    price_out: float         # $/1M output tokens


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    api_key: str             # уже разрезолвленный из env (ключи только здесь, guard #1)
    protocol: str            # "openai" | "anthropic"


@dataclass
class Routing:
    providers: dict[str, ProviderSpec]
    models: dict[str, ModelSpec]
    roles: dict[str, dict[str, Any]]   # role -> {primary, fallbacks: [...]}

    def chain(self, role: str) -> list[ModelSpec]:
        """Primary + fallbacks как список ModelSpec (в порядке попыток)."""
        r = self.roles.get(role)
        if r is None:
            raise KeyError(f"неизвестная роль: {role}")
        names = [r["primary"], *r.get("fallbacks", [])]
        return [self.models[n] for n in names if n in self.models]


@dataclass
class BudgetLimits:
    total_budget_usd: float
    server_monthly_usd: float
    per_task_default_cap_usd: float
    per_agent_daily_cap_usd: float
    per_request_cap_usd: float
    warn: float
    throttle: float
    hard_stop: float


def load_routing(path: str = "/configs/model_routing.yaml") -> Routing:
    raw = yaml.safe_load(open(path))
    providers = {}
    for name, p in raw["providers"].items():
        key = os.environ.get(p["api_key_env"], "")
        providers[name] = ProviderSpec(name, p["base_url"].rstrip("/") if p.get("base_url") else "",
                                       key, p["protocol"])
    models = {n: ModelSpec(n, m["provider"], m["price_in_per_mtok"], m["price_out_per_mtok"])
              for n, m in raw["models"].items()}
    return Routing(providers=providers, models=models, roles=raw["roles"])


def load_budget(path: str = "/configs/budget.yaml") -> BudgetLimits:
    raw = yaml.safe_load(open(path))
    t = raw["thresholds"]
    llm = raw["llm"]
    override = os.environ.get("BUDGET_TOTAL_USD")
    return BudgetLimits(
        total_budget_usd=float(override) if override else float(raw["total_budget_usd"]),
        server_monthly_usd=float(raw["server"]["monthly_cost_usd"]),
        per_task_default_cap_usd=float(llm["per_task_default_cap_usd"]),
        per_agent_daily_cap_usd=float(llm["per_agent_daily_cap_usd"]),
        per_request_cap_usd=float(llm["per_request_cap_usd"]),
        warn=float(t["warn"]), throttle=float(t["throttle"]), hard_stop=float(t["hard_stop"]),
    )
