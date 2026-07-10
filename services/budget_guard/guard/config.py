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
    api_key: str             # разрезолвленный из env (ключи провайдеров только здесь)
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
    # рамка на ОДНО действие (один LLM-вызов). Общий расход отслеживает владелец.
    max_output_tokens: int          # budget-guard клампит max_tokens вызова до этого
    max_cost_usd_per_call: float    # порог для лога (расход уже случился — не enforce)


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
    pr = raw.get("per_request", {})
    return BudgetLimits(
        max_output_tokens=int(pr.get("max_output_tokens", 8000)),
        max_cost_usd_per_call=float(pr.get("max_cost_usd", 0.5)),
    )
