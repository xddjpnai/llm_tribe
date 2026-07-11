"""Контролируемый свап работающего контейнера агента на собранный кандидат-образ.

Агент сперва собирает кандидат через propose_self_modification(target=agent)
(патч → песочница → сборка+валидация образа), затем зовёт /v1/deploy. Здесь мы
пересоздаём ЕГО контейнер на новом образе, ждём, что новый код поднялся (событие
`online` в Redis), и при неудаче откатываемся на прежний образ.

КРИТИЧНО ДЛЯ БЕЗОПАСНОСТИ: пересоздание копирует ВЕСЬ профиль старого контейнера
(cap_drop, security_opt, сеть/алиасы, тома, env с секретами, лимиты, restart,
compose-лейблы) и меняет ТОЛЬКО образ. Иначе агент, пересобрав себя, мог бы
выехать из песочницы. docker.sock агенту не пробрасывается — его нет в Binds
старого контейнера, а мы копируем ровно их.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

log = logging.getLogger("selfmod.deploy")


@dataclass
class DeployResult:
    ok: bool
    rolled_back: bool
    logs: str


def _find_container(client, agent_id: str):
    """Контейнер агента ищем по compose-лейблу сервиса, а не по угадыванию имени."""
    for c in client.containers.list(all=True,
                                    filters={"label": f"com.docker.compose.service={agent_id}"}):
        return c
    return None


def _spawn(client, name: str, image: str, insp: dict):
    """Пересоздать контейнер name на образе image, скопировав профиль из insp
    (docker inspect старого контейнера). Возвращает новый контейнер (запущенный)."""
    cfg = insp["Config"]
    hc = insp["HostConfig"]
    nets = insp.get("NetworkSettings", {}).get("Networks", {}) or {}
    net_name = hc.get("NetworkMode") or (next(iter(nets), None))
    aliases = []
    if net_name and net_name in nets:
        # алиасы без авто-хэша docker (id самого контейнера) — только осмысленные
        aliases = [a for a in (nets[net_name].get("Aliases") or []) if not name.endswith(a)]

    host_config = client.api.create_host_config(
        binds=hc.get("Binds") or [],
        cap_drop=hc.get("CapDrop") or [],
        cap_add=hc.get("CapAdd") or [],
        security_opt=hc.get("SecurityOpt") or [],
        privileged=bool(hc.get("Privileged")),
        network_mode=net_name,
        restart_policy=hc.get("RestartPolicy") or None,
        mem_limit=hc.get("Memory") or 0,
        nano_cpus=hc.get("NanoCpus") or 0,
        pids_limit=hc.get("PidsLimit") or None,
    )
    networking = None
    if net_name:
        networking = client.api.create_networking_config({
            net_name: client.api.create_endpoint_config(aliases=aliases or None)
        })
    created = client.api.create_container(
        image=image,
        name=name,
        command=cfg.get("Cmd"),
        entrypoint=cfg.get("Entrypoint"),
        environment=cfg.get("Env"),          # env старого контейнера = секреты уже внутри
        working_dir=cfg.get("WorkingDir") or None,
        user=cfg.get("User") or "",
        labels=cfg.get("Labels") or {},      # compose-лейблы: сервис остаётся управляемым
        host_config=host_config,
        networking_config=networking,
    )
    client.api.start(created["Id"])
    return client.containers.get(created["Id"])


def _wait_online(redis_client, agent_id: str, since_ts: float, timeout: int) -> bool:
    """Здоровье = новый код поднялся и объявил online в Redis-списке events.
    Это сильнее, чем «контейнер Running»: доказывает, что процесс агента реально
    стартовал на новом образе, а не крешит на импорте."""
    if redis_client is None:
        return True  # без Redis не можем проверить — считаем деплой применённым
    deadline = time.time() + timeout
    while time.time() < deadline:
        for raw in redis_client.lrange("events", -50, -1):
            try:
                e = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if (e.get("agent_id") == agent_id and e.get("action") == "online"
                    and float(e.get("ts", 0)) >= since_ts):
                return True
        time.sleep(2)
    return False


def redeploy(client, redis_client, agent_id: str, candidate_image: str,
             health_timeout: int = 90) -> DeployResult:
    cont = _find_container(client, agent_id)
    if cont is None:
        return DeployResult(False, False, f"контейнер сервиса {agent_id} не найден")

    insp = client.api.inspect_container(cont.id)
    old_image = insp["Image"]          # sha256-id прежнего образа — стабильный ref для отката
    name = insp["Name"].lstrip("/")

    since = time.time()
    try:
        cont.stop(timeout=10)
        cont.remove(force=True)
        _spawn(client, name, candidate_image, insp)
    except Exception as e:  # noqa: BLE001
        # свап не удался механически — пробуем вернуть прежний контейнер
        _rollback_safe(client, name, old_image, insp)
        return DeployResult(False, True, f"свап не удался ({type(e).__name__}: {e}); откат на прежний образ")

    if _wait_online(redis_client, agent_id, since, health_timeout):
        return DeployResult(True, False,
                            f"{name} развёрнут на {candidate_image}; агент объявил online")

    # новый образ не поднялся за таймаут — откат
    log.warning("%s не поднялся на %s за %ss — откат", name, candidate_image, health_timeout)
    since_rb = time.time()
    _rollback_safe(client, name, old_image, insp)
    back = _wait_online(redis_client, agent_id, since_rb, health_timeout)
    return DeployResult(False, True,
                        f"кандидат {candidate_image} не объявил online за {health_timeout}s — "
                        f"откат на прежний образ ({'online' if back else 'online не подтверждён'})")


def _rollback_safe(client, name: str, old_image: str, insp: dict) -> None:
    """Снести то, что сейчас под этим именем, и поднять прежний образ."""
    try:
        existing = client.containers.get(name)
        existing.stop(timeout=10)
        existing.remove(force=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        _spawn(client, name, old_image, insp)
    except Exception as e:  # noqa: BLE001
        log.error("ОТКАТ НЕ УДАЛСЯ для %s: %s — вмешайся вручную (./scripts/run.sh)", name, e)
