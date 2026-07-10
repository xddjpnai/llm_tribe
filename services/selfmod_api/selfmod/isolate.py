"""Валидация патча в изоляции. Тот же принцип песочницы, что и у экспериментов
агента: без доступа к хосту, без сети, с ресурсными лимитами.

Патченная копия доставляется в одноразовый контейнер через put_archive (tar по
docker API), а НЕ bind-mount'ом: selfmod сам работает в контейнере, и путь его
/tmp не существует на хосте — bind-mount через docker.sock дал бы пустой каталог
и валидацию-пустышку.

Протокол Runner позволяет подменить реальный DockerRunner фейком в тестах.
"""
from __future__ import annotations

import io
import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger("selfmod.isolate")


@dataclass
class RunResult:
    passed: bool
    logs: str
    candidate_image: str | None = None   # для target=agent: собранный, но НЕ развёрнутый образ


# Команда валидации: синтаксис всех .py обязан быть корректным (compile() в памяти —
# py_compile писал бы .pyc), а если есть tests/ — тесты обязаны пройти (pytest, если
# доступен, иначе stdlib unittest). Патч агента ДОЛЖЕН оставить это зелёным, иначе откат.
DEFAULT_VALIDATION = (
    "set -e; "
    "python -c \"import pathlib; "
    "[compile(p.read_text(errors='replace'), str(p), 'exec') "
    "for p in pathlib.Path('.').rglob('*.py')]\"; "
    "if [ -d tests ]; then "
    "if python -c 'import pytest' >/dev/null 2>&1; then python -m pytest -q tests; "
    "else python -m unittest discover -q -s tests -t .; fi; "
    "fi"
)


class Runner(Protocol):
    def validate_workspace(self, patched_src: Path) -> RunResult: ...
    def validate_and_build_agent(self, patched_src: Path, agent_id: str,
                                 patch_id: str) -> RunResult: ...


def _tar_dir(src: Path, arcname: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(src), arcname=arcname)
    return buf.getvalue()


class DockerRunner:
    """Реальный раннер через docker.sock. Изоляция рантайма: network_disabled,
    mem/pids лимиты, снятые capabilities. Импорт docker — ленивый, чтобы модуль
    оставался импортируемым в средах без SDK (тесты).

    validation_context: build-контекст агента; из него один раз собирается образ
    валидации workspace-патчей — те же зависимости (redis/httpx/pytest...), что и
    в рантайме агента, где патченный код реально будет работать. Без него тесты
    агента падали бы на импортах в голом python:3.12-slim.
    """

    VALIDATION_TAG = "llm-tribe/validation:latest"

    def __init__(self, base_image: str = "python:3.12-slim",
                 mem_limit: str = "1g", pids_limit: int = 128, timeout_sec: int = 300,
                 validation_context: str | None = None):
        self.base_image = base_image
        self.mem_limit = mem_limit
        self.pids_limit = pids_limit
        self.timeout_sec = timeout_sec
        self.validation_context = validation_context
        self._validation_image: str | None = None

    def _client(self):
        import docker  # noqa: PLC0415 — ленивый импорт

        return docker.from_env()

    def _workspace_image(self) -> str:
        """Образ для валидации workspace-патчей: собирается лениво один раз из
        build-контекста агента; при неудаче — базовый python (лучше, чем ничего)."""
        if self._validation_image:
            return self._validation_image
        if self.validation_context:
            try:
                # сборке нужна сеть (apt/pip); изоляция без сети — только на РАНТАЙМ
                self._client().images.build(path=self.validation_context,
                                            tag=self.VALIDATION_TAG, rm=True, forcerm=True)
                self._validation_image = self.VALIDATION_TAG
                return self._validation_image
            except Exception as e:  # noqa: BLE001
                log.warning("сборка образа валидации не удалась (%s), беру %s",
                            e, self.base_image)
        self._validation_image = self.base_image
        return self._validation_image

    def _run_validation(self, patched_src: Path, image: str,
                        inject_src: bool) -> RunResult:
        client = self._client()
        container = None
        try:
            container = client.containers.create(
                image=image,
                command=["bash", "-lc", DEFAULT_VALIDATION],
                working_dir="/patched" if inject_src else "/app",
                user="root",                    # put_archive/кэши тестов пишут в /
                network_disabled=True,          # без сети — эксперимент не ходит наружу
                mem_limit=self.mem_limit,
                pids_limit=self.pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
            )
            if inject_src:
                container.put_archive("/", _tar_dir(patched_src, "patched"))
            container.start()
            exit_code = container.wait(timeout=self.timeout_sec).get("StatusCode", 1)
            logs = container.logs().decode(errors="replace")[-8000:]
            return RunResult(exit_code == 0, logs)
        except Exception as e:  # noqa: BLE001
            return RunResult(False, f"docker validation error: {type(e).__name__}: {e}")
        finally:
            if container is not None:
                try:
                    container.remove(force=True)   # и при таймауте wait — не течём
                except Exception:  # noqa: BLE001
                    pass

    def validate_workspace(self, patched_src: Path) -> RunResult:
        return self._run_validation(patched_src, self._workspace_image(), inject_src=True)

    def validate_and_build_agent(self, patched_src: Path, agent_id: str,
                                 patch_id: str) -> RunResult:
        """Собирает КАНДИДАТ-образ из патченного контекста агента (валидирует в т.ч.
        Dockerfile), гоняет тесты внутри него. Образ помечается, но НЕ разворачивается —
        прямой рестарт прод-контейнера агентом запрещён; свап делается контролируемо."""
        client = self._client()
        tag = f"llm-tribe/agent:candidate-{agent_id}-{patch_id}"
        try:
            # сеть при сборке нужна (apt-get/pip в Dockerfile агента); песочница
            # без сети применяется к ЗАПУСКУ кандидата ниже
            _img, build_logs = client.images.build(path=str(patched_src), tag=tag,
                                                   rm=True, forcerm=True)
            build_text = "".join(chunk.get("stream", "") for chunk in build_logs)[-4000:]
        except Exception as e:  # noqa: BLE001
            return RunResult(False, f"build failed: {type(e).__name__}: {e}")

        run = self._run_validation(patched_src, tag, inject_src=False)
        if not run.passed:
            try:
                client.images.remove(tag, force=True)   # откат: убрать невалидный образ
            except Exception:  # noqa: BLE001
                pass
            return RunResult(False, f"{build_text}\n--- validation ---\n{run.logs}")
        return RunResult(True, f"{build_text}\n--- validation ---\n{run.logs}",
                         candidate_image=tag)
