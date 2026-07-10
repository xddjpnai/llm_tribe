"""Событийная шина (Kafka) + аудит (ClickHouse). Best-effort: если брокер/БД
недоступны, деградируем до stdout-лога, чтобы агент не падал из-за телеметрии.

Аудит (guard #6): каждое действие агента пишется с ts, agent_id, task_id.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger("agent.events")


class Bus:
    def __init__(self, agent_id: str, kafka_brokers: str, clickhouse_url: str | None = None):
        self.agent_id = agent_id
        self._producer = None
        self._ch = None
        try:
            from kafka import KafkaProducer  # kafka-python

            self._producer = KafkaProducer(
                bootstrap_servers=kafka_brokers.split(","),
                value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode(),
                retries=3,
                linger_ms=50,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Kafka недоступна, события идут в лог: %s", e)
        if clickhouse_url:
            try:
                self._ch = httpx_client(clickhouse_url)
            except Exception as e:  # noqa: BLE001
                log.warning("ClickHouse недоступна, аудит в лог: %s", e)

    def emit(self, topic: str, payload: dict[str, Any]) -> None:
        payload = {"ts": time.time(), "agent_id": self.agent_id, **payload}
        if self._producer is not None:
            try:
                self._producer.send(topic, payload)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("send(%s) failed: %s", topic, e)
        log.info("[event %s] %s", topic, json.dumps(payload, ensure_ascii=False))

    def audit(
        self,
        *,
        task_id: str | None,
        action: str,
        detail: str,
        cost_usd: float = 0.0,
    ) -> None:
        """Строка в ClickHouse.audit + человекочитаемая веха в бортовой журнал."""
        row = {
            "ts": time.time(),
            "agent_id": self.agent_id,
            "task_id": task_id or "",
            "action": action,
            "detail": detail[:4000],
            "cost_usd": cost_usd,
        }
        if self._ch is not None:
            try:
                self._ch.post(
                    "/",
                    params={"query": "INSERT INTO audit FORMAT JSONEachRow"},
                    content=json.dumps(row, ensure_ascii=False),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("audit insert failed: %s", e)
        # веха в журнал (сервис journal сам решит, когда суммаризировать)
        self.emit("journal.events", {"task_id": task_id, "action": action, "detail": detail[:500]})

    def flush(self) -> None:
        if self._producer is not None:
            try:
                self._producer.flush(timeout=5)
            except Exception:  # noqa: BLE001
                pass


def httpx_client(base_url: str):
    import httpx

    return httpx.Client(base_url=base_url, timeout=10.0)
