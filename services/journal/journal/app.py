"""journal-сервис: бортовой журнал (постоянная фоновая задача).

  - фоновый поток: consume journal.events -> store.append_event
  - периодический поток: раз в интервал по каждому scope с накопленными событиями
    (>= MIN_EVENTS или прошёл интервал активности) -> LLM-саммари -> дописать в .md
  - HTTP: GET /v1/journal?task_id=&agent_id= -> markdown (бот отдаёт по запросу)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

import httpx
from fastapi import FastAPI

from .store import JournalStore
from .summarize import summarize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("journal")

app = FastAPI(title="journal")

GUARD_URL = os.environ["BUDGET_GUARD_URL"]
SUMMARIZE_INTERVAL = int(os.environ.get("SUMMARIZE_INTERVAL_SEC", "3600"))  # раз в час активности
MIN_EVENTS = int(os.environ.get("SUMMARIZE_MIN_EVENTS", "20"))             # или раз в N событий

store = JournalStore(os.environ.get("JOURNAL_ROOT", "/journal"))
_http = httpx.Client(timeout=120.0)
_last_summ: dict[str, float] = {}


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/v1/journal")
def get_journal(task_id: Optional[str] = None, agent_id: Optional[str] = None) -> dict:
    if task_id:
        scope = f"task:{task_id}"
    elif agent_id:
        scope = f"agent:{agent_id}"
    else:
        scope = "system"
    md = store.read_markdown(scope)
    return {"scope": scope, "markdown": md or f"(журнал пуст для {scope})"}


def _consume_loop() -> None:
    from kafka import KafkaConsumer

    while True:
        try:
            consumer = KafkaConsumer(
                "journal.events", "tasks.verdicts",
                bootstrap_servers=os.environ["KAFKA_BROKERS"].split(","),
                value_deserializer=lambda v: json.loads(v.decode()),
                auto_offset_reset="earliest", group_id="journal",
            )
            log.info("journal consumer subscribed")
            for rec in consumer:
                store.append_event(rec.value)
        except Exception as e:  # noqa: BLE001
            log.warning("consume loop error (retry 5s): %s", e)
            time.sleep(5)


def _summarize_scope(scope: str) -> None:
    events = store.pending(scope)
    if not events:
        return
    elapsed = time.time() - _last_summ.get(scope, 0)
    if len(events) < MIN_EVENTS and elapsed < SUMMARIZE_INTERVAL:
        return
    text = summarize(GUARD_URL, scope, events, http=_http)
    if not text:            # бюджет/троттлинг — не двигаем курсор, попробуем позже
        return
    store.append_summary(scope, text)
    store.advance_cursor(scope)
    _last_summ[scope] = time.time()
    log.info("summarized %s (%d events)", scope, len(events))


def _summarize_loop() -> None:
    while True:
        try:
            for scope in store.scopes_with_raw():
                _summarize_scope(scope)
        except Exception as e:  # noqa: BLE001
            log.warning("summarize loop error: %s", e)
        time.sleep(60)      # проверяем ежеминутно; порог решает, суммировать ли


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_consume_loop, daemon=True).start()
    threading.Thread(target=_summarize_loop, daemon=True).start()
