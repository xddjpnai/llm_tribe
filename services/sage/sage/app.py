"""sage — мудрец/судья общины. Независимый внешний вердикт по сданной работе:
воспроизводит артефакт из ветки агента + оценивает качество (LLM другого вендора).
Агентами НЕ изменяем (selfmod отклоняет патчи к sage/), иначе судью бы переписали.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import redis
from fastapi import FastAPI
from pydantic import BaseModel

from .evaluate import judge
from .llm import LLMClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("sage")

app = FastAPI(title="sage")

WORKSPACE = os.environ.get("WORKSPACE", "/workspace")
THRESHOLD = float(os.environ.get("QUALITY_THRESHOLD", "0.6"))
REPRO_TIMEOUT = int(os.environ.get("REPRO_TIMEOUT_SEC", "30"))
llm = LLMClient(os.environ["BUDGET_GUARD_URL"])
_r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True) if os.environ.get("REDIS_URL") else None


class JudgeRequest(BaseModel):
    task_id: Optional[str] = None
    statement: str
    summary: str = ""
    artifact_ref: str = ""
    branch: str = ""


def _export_branch(branch: str) -> Path:
    """git archive ветки агента → writable tmp (workspace смонтирован :ro).
    Если ветки нет — оцениваем по живому дереву."""
    if not branch:
        return Path(WORKSPACE)
    tmp = Path(tempfile.mkdtemp(prefix="sage_"))
    try:
        arch = subprocess.run(["git", "-C", WORKSPACE, "archive", "--format=tar", branch],
                              capture_output=True, timeout=60)
        if arch.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            return Path(WORKSPACE)
        subprocess.run(["tar", "-x", "-C", str(tmp)], input=arch.stdout, timeout=60)
        return tmp
    except Exception as e:  # noqa: BLE001
        log.warning("export ветки %s упал: %s", branch, e)
        shutil.rmtree(tmp, ignore_errors=True)
        return Path(WORKSPACE)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/judge")
def judge_ep(req: JudgeRequest) -> dict:
    workdir = _export_branch(req.branch)
    try:
        v = judge(req.statement, req.summary, workdir, req.artifact_ref, llm, THRESHOLD, REPRO_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.exception("оценка %s упала", req.task_id)
        v = type("V", (), {"verdict": "unsolved", "quality": 0.0, "reproducible": False,
                           "reason": f"sage error: {e}"})()
    finally:
        if str(workdir) != WORKSPACE:
            shutil.rmtree(workdir, ignore_errors=True)

    out = {"task_id": req.task_id, "verdict": v.verdict, "quality": v.quality,
           "reproducible": v.reproducible, "reason": v.reason}
    log.info("вердикт %s: %s (q=%s)", req.task_id, v.verdict, v.quality)
    if _r:
        try:
            _r.rpush("events", json.dumps({"ts": time.time(), "agent_id": "sage",
                     "topic": "verdict", **out}, ensure_ascii=False))
            _r.ltrim("events", -10000, -1)
        except Exception as e:  # noqa: BLE001
            log.warning("event push failed: %s", e)
    return out
