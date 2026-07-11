"""Минимальный набор примитивов агента ("голый старт", см. README).

Сознательно НЕ содержит helper'ов под конкретные форматы/API/пайплайны — если
агенту нужен такой инструмент, он пишет его себе сам через propose_self_modification
(в свою приватную папку или ветку workspace) и дальше вызывает через run_python.

Каждый инструмент = (OpenAI-совместимая спецификация, функция).
Функция принимает ToolContext и kwargs, возвращает строку (уходит модели как tool_result).
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx


@dataclass
class ToolContext:
    agent_id: str
    task_id: str | None
    workspace: Path        # общий git-репозиторий (ветка агента)
    private: Path          # приватная папка агента (недоступна другим)
    selfmod_api_url: str
    sage_url: str          # мудрец: вердикт при submit_result
    http: httpx.Client
    audit: Callable[..., None]   # events.Bus.audit


class ToolError(Exception):
    pass


# --------- защита путей: агент пишет только в свою private и в workspace ----------

def _resolve(ctx: ToolContext, path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ctx.workspace / p
    p = p.resolve()
    allowed = (ctx.workspace.resolve(), ctx.private.resolve())
    if not any(str(p) == str(a) or str(p).startswith(str(a) + os.sep) for a in allowed):
        raise ToolError(f"path {p} вне разрешённых каталогов (/workspace, /private)")
    return p


# ----------------------------- реализации инструментов -----------------------------

def _tail(data: Any, limit: int) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        data = data.decode(errors="replace")
    return data[-limit:]


def _run_python(ctx: ToolContext, code: str, timeout_sec: int = 30) -> str:
    """Исполняет код в контейнере агента (песочница = сам контейнер: cgroup-лимиты,
    cap_drop). Без -I: cwd (workspace) в sys.path, чтобы агент мог импортировать
    модули, которые сам туда написал; окружение (TELEGRAM_BOT_TOKEN и пр.) наследуется.
    Изоляции -I не давал: исполняемый код и так пишет сам агент."""
    timeout_sec = min(max(int(timeout_sec), 1), 120)
    try:
        proc = subprocess.run(
            ["python3", "-c", code],
            cwd=str(ctx.workspace),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        # детач-процесс, унаследовавший stdout/stderr-pipe, держит их открытыми —
        # вызов ждёт EOF до таймаута; сам детач-процесс при этом выживает
        return (f"TIMEOUT после {timeout_sec}s. Если запускал detached-процесс: он жив, "
                f"но redirect его stdin/stdout/stderr в файл или /dev/null — иначе "
                f"этот вызов всегда будет висеть до таймаута.\n"
                f"--- partial stdout ---\n{_tail(e.stdout, 4000)}\n"
                f"--- partial stderr ---\n{_tail(e.stderr, 2000)}")
    out = (proc.stdout or "")[-8000:]
    err = (proc.stderr or "")[-4000:]
    return f"exit={proc.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"


def _write_file(ctx: ToolContext, path: str, content: str) -> str:
    p = _resolve(ctx, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"записано {len(content)} байт в {p}"


def _read_file(ctx: ToolContext, path: str) -> str:
    p = _resolve(ctx, path)
    if not p.exists():
        return f"файл не найден: {p}"
    return p.read_text()[:16000]


def _list_dir(ctx: ToolContext, path: str = ".") -> str:
    p = _resolve(ctx, path)
    if not p.is_dir():
        return f"не каталог: {p}"
    return "\n".join(sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir())) or "(пусто)"


def _git_commit(ctx: ToolContext, message: str) -> str:
    """Коммит в ветку агента внутри общего workspace. Мержи в main проходят
    через арбитра — сам агент в main не пишет."""
    branch = f"agent/{ctx.agent_id}"
    cmds = [
        ["git", "-C", str(ctx.workspace), "checkout", "-B", branch],
        ["git", "-C", str(ctx.workspace), "add", "-A"],
        ["git", "-C", str(ctx.workspace), "-c", "user.email=agent@llm-tribe",
         "-c", f"user.name={ctx.agent_id}", "commit", "-m", message],
    ]
    logs = []
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True)
        logs.append((r.stdout + r.stderr).strip())
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            return f"git error:\n" + "\n".join(logs)
    ctx.audit(task_id=ctx.task_id, action="git_commit", detail=f"{branch}: {message}")
    return "committed:\n" + "\n".join(l for l in logs if l)


def _propose_self_modification(ctx: ToolContext, description: str, diff: str,
                               target: str = "workspace") -> str:
    """Патч → тесты в изолированном раннере → применение/откат (selfmod-api).
    Так агент наращивает себе персональный инструментарий (см. 'принцип голого старта')."""
    ctx.audit(task_id=ctx.task_id, action="selfmod_attempt", detail=description[:500])
    r = ctx.http.post(
        f"{ctx.selfmod_api_url}/v1/patch",
        json={"agent_id": ctx.agent_id, "description": description, "target": target, "diff": diff},
        timeout=600,
    )
    r.raise_for_status()
    d = r.json()
    return (f"accepted={d.get('accepted')} tests_passed={d.get('tests_passed')} "
            f"rebuilt={d.get('rebuilt')}\nlogs:\n{(d.get('logs') or '')[:3000]}")


def _deploy_self(ctx: ToolContext, candidate_image: str, health_timeout: int = 90) -> str:
    """Развернуть собранный кандидат-образ на СВОЙ работающий контейнер (свап с
    откатом). candidate_image берётся из ответа propose_self_modification(target=agent).
    Внимание: успешный деплой перезапускает тебя — текущий процесс умрёт, новый
    поднимется на новом образе; твои detached-процессы тоже перезапустятся."""
    r = ctx.http.post(
        f"{ctx.selfmod_api_url}/v1/deploy",
        json={"agent_id": ctx.agent_id, "candidate_image": candidate_image,
              "health_timeout": int(health_timeout)},
        timeout=600,
    )
    r.raise_for_status()
    d = r.json()
    return (f"deployed={d.get('deployed')} rolled_back={d.get('rolled_back')}\n"
            f"{d.get('logs', '')}")


def _submit_result(ctx: ToolContext, summary: str, artifact_path: str) -> str:
    """Завершает работу над задачей и отправляет результат арбитру.
    Реальную отправку в шину делает graph-луп, увидев вызов этого инструмента."""
    return "__SUBMIT__"


# ------------------------------- реестр + спецификации -------------------------------

_IMPL: dict[str, Callable[..., str]] = {
    "run_python": _run_python,
    "write_file": _write_file,
    "read_file": _read_file,
    "list_dir": _list_dir,
    "git_commit": _git_commit,
    "propose_self_modification": _propose_self_modification,
    "deploy_self": _deploy_self,
    "submit_result": _submit_result,
}


def tool_specs() -> list[dict[str, Any]]:
    """OpenAI-формат function specs — передаётся модели через budget-guard."""
    def spec(name, desc, params, required):
        return {"type": "function", "function": {
            "name": name, "description": textwrap.dedent(desc).strip(),
            "parameters": {"type": "object", "properties": params, "required": required,
                           "additionalProperties": False}}}
    s = {"type": "string"}
    return [
        spec("run_python",
             "Execute Python 3 code in your container. Network egress is available (use "
             "stdlib urllib etc.). Environment variables (TELEGRAM_BOT_TOKEN, "
             "TELEGRAM_OWNER_IDS, REDIS_URL, BUDGET_GUARD_URL...) are inherited, and "
             "modules you wrote to /workspace are importable (cwd is on sys.path). "
             "To run a long-lived process (a bot, a journal loop), launch it detached "
             "(subprocess.Popen with start_new_session=True) AND redirect its "
             "stdin/stdout/stderr to a log file or /dev/null — an inherited pipe makes "
             "this call block until timeout. Returns exit code, stdout, stderr. "
             "Working dir is /workspace.",
             {"code": s, "timeout_sec": {"type": "integer"}}, ["code"]),
        spec("write_file", "Write a file under /workspace or /private.",
             {"path": s, "content": s}, ["path", "content"]),
        spec("read_file", "Read a file under /workspace or /private.", {"path": s}, ["path"]),
        spec("list_dir", "List a directory under /workspace or /private.", {"path": s}, []),
        spec("git_commit", "Commit current changes to your agent branch in /workspace.",
             {"message": s}, ["message"]),
        spec("propose_self_modification",
             "Propose a unified-diff patch to build yourself a new tool/module. It is tested in "
             "an isolated runner before being applied. Use this to grow your own toolkit "
             "instead of expecting helpers to be provided. With target=agent it patches your "
             "OWN source (services/agent) and returns a validated candidate_image WITHOUT "
             "deploying it — call deploy_self with that image to actually become it.",
             {"description": s, "diff": s, "target": {"type": "string", "enum": ["agent", "workspace"]}},
             ["description", "diff"]),
        spec("deploy_self",
             "Deploy a candidate image (from a target=agent self-modification) onto your own "
             "running container, with automatic rollback: selfmod swaps your container to the "
             "new image and waits for your new code to come online; if it doesn't, it rolls "
             "back to the previous image. This RESTARTS you — the current run ends. Use only "
             "after propose_self_modification(target=agent) returned a candidate_image.",
             {"candidate_image": s, "health_timeout": {"type": "integer"}}, ["candidate_image"]),
        spec("submit_result",
             "Mark this task done and record your result. Ends work on this task. "
             "Point artifact_path at the file(s) proving your result (tests, solution, report).",
             {"summary": s, "artifact_path": s}, ["summary", "artifact_path"]),
    ]


def execute(ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
    fn = _IMPL.get(name)
    if fn is None:
        return f"неизвестный инструмент: {name}"
    try:
        return fn(ctx, **args)
    except ToolError as e:
        return f"ToolError: {e}"
    except Exception as e:  # noqa: BLE001 — вернуть модели, чтобы она адаптировалась
        return f"Error in {name}: {type(e).__name__}: {e}"
