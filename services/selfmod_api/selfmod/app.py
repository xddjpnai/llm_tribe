"""self-modification API: единственный сервис с доступом к docker.sock.

Цикл: патч от агента → применение во ВРЕМЕННУЮ копию → валидация в изолированном
одноразовом контейнере (без сети, лимиты) → при успехе применение/сборка, иначе откат.

target=workspace: патч валидируется и применяется на ветку агента в /workspace.
target=agent:     собирается КАНДИДАТ-образ (валидирует и Dockerfile), но НЕ
                  разворачивается — прямой рестарт прод-контейнера агентом запрещён,
                  свап делается контролируемо (веха в журнал + тег образа в ответе).
"""
from __future__ import annotations

import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

from . import apply, events, protect
from .isolate import DockerRunner, Runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("selfmod-api")

app = FastAPI(title="selfmod-api")

AGENT_SRC = Path(os.environ.get("AGENT_BUILD_CONTEXT", "/build_context/agent"))
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
AGENT_UID = int(os.environ.get("AGENT_UID", "10001"))   # uid агента в его образе

# подменяемо в тестах
runner: Runner = DockerRunner()


def _chown_workspace() -> None:
    """Всё в workspace должно принадлежать агентам (uid 10001): selfmod работает
    от root, и созданные им файлы/git-объекты иначе заблокируют запись агента."""
    try:
        os.chown(WORKSPACE, AGENT_UID, AGENT_UID)
        for root, dirs, files in os.walk(WORKSPACE):
            for name in dirs + files:
                try:
                    os.lchown(os.path.join(root, name), AGENT_UID, AGENT_UID)
                except OSError:
                    pass
    except OSError as e:
        log.warning("chown workspace не удался: %s", e)


def _init_workspace() -> None:
    """Init-шаг вместо удалённого оркестратора: общий workspace должен быть
    git-репозиторием (git_commit агента, ветки для sage) и принадлежать агентам —
    named volume создаётся root-owned, потому что selfmod стартует первым."""
    if not WORKSPACE.is_dir():
        log.warning("workspace %s не смонтирован — init пропущен", WORKSPACE)
        return
    if not (WORKSPACE / ".git").exists():
        for cmd in (["git", "-C", str(WORKSPACE), "init", "-q", "-b", "main"],
                    ["git", "-C", str(WORKSPACE), "-c", "user.email=selfmod@llm-tribe",
                     "-c", "user.name=selfmod", "commit", "-q", "--allow-empty",
                     "-m", "workspace init"]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                log.warning("workspace git init: %s", (r.stdout + r.stderr).strip())
                break
    _chown_workspace()


_init_workspace()


class PatchRequest(BaseModel):
    agent_id: str
    description: str
    diff: str
    target: str = "workspace"        # "workspace" | "agent"
    task_id: Optional[str] = None


class PatchResponse(BaseModel):
    accepted: bool
    patch_id: str
    tests_passed: bool
    logs: str
    rebuilt: bool
    candidate_image: Optional[str] = None


def _commit_branch(agent_id: str, message: str) -> str:
    branch = f"agent/{agent_id}"
    for cmd in (["git", "-C", str(WORKSPACE), "checkout", "-B", branch],
                ["git", "-C", str(WORKSPACE), "add", "-A"],
                ["git", "-C", str(WORKSPACE), "-c", "user.email=selfmod@llm-tribe",
                 "-c", "user.name=selfmod", "commit", "-m", message]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            return f"branch commit warn: {(r.stdout + r.stderr).strip()}"
    return f"committed to {branch}"


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/patch", response_model=PatchResponse)
def patch(req: PatchRequest) -> PatchResponse:
    patch_id = uuid.uuid4().hex[:12]
    events.audit(req.agent_id, req.task_id or "", "selfmod_received",
                 f"{req.target}: {req.description[:200]}")

    # защита: kill-switch, управление доступом, auth, креды, деньги — неизменяемы
    protected = protect.blocked_path(req.diff)
    if protected:
        events.journal(req.agent_id, "selfmod_blocked_protected",
                       f"{patch_id}: отклонён — защищённый путь {protected}")
        events.audit(req.agent_id, req.task_id or "", "selfmod_blocked", protected)
        return PatchResponse(accepted=False, patch_id=patch_id, tests_passed=False,
                             rebuilt=False,
                             logs=f"патч отклонён: путь '{protected}' защищён от "
                                  f"самомодификации (kill-switch/доступ/креды/деньги)")

    source = AGENT_SRC if req.target == "agent" else WORKSPACE
    if not source.exists():
        return PatchResponse(accepted=False, patch_id=patch_id, tests_passed=False,
                             rebuilt=False, logs=f"целевой каталог не найден: {source}")

    # 1. применить во временную копию (fail-fast, если diff кривой)
    res = apply.stage_and_apply(source, req.diff)
    if not res.ok:
        events.journal(req.agent_id, "selfmod_rejected", f"{patch_id}: diff не применяется")
        return PatchResponse(accepted=False, patch_id=patch_id, tests_passed=False,
                             rebuilt=False, logs=res.log)

    try:
        # 2. валидация в изоляции
        if req.target == "agent":
            run = runner.validate_and_build_agent(res.workdir, req.agent_id, patch_id)
        else:
            run = runner.validate_workspace(res.workdir)

        if not run.passed:
            events.journal(req.agent_id, "selfmod_failed",
                           f"{patch_id}: тесты не прошли, откат")
            return PatchResponse(accepted=False, patch_id=patch_id, tests_passed=False,
                                 rebuilt=False, logs=run.logs[:4000])

        # 3. применение при успехе
        if req.target == "agent":
            # образ собран и провалидирован, но НЕ развёрнут (контролируемый свап)
            events.journal(req.agent_id, "selfmod_candidate_ready",
                           f"{patch_id}: образ {run.candidate_image} готов к контролируемому свапу")
            events.audit(req.agent_id, req.task_id or "", "selfmod_candidate",
                         run.candidate_image or "")
            return PatchResponse(accepted=True, patch_id=patch_id, tests_passed=True,
                                 rebuilt=True, candidate_image=run.candidate_image,
                                 logs=run.logs[:4000])
        else:
            apply.promote(res.workdir, WORKSPACE)
            commit_log = _commit_branch(req.agent_id, f"selfmod {patch_id}: {req.description[:80]}")
            _chown_workspace()   # файлы/git-объекты созданы root'ом — вернуть агентам
            events.journal(req.agent_id, "selfmod_applied",
                           f"{patch_id}: применён на ветку agent/{req.agent_id}")
            events.audit(req.agent_id, req.task_id or "", "selfmod_applied", patch_id)
            return PatchResponse(accepted=True, patch_id=patch_id, tests_passed=True,
                                 rebuilt=False, logs=f"{run.logs[:3000]}\n{commit_log}")
    finally:
        apply.cleanup(res.workdir)
