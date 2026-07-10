"""comms-bot: единая точка связи со мной. Два потока:
  1. приём команд (long-poll Telegram) -> handlers -> ответ
  2. рассылка уведомлений (consume шины) -> notifications -> сообщение админу

Оба долгоживущие (это фоновая задача, не «завершается»).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from .handlers import handle_command
from .notifications import should_notify
from .services import Services
from .telegram import TelegramClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("comms-bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ["TELEGRAM_ADMIN_USER_ID"])
NOTIFY_TOPICS = ["tasks.verdicts", "journal.events", "control.commands"]


def command_loop(tg: TelegramClient, services: Services) -> None:
    log.info("command loop started")
    while True:
        try:
            for upd in tg.get_updates():
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                user_id = msg.get("from", {}).get("id")
                chat_id = msg.get("chat", {}).get("id")
                reply = handle_command(msg.get("text", ""), user_id, ADMIN_ID, services)
                if reply:
                    tg.send_message(chat_id, reply)
        except Exception as e:  # noqa: BLE001
            log.warning("command loop error: %s", e)
            time.sleep(3)


def notify_loop(tg: TelegramClient) -> None:
    from kafka import KafkaConsumer

    while True:
        try:
            consumer = KafkaConsumer(
                *NOTIFY_TOPICS,
                bootstrap_servers=os.environ["KAFKA_BROKERS"].split(","),
                value_deserializer=lambda v: json.loads(v.decode()),
                auto_offset_reset="latest",
                group_id="comms-bot",
            )
            log.info("notify loop subscribed to %s", NOTIFY_TOPICS)
            for rec in consumer:
                text = should_notify(rec.topic, rec.value)
                if text:
                    tg.send_message(ADMIN_ID, text)
        except Exception as e:  # noqa: BLE001
            log.warning("notify loop error (retry in 5s): %s", e)
            time.sleep(5)


def main() -> None:
    tg = TelegramClient(TOKEN)
    services = Services(
        orchestrator_url=os.environ["ORCHESTRATOR_URL"],
        journal_url=os.environ["JOURNAL_URL"],
        budget_guard_url=os.environ.get("BUDGET_GUARD_URL", "http://budget-guard:8080"),
    )
    tg.send_message(ADMIN_ID, "🤖 llm-tribe: comms-bot запущен. /help")
    threading.Thread(target=notify_loop, args=(tg,), daemon=True).start()
    command_loop(tg, services)


if __name__ == "__main__":
    main()
