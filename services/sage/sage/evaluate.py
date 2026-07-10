"""Оценка результата мудрецом: объективная воспроизводимость (прогон артефакта) +
качество (LLM-судья другого вендора). Для .py-артефакта прогон авторитетнее мнения
модели; для непрогоняемого результата решает LLM."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SAGE_SYSTEM = """You are the sage — the impartial elder and judge of a self-improving agent
collective. You did NOT produce this work. Judge it strictly and honestly. You are given the
task, the agent's own summary, and the raw output of independently re-running the agent's
artifact (if it was runnable). Do NOT trust the agent's claims over the reproduction output:
if the run failed or the artifact is missing, quality is low regardless of what the summary
says. Reply with ONLY a JSON object:
{"quality": <float 0..1>, "reproducible": <bool>, "reason": "<one or two sentences>"}"""


@dataclass
class Verdict:
    verdict: str          # "solved" | "unsolved"
    quality: float        # 0..1
    reproducible: bool
    reason: str
    cost_usd: float


def _reproduce(workdir: Path, artifact: str, timeout: int):
    """(bool|None, logs). None — артефакт непрогоняемый (не .py), решает LLM."""
    if not artifact:
        return False, "артефакт не указан"
    p = (workdir / artifact).resolve()
    if not str(p).startswith(str(workdir.resolve())):
        return False, f"artifact вне рабочей копии: {p}"
    if not p.exists():
        return False, f"артефакт не найден: {p}"
    if p.suffix != ".py":
        return None, f"непрогоняемый артефакт ({p.suffix}) — оцениваю по описанию"
    try:
        r = subprocess.run(["python3", "-I", str(p)], cwd=str(workdir),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT после {timeout}s"
    return r.returncode == 0, f"exit={r.returncode}\nstdout:\n{r.stdout[-6000:]}\nstderr:\n{r.stderr[-3000:]}"


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def judge(statement: str, summary: str, workdir: Path, artifact: str,
          llm, threshold: float, timeout: int) -> Verdict:
    repro, logs = _reproduce(workdir, artifact, timeout)
    user = (f"TASK:\n{statement}\n\nAGENT SUMMARY:\n{summary}\n\n"
            f"REPRODUCTION OUTPUT (independent re-run):\n{logs}")
    text, cost = llm.chat([{"role": "system", "content": SAGE_SYSTEM},
                           {"role": "user", "content": user}])
    p = _parse_json(text)
    quality = max(0.0, min(1.0, float(p.get("quality", 0.0) or 0.0)))
    # .py: объективный прогон авторитетнее; непрогоняемое — мнение мудреца
    reproducible = bool(p.get("reproducible", True)) if repro is None else repro
    reason = (p.get("reason") or "no reason returned")[:500]
    solved = reproducible and quality >= threshold
    return Verdict("solved" if solved else "unsolved", round(quality, 3),
                   bool(reproducible), reason, cost)
