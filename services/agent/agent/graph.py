"""Открытый агентский луп на LangGraph.

Это НЕ фиксированный пайплайн (idea→experiment→writeup). Один узел `reason`
(модель решает следующий шаг) + один узел `act` (исполнение инструментов),
цикл до вызова submit_result / исчерпания бюджета / потолка шагов. Любую
внутреннюю стадийность (генерация идей, ревью, эксперименты) агент вправе
выстроить сам — оркестратор её не навязывает.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from langgraph.graph import END, StateGraph

from .llm import LLMClient
from .tools import ToolContext, execute, tool_specs

SYSTEM_PROMPT = """\
You are an autonomous, self-improving agent. You start BARE: your only capabilities are
the provided primitive tools (run_python, read/write/list files, git_commit,
propose_self_modification, submit_result). NOTHING else is pre-built for you — no journal,
no messaging, no task system, no helper libraries. You build whatever you need yourself.

Your early tasks are to build your own infrastructure: a flight journal, a Telegram channel
to the operator, and task intake through that channel. Later tasks arrive as free text via
the intake you (or a fellow agent) built. Other agents work in the same /workspace on their
own git branches; reuse what they already built instead of duplicating it.

HOW TO WORK:
- Put real code in files under /workspace with write_file (e.g. /workspace/comms/bot.py),
  then test via run_python (cwd is /workspace and it is on sys.path, so your modules are
  importable). Don't cram whole programs into one run_python string.
- Python packages already installed: httpx, redis, pyyaml. Anything else: you have network
  egress and may install into your own environment if truly needed.
- Environment inherited by run_python: AGENT_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_IDS
  (comma-separated numeric ids), REDIS_URL, BUDGET_GUARD_URL, SELFMOD_API_URL.
- LLM access (you hold no provider keys): POST {BUDGET_GUARD_URL}/v1/chat with JSON
  {"agent_id", "role": "journal"|"comms"|"routine", "messages": [...], "max_tokens"}
  -> {"content", "cost_usd", ...}.
- Shared state = Redis (redis.from_url(REDIS_URL)). Conventions: list `events` is
  telemetry (RPUSH JSON lines); list `tasks` is the task queue (RPUSH a JSON object
  {"id": ..., "statement": ...}; every agent BLPOPs it); `claim:<id>` keys are one-shot
  claims via SET NX.
- Telegram: plain HTTPS Bot API (httpx/urllib) — getUpdates long polling with offset,
  sendMessage. Only act on messages whose chat id is in TELEGRAM_OWNER_IDS. A bot CANNOT
  message a user who never pressed Start: if sendMessage returns 403, keep polling and
  greet the owner once they show up in getUpdates.
- Long-lived processes (bot, journal loop): launch DETACHED via run_python —
  subprocess.Popen(..., start_new_session=True) with stdin/stdout/stderr redirected to a
  log file or /dev/null (an inherited pipe hangs the run_python call until timeout). A few
  seconds later read the log to confirm it survived. Processes die on container restart —
  keep code relaunchable and state in files/Redis, not in memory.
- Before you RELY on code you wrote, validate it with propose_self_modification (target
  "workspace"): send a unified diff ("--- a/path", "+++ b/path", "/dev/null" side for
  new/deleted files). It is applied to a copy, tested in an isolated sandbox (all .py must
  compile; tests/ must pass if present), and only then applied and committed — so a broken
  change can't take you down.

JUDGEMENT: you cannot declare a task solved — the sage (an impartial judge on a different
model) decides. On submit_result the sage checks out your git branch FRESH and runs
`python3 <artifact_path>` (path relative to the workspace root) WITHOUT your running
processes and WITHOUT your Telegram secrets. So the artifact you submit must be committed
(git_commit first!), self-contained, need no arguments or input, avoid depending on live
external services (use fakes/samples in a demo), and exit 0 within 30 seconds while
printing evidence of what was built. If the verdict is unsolved, read the reason, fix,
resubmit. Be economical with the budget and don't burn steps narrating — act."""


# NB: аннотации в этом классе — не PEP604 (`X | None`), а typing.Optional/List/Dict.
# LangGraph резолвит их через get_type_hints в рантайме, поэтому схема графа должна
# вычисляться на любом интерпретаторе. Остальной код сервиса — обычный 3.12-стиль.
@dataclass
class AgentState:
    task: Dict[str, Any]
    messages: List[Dict[str, Any]] = field(default_factory=list)
    step: int = 0
    total_cost: float = 0.0
    done: bool = False
    submission: Optional[Dict[str, Any]] = None
    stop_reason: str = ""


def _consult_sage(tctx: ToolContext, task: dict, summary: str, artifact: str, branch: str) -> dict:
    """Спросить вердикт у мудреца. При недоступности — считаем unsolved (агент
    не может сам себя объявить решившим в обход судьи)."""
    try:
        r = tctx.http.post(f"{tctx.sage_url}/v1/judge", json={
            "task_id": tctx.task_id, "statement": task.get("statement", ""),
            "summary": summary, "artifact_ref": artifact, "branch": branch}, timeout=360)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        return {"verdict": "unsolved", "quality": 0.0, "reproducible": False,
                "reason": f"sage недоступен: {e}"}


def build_graph(llm: LLMClient, tctx: ToolContext, bus, max_steps: int):
    specs = tool_specs()
    task_id = tctx.task_id

    def reason(state: AgentState) -> AgentState:
        if state.step >= max_steps:
            state.done, state.stop_reason = True, "max_steps"
            return state
        state.step += 1
        res = llm.chat(state.messages, task_id=task_id, tools=specs)
        state.total_cost += res.cost_usd
        if res.fell_back:
            bus.emit("agent", {"task_id": task_id,
                     "action": "provider_fallback", "detail": f"ответила {res.model}"})

        assistant: dict[str, Any] = {"role": "assistant", "content": res.content or ""}
        if res.tool_calls:
            assistant["tool_calls"] = res.tool_calls
        state.messages.append(assistant)

        if not res.tool_calls:
            # модель заговорила без действия — подтолкнуть к инструменту или submit
            state.messages.append({"role": "user", "content":
                "Take a concrete action with a tool, or call submit_result if you are done."})
        return state

    def act(state: AgentState) -> AgentState:
        last = state.messages[-1]
        for call in last.get("tool_calls", []):
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            bus.audit(task_id=task_id, action=f"tool:{name}",
                      detail=json.dumps(args, ensure_ascii=False)[:500])

            if name == "submit_result":
                summary = args.get("summary", "")
                artifact = args.get("artifact_path", "")
                branch = f"agent/{tctx.agent_id}"
                verdict = _consult_sage(tctx, state.task, summary, artifact, branch)
                bus.emit("verdict", {"task_id": task_id, **verdict})
                if verdict.get("verdict") == "solved":
                    state.submission = {"summary": summary, "artifact_path": artifact,
                                        "branch": branch, "verdict": verdict}
                    state.done, state.stop_reason = True, "solved"
                    result = f"SAGE: solved (quality={verdict.get('quality')}). {verdict.get('reason','')}"
                else:
                    # мудрец завернул работу — вернуть причину, дать доработать
                    result = (f"SAGE VERDICT: unsolved (quality={verdict.get('quality')}, "
                              f"reproducible={verdict.get('reproducible')}).\n"
                              f"Reason: {verdict.get('reason','')}\n"
                              f"Fix the issue and submit again, or improve your artifact "
                              f"(commit it to your branch first so the sage can reproduce it).")
            else:
                result = execute(tctx, name, args)

            state.messages.append({
                "role": "tool", "tool_call_id": call.get("id", name),
                "name": name, "content": result[:12000],
            })
        return state

    def route(state: AgentState) -> Literal["act", "reason", "__end__"]:
        if state.done:
            return END
        return "act" if state.messages[-1].get("tool_calls") else "reason"

    g = StateGraph(AgentState)
    g.add_node("reason", reason)
    g.add_node("act", act)
    g.set_entry_point("reason")
    g.add_conditional_edges("reason", route, {"act": "act", "reason": "reason", END: END})
    g.add_conditional_edges("act", lambda s: END if s.done else "reason",
                            {"reason": "reason", END: END})
    return g.compile()


def run_task(task: dict[str, Any], llm: LLMClient, tctx: ToolContext, bus, max_steps: int) -> AgentState:
    graph = build_graph(llm, tctx, bus, max_steps)
    init = AgentState(task=task, messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            f"Task id: {task.get('id') or task.get('task_id')}\n"
            f"Kind: {task.get('kind', 'open')}\n\n"
            f"Task:\n{task.get('statement')}"},
    ])
    # recursion_limit с запасом: reason+act на шаг, +буфер на throttle-повторы
    final = graph.invoke(init, config={"recursion_limit": max_steps * 3 + 20})
    return final if isinstance(final, AgentState) else AgentState(**final)
