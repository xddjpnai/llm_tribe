"""Тонкий клиент Telegram Bot API на httpx (long-poll getUpdates + sendMessage).
Достаточно для контура управления; тяжёлый python-telegram-bot не нужен."""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("bot.telegram")


class TelegramClient:
    def __init__(self, token: str, timeout: float = 65.0):
        self._base = f"https://api.telegram.org/bot{token}"
        self._http = httpx.Client(timeout=timeout)
        self._offset: int | None = None

    def get_updates(self, long_poll_sec: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": long_poll_sec}
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            r = self._http.get(f"{self._base}/getUpdates", params=params)
            r.raise_for_status()
            updates = r.json().get("result", [])
        except httpx.HTTPError as e:
            log.warning("getUpdates failed: %s", e)
            return []
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    def send_message(self, chat_id: int | str, text: str) -> None:
        # markdown отключён намеренно: LLM-саммари/детали могут ломать разметку
        for chunk in _split(text, 4000):
            try:
                self._http.post(f"{self._base}/sendMessage",
                                json={"chat_id": chat_id, "text": chunk,
                                      "disable_web_page_preview": True})
            except httpx.HTTPError as e:
                log.warning("sendMessage failed: %s", e)


def _split(text: str, limit: int) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]
