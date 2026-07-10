"""Бэкенд внешнего поиска. По умолчанию Brave Search API (SEARCH_API_KEY —
X-Subscription-Token). Нормализует ответ к [{title,url,snippet}].

Провайдер можно сменить переменной SEARCH_PROVIDER (brave|tavily); интерфейс
один, чтобы app и allowlist-фильтр не зависели от конкретного вендора.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("search.backend")


def raw_search(query: str, count: int, http: httpx.Client) -> list[dict]:
    provider = os.environ.get("SEARCH_PROVIDER", "brave").lower()
    key = os.environ.get("SEARCH_API_KEY", "")
    if not key:
        log.warning("SEARCH_API_KEY не задан — поиск вернёт пусто")
        return []
    if provider == "tavily":
        return _tavily(query, count, key, http)
    return _brave(query, count, key, http)


def _brave(query: str, count: int, key: str, http: httpx.Client) -> list[dict]:
    r = http.get("https://api.search.brave.com/res/v1/web/search",
                 headers={"X-Subscription-Token": key, "Accept": "application/json"},
                 params={"q": query, "count": count})
    r.raise_for_status()
    web = r.json().get("web", {}).get("results", [])
    return [{"title": w.get("title"), "url": w.get("url"),
             "snippet": w.get("description")} for w in web]


def _tavily(query: str, count: int, key: str, http: httpx.Client) -> list[dict]:
    r = http.post("https://api.tavily.com/search",
                  json={"api_key": key, "query": query, "max_results": count})
    r.raise_for_status()
    return [{"title": w.get("title"), "url": w.get("url"),
             "snippet": w.get("content")} for w in r.json().get("results", [])]
