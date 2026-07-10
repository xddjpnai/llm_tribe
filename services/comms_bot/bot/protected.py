"""ЗАЩИЩЁННОЕ ЯДРО бота. Агентам ЗАПРЕЩЕНО менять этот файл — selfmod-api
отклоняет любой патч, касающийся protected-путей (см. services/selfmod_api).

Здесь всё, что даёт КОНТРОЛЬ над системой и над тем, кто ей управляет:
  - проверка пользователя (владелец / участник);
  - kill-switch (/kill, /stop, /pause, /resume);
  - управление доступом (/user) — только владелец.

Владельцы задаются в файле кредов (TELEGRAM_OWNER_IDS) — их я вписываю сам, они
недоступны агентам и не удаляются через /user. Участники добавляются владельцем
через /user и хранятся в отдельном volume, смонтированном ТОЛЬКО в бот.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import httpx

_http = httpx.Client(timeout=15.0)
ORCH_URL = os.environ.get("ORCHESTRATOR_URL", "http://orchestrator:8000").rstrip("/")

# владельцы из кредов (недоступны агентам, не удаляются через /user)
OWNER_IDS = {int(x) for x in os.environ.get("TELEGRAM_OWNER_IDS", "").replace(" ", "").split(",") if x}
# участники, добавленные через /user — на bot-only volume (агенты его не монтируют)
MEMBERS_PATH = Path(os.environ.get("AUTHZ_STORE", "/authz/members.json"))
_lock = threading.Lock()

# команды, которые обрабатываются ТОЛЬКО здесь (мимо изменяемых обработчиков)
PROTECTED_COMMANDS = {"kill", "stop", "pause", "resume", "user"}


def _load_members() -> set[int]:
    if MEMBERS_PATH.exists():
        try:
            return {int(x) for x in json.loads(MEMBERS_PATH.read_text())}
        except Exception:  # noqa: BLE001
            return set()
    return set()


def _save_members(ids: set[int]) -> None:
    MEMBERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MEMBERS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ids)))
    tmp.replace(MEMBERS_PATH)


def is_owner(uid: int) -> bool:
    return uid in OWNER_IDS


def is_member(uid: int) -> bool:
    # владелец всегда участник; плюс добавленные через /user
    return uid in OWNER_IDS or uid in _load_members()


def owners() -> set[int]:
    return set(OWNER_IDS)


def _handle_user(uid: int, rest: str) -> str:
    if not is_owner(uid):
        return "команда /user доступна только владельцу"
    parts = rest.split()
    if not parts or parts[0] == "list":
        return (f"владельцы (из кредов, полный доступ): {sorted(OWNER_IDS)}\n"
                f"добавленные участники: {sorted(_load_members()) or '—'}")
    action = parts[0]
    if action not in ("add", "remove") or len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return "usage: /user add <id> | /user remove <id> | /user list"
    target = int(parts[1])
    with _lock:
        members = _load_members()
        if action == "add":
            if target in OWNER_IDS:
                return f"{target} — владелец, уже имеет полный доступ"
            members.add(target)
            _save_members(members)
            return f"✅ доступ выдан: {target}"
        members.discard(target)      # remove; владельца из кредов это не трогает
        _save_members(members)
        return f"доступ отозван: {target}"


def _handle_kill(cmd: str, rest: str) -> str:
    target = rest.strip() or "all"
    action = "pause" if cmd == "pause" else ("resume" if cmd == "resume" else "stop")
    try:
        r = _http.post(f"{ORCH_URL}/v1/kill", json={"target": target, "action": action})
        r.raise_for_status()
        return f"🛑 kill-switch: {action} -> {target}\n{r.json()}"
    except httpx.HTTPError as e:
        return f"kill-switch: ошибка связи с оркестратором: {e}"


def dispatch(text: str, uid: int, cmd: str, rest: str) -> tuple[bool, str | None]:
    """Единая точка контроля, вызывается ДО изменяемых обработчиков.
    Возвращает (handled, reply):
      - не участник           -> (True, None)  — молчаливое игнорирование, дальше не идём
      - protected-команда     -> (True, reply) — обработана здесь
      - обычная команда       -> (False, None) — вызывающий передаёт в изменяемый handlers
    """
    if not is_member(uid):
        return True, None
    if cmd in ("kill", "stop", "pause", "resume"):
        return True, _handle_kill(cmd, rest)
    if cmd == "user":
        return True, _handle_user(uid, rest)
    return False, None
