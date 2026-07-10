"""budget-guard: FastAPI-прокси перед всеми платными LLM API.

Единственная точка входа (guard #1). На каждый /v1/chat:
  1. admission: проверка капов и state (hard_stop/task_cap/agent_cap/throttle)
  2. fallback-цепочка роли: primary -> fallbacks (другой провайдер) при недоступности
  3. запись стоимости в оба счётчика (llm + учёт сервера идёт отдельно)
  4. трейс вызова (промпт-мета/токены/стоимость/модель) в ClickHouse + алерты порогов
"""
from __future__ import annotations

import json
import logging
import os
import time
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
acct = Accounting(_r, limits)
_http = httpx.Client(timeout=300.0)
_ch_url = os.environ.get("CLICKHOUSE_URL")

# антидребезг алертов: не спамить journal одним и тем же порогом
_last_alert_state = {"v": "ok"}


class ChatRequest(BaseModel):
    agent_id: str
    task_id: Optional[str] = None
    role: str
    messages: list[dict[str, Any]]
    tools: Optional[list[dict]] = None
    tool_choice: Optional[str] = None
    max_tokens: int = 4096
    temperature: Optional[float] = None


class TaskCapRequest(BaseModel):
    task_id: str
    cap_usd: float


def _trace(agent_id: str, task_id: Optional[str], model: str, tin: int, tout: int,
           cost: float, fell_back: bool) -> None:
    if not _ch_url:
        return
    row = {"ts": time.time(), "agent_id": agent_id, "task_id": task_id or "",
           "model": model, "input_tokens": tin, "output_tokens": tout,
           "cost_usd": cost, "fell_back": 1 if fell_back else 0}
    try:
        _http.post(_ch_url, params={"query": "INSERT INTO llm_traces FORMAT JSONEachRow"},
                   content=json.dumps(row), timeout=10)
    except Exception as e:  # noqa: BLE001
        log.warning("trace insert failed: %s", e)


def _maybe_alert(state: str) -> None:
    """При смене состояния порога — веха в журнал (бот подхватит warn/hard_stop)."""
    if state != _last_alert_state["v"]:
        _last_alert_state["v"] = state
        try:
            from kafka import KafkaProducer

            p = KafkaProducer(bootstrap_servers=os.environ["KAFKA_BROKERS"].split(","),
                              value_serializer=lambda v: json.dumps(v).encode())
            p.send("journal.events", {"ts": time.time(), "agent_id": "budget-guard",
                   "action": "budget_state", "detail": f"state -> {state}"})
            p.flush(timeout=3)
        except Exception as e:  # noqa: BLE001
            log.warning("alert emit failed: %s", e)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/v1/budget")
def budget() -> dict:
    snap = acct.snapshot()
    return {"spent_total_usd": snap.total_spent, "llm_spent_usd": snap.llm_spent,
            "server_spent_usd": snap.server_spent, "budget_usd": snap.total_budget,
            "fraction": snap.fraction, "state": snap.state}


@app.post("/v1/task_cap")
def set_task_cap(req: TaskCapRequest) -> dict:
    """Оркестратор регистрирует фактический (конкурентно масштабированный) cap задачи.
    budget-guard — авторитет: enforce'ит именно это значение, а не доверяет капу
    от агента и не берёт всегда дефолт."""
    acct.set_task_cap(req.task_id, req.cap_usd)
    return {"ok": True, "task_id": req.task_id, "cap_usd": req.cap_usd}


@app.post("/v1/chat")
def chat(req: ChatRequest):
    # фактический cap задачи (из /v1/task_cap) либо дефолт из budget.yaml
    task_cap = acct.get_task_cap(req.task_id, limits.per_task_default_cap_usd)
    ok, reason, retry = acct.check_admission(req.agent_id, req.task_id, task_cap)
    if not ok:
        if reason == "throttle":
            return JSONResponse(status_code=429, content={"retry_after_sec": retry, "reason": reason})
        _maybe_alert("hard_stop" if reason == "hard_stop" else acct.snapshot().state)
        return JSONResponse(status_code=402, content={"reason": reason})

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
                        req.max_tokens, req.temperature)
        except ProviderError as e:
            last_err = str(e)
            log.warning("fallback: модель %s недоступна (%s)", model.name, e)
            continue

        cost = cost_usd(model, comp.input_tokens, comp.output_tokens)
        # защита от гигантского контекста: если один вызов дороже per-request капа —
        # расход всё равно записываем (он уже потрачен), но помечаем в трейсе
        fell_back = idx > 0
        snap = acct.record(req.agent_id, req.task_id, cost)
        _trace(req.agent_id, req.task_id, model.name, comp.input_tokens,
               comp.output_tokens, cost, fell_back)
        _maybe_alert(snap.state)
        if cost > acct.per_request_cap():
            log.warning("вызов %s превысил per-request cap: $%.4f", model.name, cost)

        return {
            "content": comp.content,
            "tool_calls": comp.tool_calls,
            "usage": {"input_tokens": comp.input_tokens, "output_tokens": comp.output_tokens},
            "cost_usd": cost, "model": model.name, "fell_back": fell_back,
            "budget": {"spent_total_usd": snap.total_spent,
                       "task_spent_usd": acct.task_spent(req.task_id) if req.task_id else 0.0,
                       "state": snap.state},
        }

    # вся цепочка недоступна — единая точка отказа на провайдере (guard #7 не спас)
    log.error("вся fallback-цепочка роли %s недоступна: %s", req.role, last_err)
    return JSONResponse(status_code=503, content={"error": "all providers unavailable",
                        "detail": last_err})
