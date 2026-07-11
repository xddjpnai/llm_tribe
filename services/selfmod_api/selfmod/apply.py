"""Применение unified-diff к рабочей копии. Чистая логика, без docker —
тестируется офлайн на настоящем git-репозитории.

Патч НИКОГДА не применяется к живому исходнику напрямую: сначала во временную
копию, там валидируется, и только при успехе переносится в целевой каталог.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ApplyResult:
    ok: bool
    log: str
    workdir: Path | None   # временная копия с применённым патчем (для последующей валидации)


def _run(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def stage_and_apply(source_dir: Path, diff: str) -> ApplyResult:
    """Копирует source_dir во временный каталог, инициализирует git (если нужно),
    проверяет применимость патча (`git apply --check`) и применяет. Возвращает
    временную копию — вызывающий валидирует её в изоляции и решает судьбу."""
    if not diff.strip():
        return ApplyResult(False, "пустой diff", None)

    workdir = Path(tempfile.mkdtemp(prefix="selfmod_"))
    dst = workdir / "src"
    shutil.copytree(source_dir, dst, dirs_exist_ok=True)

    # git нужен только как надёжный движок применения патча; если репо нет — временный
    if (dst / ".git").exists():
        # copytree меняет inode/stat файлов — скопированный индекс становится
        # «протухшим», и `git apply --3way` отвергает любой diff по существующему
        # файлу («does not match index»). Освежаем stat-информацию индекса.
        _run(["git", "update-index", "-q", "--refresh"], cwd=str(dst))
    else:
        for cmd in (["git", "init", "-q"],
                    ["git", "-c", "user.email=selfmod@llm-tribe", "-c", "user.name=selfmod",
                     "add", "-A"],
                    ["git", "-c", "user.email=selfmod@llm-tribe", "-c", "user.name=selfmod",
                     "commit", "-q", "-m", "base", "--allow-empty"]):
            rc, out = _run(cmd, cwd=str(dst))
            if rc != 0:
                shutil.rmtree(workdir, ignore_errors=True)
                return ApplyResult(False, f"git init failed: {out}", None)

    diff_file = workdir / "patch.diff"
    diff_file.write_text(diff if diff.endswith("\n") else diff + "\n")

    # 3-way + whitespace-терпимость повышают шанс применения diff от модели
    check_rc, check_out = _run(
        ["git", "apply", "--check", "--3way", "--whitespace=nowarn", str(diff_file)],
        cwd=str(dst))
    if check_rc != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        return ApplyResult(False, f"патч не применяется:\n{check_out}", None)

    apply_rc, apply_out = _run(
        ["git", "apply", "--3way", "--whitespace=nowarn", str(diff_file)], cwd=str(dst))
    if apply_rc != 0:
        shutil.rmtree(workdir, ignore_errors=True)
        return ApplyResult(False, f"git apply failed:\n{apply_out}", None)

    return ApplyResult(True, "патч применён к временной копии", dst)


def promote(patched_src: Path, target_dir: Path) -> str:
    """Переносит валидированную копию в целевой каталог (после успешных тестов)."""
    for item in patched_src.iterdir():
        if item.name == ".git":
            continue
        dest = target_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)
    return f"перенесено в {target_dir}"


def cleanup(workdir: Path | None) -> None:
    if workdir and workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
