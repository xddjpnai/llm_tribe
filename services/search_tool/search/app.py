"""search-tool: контролируемое окно агентов наружу (guard #4).
Allowlist источников + суточная квота запросов на агента. Без квоты/allowlist
у агентов НЕТ произвольного доступа в интернет (сеть agents_net — internal)."""
from __future__ import annotations

import logging
import os
import time

import httpx
import redis
import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import backend
from .filter import filter_results

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("search-tool")

app = FastAPI(title="search-tool")

_cfg = yaml.safe_load(open(os.environ.get("SEARCH_CONFIG", "/configs/search_allowlist.yaml")))
ALLOWLIST = _cfg["allowlist_domains"]
DAILY_QUOTA = int(_cfg["per_agent_daily_quota"])
MAX_CAP = int(_cfg["max_results_cap"])

_r = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/2"), decode_responses=True)
_http = httpx.Client(timeout=30.0)


class SearchRequest(BaseModel):
    agent_id: str
    query: str
    max_results: int = 5


def _quota_key(agent_id: str) -> str:
    return f"search:quota:{agent_id}:{time.strftime('%Y-%m-%d')}"


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/search")
def search(req: SearchRequest):
    key = _quota_key(req.agent_id)
    used = int(_r.get(key) or 0)
    if used >= DAILY_QUOTA:
        return JSONResponse(status_code=429,
                            content={"reason": "quota", "quota_remaining": 0})

    cap = min(max(1, req.max_results), MAX_CAP)
    try:
        raw = backend.raw_search(req.query, cap * 3, _http)   # берём с запасом до фильтра
    except httpx.HTTPError as e:
        log.warning("search backend error: %s", e)
        return JSONResponse(status_code=502, content={"error": f"search backend: {e}"})

    results = filter_results(raw, ALLOWLIST, cap)

    # квоту списываем за факт запроса (даже если allowlist всё отфильтровал —
    # обращение наружу состоялось)
    _r.incr(key)
    _r.expire(key, 60 * 60 * 48)
    remaining = max(0, DAILY_QUOTA - (used + 1))
    return {"results": results, "quota_remaining": remaining}
