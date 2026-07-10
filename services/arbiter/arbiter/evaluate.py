"""Оценка сданного результата. Арбитр ВСЕГДА оценивает — оценка не пропускается.

Два объективных сигнала:
  1. Воспроизводимость — арбитр исполняет артефакт агента (тесты/бенчмарк) в
     изолированном subprocess с таймаутом; смотрит exit code.
  2. Качество отчёта — LLM-оценка (роль arbiter, другой вендор, чем researcher:
     self-preference bias решён) по постановке + summary + логам прогона.

Вердикт: solved (воспроизводимо И quality >= threshold) иначе unsolved.
Если агент не сдал ничего / упёрся в бюджет — тоже оценивается и обычно unsolved.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .llm import LLMClient

ARBITER_SYSTEM = """\
You are an impartial research arbiter. You did not produce this work. Judge it strictly and
objectively. You are given the problem statement, the agent's own summary, and the raw output
of independently re-running the agent's artifact. Do NOT trust the agent's claims over the
reproduction output — if the run failed or the artifact is missing, quality is low regardless
of what the summary asserts.

Reply with ONLY a JSON object:
{"quality": <float 0..1>, "reproducible": <bool>, "reason": "<one or two sentences>"}
quality reflects report clarity, honesty vs the reproduction output, and how convincingly the
result is demonstrated."""


@dataclass
class Verdict:
    verdict: str          # "solved" | "unsolved"
    quality: float        # 0..1 — используется в конкурентной механике бюджета
    reproducible: bool
    reason: str
    cost_usd: float


def _reproduce(workspace: Path, artifact_path: str, timeout_sec: int) -> tuple[bool, str]:
    """Исполняет артефакт агента (если это .py). Возвращает (успех, логи).
    Артефакт-агента лежит в его ветке; для скелета предполагаем, что рабочее
    дерево workspace уже на нужной ветке (переключение делает main перед вызовом)."""
    if not artifact_path:
        return False, "артефакт не указан"
    p = (workspace / artifact_path).resolve()
    if not str(p).startswith(str(workspace.resolve())):
        return False, f"artifact вне workspace: {p}"
    if not p.exists():
        return False, f"артефакт не найден: {p}"
    if p.suffix != ".py":
        # нечисловой артефакт: воспроизводимость по коду не проверить автоматически
        return False, f"не исполняемый .py артефакт ({p.suffix}), нужна ручная проверка"
    try:
        proc = subprocess.run(
            ["python3", "-I", str(p)],
            cwd=str(workspace), capture_output=True, text=True, timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT после {timeout_sec}s"
    logs = f"exit={proc.returncode}\nstdout:\n{proc.stdout[-6000:]}\nstderr:\n{proc.stderr[-3000:]}"
    return proc.returncode == 0, logs


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def evaluate(submission: dict, task_statement: str, workspace: Path,
             llm: LLMClient, quality_threshold: float, repro_timeout: int) -> Verdict:
    reproducible, logs = _reproduce(workspace, submission.get("artifact_ref", ""), repro_timeout)

    user = (
        f"PROBLEM STATEMENT:\n{task_statement}\n\n"
        f"AGENT SUMMARY:\n{submission.get('summary', '')}\n\n"
        f"REPRODUCTION OUTPUT (independent re-run of the artifact):\n{logs}"
    )
    text, cost = llm.chat([
        {"role": "system", "content": ARBITER_SYSTEM},
        {"role": "user", "content": user},
    ])
    parsed = _parse_json(text)
    quality = float(parsed.get("quality", 0.0) or 0.0)
    # доверяем объективному прогону поверх мнения модели о воспроизводимости
    reproducible_final = reproducible and bool(parsed.get("reproducible", reproducible))
    reason = parsed.get("reason", "no reason returned")[:500]

    solved = reproducible_final and quality >= quality_threshold
    return Verdict(
        verdict="solved" if solved else "unsolved",
        quality=round(max(0.0, min(1.0, quality)), 3),
        reproducible=reproducible_final,
        reason=reason,
        cost_usd=cost,
    )
