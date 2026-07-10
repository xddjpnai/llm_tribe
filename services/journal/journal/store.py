"""Хранилище бортового журнала. Отдельно от ClickHouse-трейсов: это
человекочитаемый нарратив, а не агрегаты.

Раскладка в /journal:
  raw/<scope>.jsonl     — сырые события (лог), scope = system | agent:<id> | task:<id>
  raw/<scope>.cursor    — сколько строк уже просуммировано (чтобы не повторять)
  <scope>.md            — нарратив: саммари с таймстампами, дописывается вниз

Чистые файловые операции — тестируется офлайн.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _safe(scope: str) -> str:
    return scope.replace("/", "_").replace(":", "-")


class JournalStore:
    def __init__(self, root: str = "/journal"):
        self.root = Path(root)
        (self.root / "raw").mkdir(parents=True, exist_ok=True)

    # ------------------------------- запись сырых событий -------------------------------
    def append_event(self, event: dict[str, Any]) -> list[str]:
        """Раскладывает событие по scope'ам (system + agent + task). Возвращает
        затронутые scope'ы (чтобы вызывающий знал, где копятся новые события)."""
        scopes = ["system"]
        if event.get("agent_id"):
            scopes.append(f"agent:{event['agent_id']}")
        if event.get("task_id"):
            scopes.append(f"task:{event['task_id']}")
        line = json.dumps(event, ensure_ascii=False)
        for sc in scopes:
            with open(self.root / "raw" / f"{_safe(sc)}.jsonl", "a") as f:
                f.write(line + "\n")
        return scopes

    # ------------------------ непросуммированные события scope --------------------------
    def _cursor_path(self, scope: str) -> Path:
        return self.root / "raw" / f"{_safe(scope)}.cursor"

    def pending(self, scope: str) -> list[dict[str, Any]]:
        raw = self.root / "raw" / f"{_safe(scope)}.jsonl"
        if not raw.exists():
            return []
        lines = raw.read_text().splitlines()
        cursor = int(self._cursor_path(scope).read_text()) if self._cursor_path(scope).exists() else 0
        return [json.loads(l) for l in lines[cursor:] if l.strip()]

    def advance_cursor(self, scope: str) -> None:
        raw = self.root / "raw" / f"{_safe(scope)}.jsonl"
        n = len(raw.read_text().splitlines()) if raw.exists() else 0
        self._cursor_path(scope).write_text(str(n))

    def scopes_with_raw(self) -> list[str]:
        out = []
        for p in (self.root / "raw").glob("*.jsonl"):
            name = p.stem
            if name == "system":
                out.append("system")
            elif name.startswith("agent-"):
                out.append("agent:" + name[len("agent-"):])
            elif name.startswith("task-"):
                out.append("task:" + name[len("task-"):])
        return out

    # ------------------------------- нарратив (markdown) -------------------------------
    def append_summary(self, scope: str, summary: str) -> None:
        md = self.root / f"{_safe(scope)}.md"
        header = f"\n## {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} — {scope}\n\n"
        with open(md, "a") as f:
            if md.stat().st_size == 0 if md.exists() else True:
                f.write(f"# Бортовой журнал — {scope}\n")
            f.write(header + summary.strip() + "\n")

    def read_markdown(self, scope: str) -> str:
        md = self.root / f"{_safe(scope)}.md"
        return md.read_text() if md.exists() else ""
