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

Your early tasks are to build your own infrastructure: a flight journal, a channel to
contact the operator (a Telegram bot — TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_IDS are in
your environment), and a way to receive new tasks from the operator through that channel.
Write that code with run_python + files + git in /workspace, run long-running pieces (the
bot, the journal loop) as DETACHED background processes via run_python, and reuse them on
later tasks.

You have network egress (use it via run_python / stdlib). LLM access is only through the
provided tool path (budget-guard); you have no provider keys yourself.

Before you RELY on new code you wrote (a tool, a deploy, a change to how you operate),
validate it with propose_self_modification, which tests the patch in an isolated sandbox
before applying — so a broken change can't take you down. There is no external judge:
verify your own results with run_python. Be economical with the budget and don't burn
steps narrating — act. Call submit_result when the task is done."""


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
                state.submission = {
                    "summary": args.get("summary", ""),
                    "artifact_path": args.get("artifact_path", ""),
                    "branch": f"agent/{tctx.agent_id}",
                }
                state.done, state.stop_reason = True, "submitted"
                result = "__SUBMITTED__"
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
