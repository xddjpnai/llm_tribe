"""Фильтрация результатов по allowlist доменов (guard #4). Чистая логика,
тестируется офлайн. Совпадение по домену И его поддоменам, регистронезависимо."""
from __future__ import annotations

from urllib.parse import urlparse


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def domain_allowed(url: str, allowlist: list[str]) -> bool:
    host = _host(url)
    if not host:
        return False
    for d in allowlist:
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def filter_results(results: list[dict], allowlist: list[str], cap: int) -> list[dict]:
    """Оставляет только результаты с разрешённых доменов, не больше cap штук."""
    out = []
    for r in results:
        if domain_allowed(r.get("url", ""), allowlist):
            out.append({"title": r.get("title", ""), "url": r.get("url", ""),
                        "snippet": (r.get("snippet") or r.get("description") or "")[:500]})
        if len(out) >= cap:
            break
    return out
