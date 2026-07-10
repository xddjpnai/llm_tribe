"""Арбитр: слушает tasks.submissions, воспроизводит артефакт, выносит вердикт.

Оценка выполняется ВСЕГДА и никогда не пропускается. При неоднозначности арбитр
не эскалирует на более дорогую модель — выносит вердикт на своей штатной модели
(роль arbiter). Вердикт → tasks.verdicts (оркестратор + бот).
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from .config import Config
from .evaluate import evaluate
from .llm import LLMClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("arbiter.main")


def _export_branch(workspace: str, branch: str) -> Path | None:
    """git archive <branch> → распаковка в writable tmp (workspace смонтирован :ro)."""
    if not branch:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="arb_"))
    try:
        archive = subprocess.run(
            ["git", "-C", workspace, "archive", "--format=tar", branch],
            capture_output=True, timeout=60,
        )
        if archive.returncode != 0:
            log.warning("git archive %s не удался: %s", branch, archive.stderr.decode()[:300])
            return None
        subprocess.run(["tar", "-x", "-C", str(tmp)], input=archive.stdout, timeout=60)
        return tmp
    except Exception as e:  # noqa: BLE001
        log.warning("экспорт ветки %s упал: %s", branch, e)
        return None


def _clickhouse_audit(cfg: Config, task_id: str, verdict, cost: float) -> None:
    if not cfg.clickhouse_url:
        return
    try:
        import httpx

        row = {"ts": time.time(), "agent_id": "arbiter", "task_id": task_id,
               "action": f"verdict:{verdict.verdict}",
               "detail": f"quality={verdict.quality} repro={verdict.reproducible} {verdict.reason}",
               "cost_usd": cost}
        httpx.post(cfg.clickhouse_url, params={"query": "INSERT INTO audit FORMAT JSONEachRow"},
                   content=json.dumps(row, ensure_ascii=False), timeout=10)
    except Exception as e:  # noqa: BLE001
        log.warning("audit insert failed: %s", e)


def main() -> None:
    cfg = Config.from_env()
    llm = LLMClient(cfg.budget_guard_url, cfg.role)

    from kafka import KafkaConsumer, KafkaProducer

    consumer = KafkaConsumer(
        "tasks.submissions",
        bootstrap_servers=cfg.kafka_brokers.split(","),
        value_deserializer=lambda v: json.loads(v.decode()),
        group_id="arbiter", auto_offset_reset="earliest", enable_auto_commit=True,
    )
    producer = KafkaProducer(
        bootstrap_servers=cfg.kafka_brokers.split(","),
        value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode(),
    )
    log.info("arbiter готов, слушаю tasks.submissions")

    for msg in consumer:
        sub = msg.value
        task_id = sub.get("task_id")
        # Постановка задачи должна прийти в submission (агент кладёт statement) либо
        # арбитр берёт её из своего кеша/оркестратора. Для скелета — из поля statement.
        statement = sub.get("statement", "(постановка недоступна арбитру)")
        log.info("оцениваю задачу %s от %s", task_id, sub.get("agent_id"))

        export = _export_branch(cfg.workspace, sub.get("branch", ""))
        workspace = export if export else Path(cfg.workspace)
        try:
            verdict = evaluate(sub, statement, workspace, llm,
                               cfg.quality_threshold, cfg.repro_timeout_sec)
        except Exception as e:  # noqa: BLE001
            log.exception("оценка задачи %s упала", task_id)
            verdict = type("V", (), {"verdict": "unsolved", "quality": 0.0,
                           "reproducible": False, "reason": f"arbiter error: {e}",
                           "cost_usd": 0.0})()

        payload = {
            "task_id": task_id, "agent_id": sub.get("agent_id"),
            "verdict": verdict.verdict, "quality": verdict.quality,
            "reproducible": verdict.reproducible, "reason": verdict.reason,
        }
        producer.send("tasks.verdicts", payload)
        producer.send("journal.events", {"task_id": task_id, "action": f"verdict_{verdict.verdict}",
                      "detail": f"{sub.get('agent_id')}: q={verdict.quality} — {verdict.reason}"})
        producer.flush()
        _clickhouse_audit(cfg, task_id, verdict, verdict.cost_usd)
        log.info("вердикт %s: %s (quality=%s)", task_id, verdict.verdict, verdict.quality)


if __name__ == "__main__":
    main()
