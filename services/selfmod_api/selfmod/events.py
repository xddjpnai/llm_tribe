"""Best-effort эмиссия событий: веха в бортовой журнал + аудит-запись в ClickHouse.
Отказ шины/БД не роняет обработку патча — только логируется."""
from __future__ import annotations

import json
import logging
import os
import time

import httpx

log = logging.getLogger("selfmod.events")
_http = httpx.Client(timeout=10.0)
_producer = None


def _kafka():
    global _producer
    if _producer is None:
        try:
            from kafka import KafkaProducer

            _producer = KafkaProducer(
                bootstrap_servers=os.environ["KAFKA_BROKERS"].split(","),
                value_serializer=lambda v: json.dumps(v).encode(),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("kafka producer init failed: %s", e)
            _producer = False
    return _producer or None


def journal(agent_id: str, action: str, detail: str) -> None:
    p = _kafka()
    if not p:
        return
    try:
        p.send("journal.events", {"ts": time.time(), "agent_id": agent_id,
               "action": action, "detail": detail})
        p.flush(timeout=3)
    except Exception as e:  # noqa: BLE001
        log.warning("journal emit failed: %s", e)


def audit(agent_id: str, task_id: str, action: str, detail: str) -> None:
    url = os.environ.get("CLICKHOUSE_URL")
    if not url:
        return
    row = {"ts": time.time(), "agent_id": agent_id, "task_id": task_id or "",
           "action": action, "detail": detail[:2000], "cost_usd": 0.0}
    try:
        _http.post(url, params={"query": "INSERT INTO audit FORMAT JSONEachRow"},
                   content=json.dumps(row), timeout=10)
    except Exception as e:  # noqa: BLE001
        log.warning("audit insert failed: %s", e)
