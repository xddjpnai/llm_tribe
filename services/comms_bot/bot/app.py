"""ЗАЩИЩЁННЫЙ цикл бота. Порядок проверок (auth -> protected-команды -> изменяемые
обработчики) задаётся здесь и не подлежит изменению агентами (selfmod-api отклоняет
патчи к этому файлу). Два долгоживущих потока: приём команд + рассылка уведомлений.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from . import handlers, protected
from .llm import LLMClient
from .notifications import should_notify
from .services import Services
from .telegram import TelegramClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("comms-bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
NOTIFY_TOPICS = ["tasks.verdicts", "journal.events", "control.commands"]


def _parse(text: str) -> tuple[str, str]:
    parts = (text or "").strip().split(maxsplit=1)
    cmd = parts[0].lstrip("/").lower().split("@")[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    return cmd, rest


def command_loop(tg: TelegramClient, services: Services, llm: LLMClient) -> None:
    log.info("command loop started; владельцы: %s", sorted(protected.OWNER_IDS))
    while True:
        try:
            for upd in tg.get_updates():
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                user_id = msg.get("from", {}).get("id")
                chat_id = msg.get("chat", {}).get("id")
                cmd, rest = _parse(text)

                # 1) ЗАЩИЩЁННЫЙ гейт: auth + ЯВНЫЕ protected-команды (/kill, /user).
                #    Не участник -> молча игнор. Свободный текст сюда как команда не
                #    попадает — kill только через явный /kill.
                handled, reply = protected.dispatch(text, user_id, cmd, rest)
                if handled:
                    if reply:
                        tg.send_message(chat_id, reply)
                    continue

                # 2) ИЗМЕНЯЕМЫЙ слой: свободный текст (LLM-роутер) или slash-шорткат.
                #    Пользователь уже аутентифицирован; kill тут недоступен.
                reply = handlers.handle_message(text, user_id, services, llm)
                if reply:
                    tg.send_message(chat_id, reply)
        except Exception as e:  # noqa: BLE001
            log.warning("command loop error: %s", e)
            time.sleep(3)


def notify_loop(tg: TelegramClient) -> None:
    from kafka import KafkaConsumer

    owners = sorted(protected.OWNER_IDS)
    while True:
        try:
            consumer = KafkaConsumer(
                *NOTIFY_TOPICS,
                bootstrap_servers=os.environ["KAFKA_BROKERS"].split(","),
                value_deserializer=lambda v: json.loads(v.decode()),
                auto_offset_reset="latest", group_id="comms-bot",
            )
            log.info("notify loop subscribed to %s", NOTIFY_TOPICS)
            for rec in consumer:
                text = should_notify(rec.topic, rec.value)
                if text:
                    for owner in owners:           # проактивные алерты — владельцам
                        tg.send_message(owner, text)
        except Exception as e:  # noqa: BLE001
            log.warning("notify loop error (retry in 5s): %s", e)
            time.sleep(5)


def main() -> None:
    if not protected.OWNER_IDS:
        log.error("TELEGRAM_OWNER_IDS пуст — впиши свой Telegram id в secrets/credentials.env")
    tg = TelegramClient(TOKEN)
    guard_url = os.environ.get("BUDGET_GUARD_URL", "http://budget-guard:8080")
    services = Services(
        orchestrator_url=os.environ["ORCHESTRATOR_URL"],
        journal_url=os.environ["JOURNAL_URL"],
        budget_guard_url=guard_url,
    )
    llm = LLMClient(guard_url)
    for owner in sorted(protected.OWNER_IDS):
        tg.send_message(owner, "🤖 llm-tribe: comms-bot запущен. Напиши мне текстом, "
                               "что нужно, или /help.")
    threading.Thread(target=notify_loop, args=(tg,), daemon=True).start()
    command_loop(tg, services, llm)


if __name__ == "__main__":
    main()
