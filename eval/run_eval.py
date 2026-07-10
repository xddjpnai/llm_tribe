#!/usr/bin/env python3
"""Мини-эвал моделей-кандидатов на задачах program search (шаг 0).

Для каждой пары (модель, задача): запрос кода -> извлечение ```python блока ->
исполнение в subprocess с таймаутом -> метрика (pass-rate или normalized score).
Итог: markdown-таблица + eval_results.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).parent
SYSTEM_PROMPT = (
    "You are an expert competitive programmer. Reply with a single Python "
    "function inside one ```python code block. No explanations outside the block. "
    "Standard library only."
)

# ---------------------------------------------------------------- LLM calls

async def call_openai(client: httpx.AsyncClient, cand: dict, prompt: str) -> tuple[str, int, int]:
    resp = await client.post(
        f"{cand['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {os.environ[cand['api_key_env']]}"},
        json={
            "model": cand["name"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 4096,
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    usage = data.get("usage", {})
    return (
        data["choices"][0]["message"]["content"],
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
    )


def call_anthropic(cand: dict, prompt: str) -> tuple[str, int, int]:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ[cand["api_key_env"]])
    msg = client.messages.create(
        model=cand["name"],
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    if msg.stop_reason == "refusal":
        return "", msg.usage.input_tokens, msg.usage.output_tokens
    text = "".join(b.text for b in msg.content if b.type == "text")
    return text, msg.usage.input_tokens, msg.usage.output_tokens


def extract_code(reply: str) -> str | None:
    m = re.findall(r"```(?:python)?\s*\n(.*?)```", reply, re.DOTALL)
    return m[-1] if m else None

# ------------------------------------------------------------- execution

RUNNER_TEMPLATE = """
import json, sys
{code}

task = json.loads(sys.stdin.read())
fn = globals()[task["function_name"]]
out = []
if task["kind"] == "exact":
    for t in task["tests"]:
        try:
            out.append(fn(*t["args"]) == t["expected"])
        except Exception:
            out.append(False)
else:
    ns = {{}}
    exec(task["scorer"], ns)
    for t in task["tests"]:
        try:
            r = fn(*t["args"])
            out.append(max(0.0, min(1.0, float(ns["score"](r, t["args"])))))
        except Exception:
            out.append(0.0)
print(json.dumps(out))
"""


def run_candidate(code: str, task: dict) -> float:
    """Возвращает метрику 0..1: pass-rate (exact) или средний score (maximize)."""
    runner = RUNNER_TEMPLATE.format(code=textwrap.indent(code, ""))
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(runner)
        path = f.name
    try:
        proc = subprocess.run(
            [sys.executable, "-I", path],
            input=json.dumps(task),
            capture_output=True,
            text=True,
            timeout=task.get("timeout_sec", 15),
        )
        results = json.loads(proc.stdout.strip().splitlines()[-1])
        return sum(float(x) for x in results) / max(len(results), 1)
    except Exception:
        return 0.0
    finally:
        os.unlink(path)

# ------------------------------------------------------------------ main

async def evaluate(cand: dict, tasks: list[dict]) -> dict:
    scores, cost = {}, 0.0
    async with httpx.AsyncClient() as client:
        for task in tasks:
            prompt = (
                f"{task['statement']}\n\n"
                f"Implement it as a Python function `{task['function_name']}`."
            )
            try:
                if cand["protocol"] == "anthropic":
                    reply, tin, tout = await asyncio.to_thread(call_anthropic, cand, prompt)
                else:
                    reply, tin, tout = await call_openai(client, cand, prompt)
            except Exception as e:
                print(f"  [{cand['name']} / {task['id']}] API error: {e}", file=sys.stderr)
                scores[task["id"]] = 0.0
                continue
            cost += tin / 1e6 * cand["price_in_per_mtok"] + tout / 1e6 * cand["price_out_per_mtok"]
            code = extract_code(reply)
            scores[task["id"]] = run_candidate(code, task) if code else 0.0
            print(f"  [{cand['name']} / {task['id']}] score={scores[task['id']]:.2f}")
    mean = sum(scores.values()) / max(len(scores), 1)
    return {"model": cand["name"], "scores": scores, "mean": mean, "cost_usd": round(cost, 4)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", help="подмножество моделей из models.yaml")
    args = ap.parse_args()

    cands = yaml.safe_load((HERE / "models.yaml").read_text())["candidates"]
    if args.models:
        cands = [c for c in cands if c["name"] in args.models]
    missing = [c["name"] for c in cands if not os.environ.get(c["api_key_env"])]
    if missing:
        sys.exit(f"Нет API-ключей для: {', '.join(missing)} — задай env или сузь --models")

    tasks = [json.loads(p.read_text()) for p in sorted((HERE / "tasks").glob("*.json"))]
    print(f"Моделей: {len(cands)}, задач: {len(tasks)}\n")

    results = [await evaluate(c, tasks) for c in cands]
    results.sort(key=lambda r: -r["mean"])

    ids = [t["id"] for t in tasks]
    header = "| model | " + " | ".join(ids) + " | mean | cost $ |"
    sep = "|" + "---|" * (len(ids) + 3)
    print("\n" + header + "\n" + sep)
    for r in results:
        row = " | ".join(f"{r['scores'].get(i, 0):.2f}" for i in ids)
        print(f"| {r['model']} | {row} | **{r['mean']:.2f}** | {r['cost_usd']:.4f} |")

    (HERE / "eval_results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print("\nСохранено в eval_results.json — обнови configs/model_routing.yaml по итогам.")


if __name__ == "__main__":
    asyncio.run(main())
