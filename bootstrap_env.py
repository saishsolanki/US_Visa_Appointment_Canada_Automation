#!/usr/bin/env python3
"""Reproducible Python environment bootstrapper for this repository."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path


REQUIRED_IMPORTS = ["selenium", "flask", "requests"]


def _run(cmd: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(cmd, cwd=str(cwd), check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(cmd)}")


def _venv_python(venv_dir: Path) -> Path:
    if sys.platform.startswith("win"):
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_pip_ok(venv_dir: Path) -> bool:
    python_bin = _venv_python(venv_dir)
    if not python_bin.exists():
        return False
    probe = subprocess.run([str(python_bin), "-m", "pip", "--version"], check=False, capture_output=True, text=True)
    return probe.returncode == 0


def _remove_venv_dir(venv_dir: Path) -> None:
    """Remove an existing virtualenv directory, tolerating transient Windows locks."""
    if not venv_dir.exists():
        return

    try:
        shutil.rmtree(venv_dir)
        return
    except OSError:
        pass

    quarantine = venv_dir.parent / f"{venv_dir.name}_stale_{int(time.time())}"
    try:
        if quarantine.exists():
            shutil.rmtree(quarantine, ignore_errors=True)
        venv_dir.rename(quarantine)
        shutil.rmtree(quarantine, ignore_errors=True)
        return
    except OSError as exc:
        raise RuntimeError(
            f"Unable to replace locked virtualenv at {venv_dir}. "
            "Close Python processes using it or choose a different --venv-dir."
        ) from exc


def bootstrap_environment(*, project_dir: Path, venv_dir: Path, python_cmd: str, fresh: bool) -> Path:
    if fresh and venv_dir.exists():
        _remove_venv_dir(venv_dir)

    if not _venv_pip_ok(venv_dir):
        _remove_venv_dir(venv_dir)
        _run([python_cmd, "-m", "venv", str(venv_dir)], cwd=project_dir)

    python_bin = _venv_python(venv_dir)
    _run([str(python_bin), "-m", "pip", "install", "--upgrade", "pip"], cwd=project_dir)
    _run([str(python_bin), "-m", "pip", "install", "-r", "requirements.txt"], cwd=project_dir)

    for module_name in REQUIRED_IMPORTS:
        _run([str(python_bin), "-c", f"import {module_name}"], cwd=project_dir)

    return python_bin


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap a reproducible project virtual environment.")
    parser.add_argument("--venv-dir", default="venv", help="Virtual environment directory (default: venv)")
    parser.add_argument("--python", default=sys.executable, help="Base Python executable to create the venv")
    parser.add_argument("--fresh", action="store_true", help="Recreate the virtual environment from scratch")
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parent
    venv_dir = (project_dir / args.venv_dir).resolve()

    try:
        python_bin = bootstrap_environment(
            project_dir=project_dir,
            venv_dir=venv_dir,
            python_cmd=args.python,
            fresh=args.fresh,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1

    print("Bootstrap complete.")
    print(f"Python: {python_bin}")
    if sys.platform.startswith("win"):
        print(f"Activate: {venv_dir}\\Scripts\\activate")
    else:
        print(f"Activate: source {venv_dir}/bin/activate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
