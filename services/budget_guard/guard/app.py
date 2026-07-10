"""budget-guard: единственная точка ко всем LLM API (ключи только здесь).
На каждый /v1/chat: клампит max_tokens до рамки на один вызов, резолвит роль →
модель, при недоступности провайдера идёт по fallback-цепочке, пишет накопительную
стоимость. Общий потолок не enforce'ится — за ним следит владелец.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .accounting import Accounting
from .config import load_budget, load_routing
from .providers import ProviderError, call, cost_usd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("budget-guard")

app = FastAPI(title="budget-guard")

routing = load_routing(os.environ.get("MODEL_ROUTING", "/configs/model_routing.yaml"))
limits = load_budget(os.environ.get("BUDGET_CONFIG", "/configs/budget.yaml"))
_r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
acct = Accounting(_r)
_http = httpx.Client(timeout=300.0)


class ChatRequest(BaseModel):
    agent_id: str
    task_id: Optional[str] = None
    role: str
    messages: list[dict[str, Any]]
    tools: Optional[list[dict]] = None
    tool_choice: Optional[str] = None
    max_tokens: int = 4096
    temperature: Optional[float] = None


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/v1/budget")
def budget() -> dict:
    # накопительный LLM-расход, информативно (потолок отслеживает владелец сам)
    return {"llm_spent_usd": acct.total(),
            "frame_per_call": {"max_output_tokens": limits.max_output_tokens,
                               "max_cost_usd": limits.max_cost_usd_per_call}}


@app.post("/v1/chat")
def chat(req: ChatRequest):
    # РАМКА НА ОДНО ДЕЙСТВИЕ: клампим запрошенный max_tokens до потолка — так один
    # вызов не может выйти за рамку по выводу. Общий расход не enforce'ится.
    max_tokens = min(req.max_tokens, limits.max_output_tokens)

    try:
        chain = routing.chain(req.role)
    except KeyError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    last_err = None
    for idx, model in enumerate(chain):
        provider = routing.providers.get(model.provider)
        if provider is None or not provider.api_key:
            last_err = f"{model.name}: провайдер без ключа"
            continue
        try:
            comp = call(_http, provider, model, req.messages, req.tools,
                        max_tokens, req.temperature)
        except ProviderError as e:
            last_err = str(e)
            log.warning("fallback: модель %s недоступна (%s)", model.name, e)
            continue

        cost = cost_usd(model, comp.input_tokens, comp.output_tokens)
        fell_back = idx > 0
        acct.record(cost)
        if cost > limits.max_cost_usd_per_call:
            log.warning("вызов %s стоил $%.4f — выше рамки на действие", model.name, cost)

        return {
            "content": comp.content,
            "tool_calls": comp.tool_calls,
            "usage": {"input_tokens": comp.input_tokens, "output_tokens": comp.output_tokens},
            "cost_usd": cost, "model": model.name, "fell_back": fell_back,
            "budget": {"llm_spent_usd": acct.total()},
        }

    log.error("вся fallback-цепочка роли %s недоступна: %s", req.role, last_err)
    return JSONResponse(status_code=503, content={"error": "all providers unavailable",
                        "detail": last_err})
