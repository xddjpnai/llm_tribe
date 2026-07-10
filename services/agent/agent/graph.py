"""Открытый агентский луп на LangGraph.

Это НЕ фиксированный пайплайн (idea→experiment→writeup). Один узел `reason`
(модель решает следующий шаг) + один узел `act` (исполнение инструментов),
цикл до вызова submit_result / исчерпания бюджета / потолка шагов. Любую
внутреннюю стадийность (генерация идей, ревью, эксперименты) агент вправе
выстроить сам — оркестратор её не навязывает.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from langgraph.graph import END, StateGraph

from .llm import BudgetExhausted, LLMClient, Throttled
from .tools import ToolContext, execute, tool_specs

SYSTEM_PROMPT = """\
You are an autonomous research agent working on program-search / algorithmic-discovery
problems. You start BARE: your only capabilities are the provided primitive tools. There
are NO pre-written helper scripts. If you need a parser, a wrapper, or any new tool, BUILD
IT YOURSELF with propose_self_modification, then call it via run_python.

How you organize the work on a problem is entirely up to you — plan, generate ideas, write
code, run experiments, review, revise, in whatever order works. Verify your results with
run_python before submitting: a result the arbiter cannot reproduce counts as unsolved.

You operate under a hard budget cap for this task. Be economical: prefer local work
(run_python) over literature search, which costs quota, and don't burn steps narrating —
act. Need a helper, parser, or new tool? Build it yourself via propose_self_modification
(patch -> tested in isolation -> applied); nothing beyond the minimal primitives is
provided for you. When you have a reproducible result and a short report, call
submit_result. If you cannot solve it within budget, submit your best partial honestly."""


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
        try:
            res = llm.chat(state.messages, task_id=task_id, tools=specs)
        except BudgetExhausted as e:
            state.done, state.stop_reason = True, f"budget:{e.reason}"
            return state
        except Throttled as e:
            time.sleep(min(e.retry_after_sec, 30))
            state.step -= 1  # шаг не потрачен, просто подождали
            return state

        state.total_cost += res.cost_usd
        if res.fell_back:
            bus.emit("journal.events", {"task_id": task_id,
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
            f"Task id: {task.get('task_id')}\nKind: {task.get('kind')}\n"
            f"Budget cap for this task: ${task.get('cap_usd')}\n\n"
            f"Problem statement:\n{task.get('statement')}"},
    ])
    # recursion_limit с запасом: reason+act на шаг, +буфер на throttle-повторы
    final = graph.invoke(init, config={"recursion_limit": max_steps * 3 + 20})
    return final if isinstance(final, AgentState) else AgentState(**final)
