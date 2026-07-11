"""Адаптеры провайдеров: единый /v1/chat -> протокол провайдера -> нормализация.

OpenAI-протокол (glm/deepseek/kimi) поддерживает tools — на нём работают агенты.
Anthropic-протокол (claude) — текстовый путь для арбитра/журнала/бота (они tools
не шлют; routing никогда не отправляет tool-запрос на anthropic-fallback).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import ModelSpec, ProviderSpec


class ProviderError(Exception):
    """Провайдер недоступен/ошибся — сигнал для fallback на следующую модель."""


@dataclass
class Completion:
    content: str | None
    tool_calls: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int


def call(http: httpx.Client, provider: ProviderSpec, model: ModelSpec,
         messages: list[dict], tools: list[dict] | None, max_tokens: int,
         temperature: float | None) -> Completion:
    if not provider.api_key:
        raise ProviderError(f"нет API-ключа для провайдера {provider.name}")
    if provider.protocol == "openai":
        return _openai(http, provider, model, messages, tools, max_tokens, temperature)
    if provider.protocol == "anthropic":
        return _anthropic(http, provider, model, messages, tools, max_tokens)
    raise ProviderError(f"неизвестный протокол {provider.protocol}")


def _openai(http, provider, model, messages, tools, max_tokens, temperature) -> Completion:
    body: dict[str, Any] = {"model": model.name, "messages": messages, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if temperature is not None:
        body["temperature"] = temperature
    try:
        r = http.post(f"{provider.base_url}/chat/completions",
                      headers={"Authorization": f"Bearer {provider.api_key}"}, json=body)
    except httpx.HTTPError as e:
        raise ProviderError(f"{provider.name} network: {e}") from e
    # ЛЮБОЙ не-2xx = сигнал fallback (401 при истёкшем ключе, 400/404 при кривой
    # модели, 429/5xx при недоступности). Иначе один плохой провайдер уронил бы
    # весь /v1/chat в 500 вместо перехода на следующую модель в цепочке.
    if r.status_code >= 400:
        raise ProviderError(f"{provider.name} status {r.status_code}: {r.text[:200]}")
    d = r.json()
    msg = d["choices"][0]["message"]
    usage = d.get("usage", {})
    return Completion(
        content=msg.get("content"),
        tool_calls=msg.get("tool_calls") or [],
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
    )


def _anthropic(http, provider, model, messages, tools, max_tokens) -> Completion:
    if tools:
        # routing не должен слать tool-запрос на anthropic; страховка
        raise ProviderError("anthropic-путь в этом guard — текстовый (tools не поддержаны)")
    system_parts, conv = [], []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system_parts.append(m.get("content", ""))
        elif role in ("user", "assistant"):
            conv.append({"role": role, "content": m.get("content", "")})
        elif role == "tool":
            # арбитр/журнал tools не используют; если прилетело — сворачиваем в user
            conv.append({"role": "user", "content": str(m.get("content", ""))})
    body: dict[str, Any] = {"model": model.name, "max_tokens": max_tokens, "messages": conv}
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    try:
        r = http.post(f"{provider.base_url}/v1/messages",
                      headers={"x-api-key": provider.api_key,
                               "anthropic-version": "2023-06-01"}, json=body)
    except httpx.HTTPError as e:
        raise ProviderError(f"{provider.name} network: {e}") from e
    if r.status_code >= 400:   # любой не-2xx = fallback, а не 500 (см. _openai)
        raise ProviderError(f"{provider.name} status {r.status_code}: {r.text[:200]}")
    d = r.json()
    text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    usage = d.get("usage", {})
    return Completion(
        content=text or None,
        tool_calls=[],
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )


def cost_usd(model: ModelSpec, input_tokens: int, output_tokens: int) -> float:
    return round(input_tokens / 1e6 * model.price_in + output_tokens / 1e6 * model.price_out, 6)
