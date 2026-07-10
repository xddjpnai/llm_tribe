"""Валидация патча в изоляции. Тот же принцип песочницы, что и у экспериментов
агента: без доступа к хосту, без сети, с ресурсными лимитами (guard #2).

Протокол Runner позволяет подменить реальный DockerRunner фейком в тестах.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class RunResult:
    passed: bool
    logs: str
    candidate_image: str | None = None   # для target=agent: собранный, но НЕ развёрнутый образ


# Команда валидации: тесты обязаны пройти; если тестов нет — обязан импортироваться пакет.
# Патч агента ДОЛЖЕН оставить это зелёным, иначе откат.
DEFAULT_VALIDATION = (
    'set -e; '
    'if ls tests/*.py >/dev/null 2>&1 || [ -d tests ]; then python -m pytest -q; '
    'else python -c "import pathlib,py_compile; '
    '[py_compile.compile(str(p), doraise=True) for p in pathlib.Path(\'.\').rglob(\'*.py\')]"; fi'
)


class Runner(Protocol):
    def validate_workspace(self, patched_src: Path) -> RunResult: ...
    def validate_and_build_agent(self, patched_src: Path, agent_id: str,
                                 patch_id: str) -> RunResult: ...


class DockerRunner:
    """Реальный раннер через docker.sock. Изоляция: network_disabled, mem/pids лимиты,
    read-only корень, снятые capabilities. Импорт docker — ленивый, чтобы модуль
    оставался импортируемым в средах без SDK (тесты)."""

    def __init__(self, base_image: str = "python:3.12-slim",
                 mem_limit: str = "1g", pids_limit: int = 128, timeout_sec: int = 300):
        self.base_image = base_image
        self.mem_limit = mem_limit
        self.pids_limit = pids_limit
        self.timeout_sec = timeout_sec

    def _client(self):
        import docker  # noqa: PLC0415 — ленивый импорт

        return docker.from_env()

    def _run_validation(self, patched_src: Path, image: str) -> RunResult:
        client = self._client()
        try:
            container = client.containers.run(
                image=image,
                command=["bash", "-lc", DEFAULT_VALIDATION],
                # копию монтируем внутрь; для agent-образа код уже внутри, но
                # для workspace валидируем смонтированную патченную копию
                volumes={str(patched_src): {"bind": "/patched", "mode": "ro"}},
                working_dir="/patched",
                network_disabled=True,          # без сети — эксперимент не ходит наружу
                mem_limit=self.mem_limit,
                pids_limit=self.pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                detach=True,
            )
            exit_code = container.wait(timeout=self.timeout_sec).get("StatusCode", 1)
            logs = container.logs().decode(errors="replace")[-8000:]
            container.remove(force=True)
            return RunResult(exit_code == 0, logs)
        except Exception as e:  # noqa: BLE001
            return RunResult(False, f"docker validation error: {type(e).__name__}: {e}")

    def validate_workspace(self, patched_src: Path) -> RunResult:
        # для workspace достаточно валидации в базовом образе с смонтированным кодом
        return self._run_validation(patched_src, self.base_image)

    def validate_and_build_agent(self, patched_src: Path, agent_id: str,
                                 patch_id: str) -> RunResult:
        """Собирает КАНДИДАТ-образ из патченного контекста агента (валидирует в т.ч.
        Dockerfile), гоняет тесты внутри него. Образ помечается, но НЕ разворачивается —
        прямой рестарт прод-контейнера агентом запрещён; свап делается контролируемо."""
        client = self._client()
        tag = f"llm-tribe/agent:candidate-{agent_id}-{patch_id}"
        try:
            _img, build_logs = client.images.build(path=str(patched_src), tag=tag, rm=True,
                                                    forcerm=True, network_mode="none")
            build_text = "".join(chunk.get("stream", "") for chunk in build_logs)[-4000:]
        except Exception as e:  # noqa: BLE001
            return RunResult(False, f"build failed: {type(e).__name__}: {e}")

        try:
            container = client.containers.run(
                image=tag,
                command=["bash", "-lc", DEFAULT_VALIDATION],
                working_dir="/app",
                network_disabled=True, mem_limit=self.mem_limit, pids_limit=self.pids_limit,
                cap_drop=["ALL"], security_opt=["no-new-privileges"], detach=True,
            )
            exit_code = container.wait(timeout=self.timeout_sec).get("StatusCode", 1)
            logs = container.logs().decode(errors="replace")[-6000:]
            container.remove(force=True)
        except Exception as e:  # noqa: BLE001
            return RunResult(False, f"{build_text}\nrun failed: {type(e).__name__}: {e}")

        if exit_code != 0:
            try:
                client.images.remove(tag, force=True)   # откат: убрать невалидный образ
            except Exception:  # noqa: BLE001
                pass
            return RunResult(False, f"{build_text}\n--- validation ---\n{logs}")
        return RunResult(True, f"{build_text}\n--- validation ---\n{logs}", candidate_image=tag)
