"""Минимальный набор примитивов агента ("голый старт", см. README).

Сознательно НЕ содержит helper'ов под конкретные форматы/API/пайплайны — если
агенту нужен такой инструмент, он пишет его себе сам через propose_self_modification
(в свою приватную папку или ветку workspace) и дальше вызывает через run_python.

Каждый инструмент = (OpenAI-совместимая спецификация, функция).
Функция принимает ToolContext и kwargs, возвращает строку (уходит модели как tool_result).
"""
from __future__ import annotations

import json
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
    search_tool_url: str
    selfmod_api_url: str
    cpu_models_url: str
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

def _run_python(ctx: ToolContext, code: str, timeout_sec: int = 30) -> str:
    """Исполняет код в контейнере агента (песочница v1: cgroup-лимиты + internal-сеть).
    Рабочая директория — workspace, туда же агент может писать вспомогательные модули."""
    timeout_sec = min(max(int(timeout_sec), 1), 120)
    try:
        proc = subprocess.run(
            ["python3", "-I", "-c", code],
            cwd=str(ctx.workspace),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return f"TIMEOUT после {timeout_sec}s"
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
         "-c", "user.name", ctx.agent_id, "commit", "-m", message],
    ]
    logs = []
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True)
        logs.append((r.stdout + r.stderr).strip())
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            return f"git error:\n" + "\n".join(logs)
    ctx.audit(task_id=ctx.task_id, action="git_commit", detail=f"{branch}: {message}")
    return "committed:\n" + "\n".join(l for l in logs if l)


def _search_literature(ctx: ToolContext, query: str, max_results: int = 5) -> str:
    r = ctx.http.post(
        f"{ctx.search_tool_url}/v1/search",
        json={"agent_id": ctx.agent_id, "query": query, "max_results": max_results},
    )
    if r.status_code == 429:
        return "квота поиска исчерпана"
    r.raise_for_status()
    res = r.json().get("results", [])
    return json.dumps(res, ensure_ascii=False, indent=2) if res else "ничего не найдено"


def _embed_texts(ctx: ToolContext, texts: list[str]) -> str:
    """Бесплатный self-hosted эмбеддер (cpu-models), для RAG-индекса в приватной папке."""
    r = ctx.http.post(f"{ctx.cpu_models_url}/v1/embed", json={"texts": texts})
    r.raise_for_status()
    d = r.json()
    return f"получено {len(d.get('vectors', []))} векторов, dim={d.get('dim')}"


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
    "search_literature": _search_literature,
    "embed_texts": _embed_texts,
    "propose_self_modification": _propose_self_modification,
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
             "Execute Python 3 code in your container (stdlib + anything you installed via "
             "self-modification). Returns exit code, stdout, stderr. Working dir is /workspace.",
             {"code": s, "timeout_sec": {"type": "integer"}}, ["code"]),
        spec("write_file", "Write a file under /workspace or /private.",
             {"path": s, "content": s}, ["path", "content"]),
        spec("read_file", "Read a file under /workspace or /private.", {"path": s}, ["path"]),
        spec("list_dir", "List a directory under /workspace or /private.", {"path": s}, []),
        spec("git_commit", "Commit current changes to your agent branch in /workspace.",
             {"message": s}, ["message"]),
        spec("search_literature",
             "Search external literature via the controlled search-tool (allowlist + quota). "
             "Your only window to the outside world.",
             {"query": s, "max_results": {"type": "integer"}}, ["query"]),
        spec("embed_texts", "Get embeddings from the free local model (for a RAG index).",
             {"texts": {"type": "array", "items": s}}, ["texts"]),
        spec("propose_self_modification",
             "Propose a unified-diff patch to build yourself a new tool/module. It is tested in "
             "an isolated runner before being applied. Use this to grow your own toolkit "
             "instead of expecting helpers to be provided.",
             {"description": s, "diff": s, "target": {"type": "string", "enum": ["agent", "workspace"]}},
             ["description", "diff"]),
        spec("submit_result",
             "Submit your final result for arbiter evaluation. Ends work on this task. "
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
