from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import sys


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeLaunchSpec:
    runtime_kind: str
    runtime_name: str
    command: list[str]
    cwd: str | None
    execution_mode: str


def is_frozen_runtime() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_runtime_mode_label() -> str:
    return "frozen" if is_frozen_runtime() else "source"


def build_runtime_helper_launch(helper_name: str) -> RuntimeLaunchSpec:
    return _build_runtime_launch("--helper", helper_name, "helper")


def build_runtime_installer_launch(installer_name: str) -> RuntimeLaunchSpec:
    return _build_runtime_launch("--installer", installer_name, "installer")


def _build_runtime_launch(argument_name: str, runtime_name: str, runtime_kind: str) -> RuntimeLaunchSpec:
    if is_frozen_runtime():
        spec = RuntimeLaunchSpec(
            runtime_kind=runtime_kind,
            runtime_name=runtime_name,
            command=[sys.executable, argument_name, runtime_name],
            cwd=None,
            execution_mode="frozen-self-exe",
        )
    else:
        app_root = Path(__file__).resolve().parent.parent.parent
        spec = RuntimeLaunchSpec(
            runtime_kind=runtime_kind,
            runtime_name=runtime_name,
            command=[sys.executable, "-X", "utf8", "-u", "-m", "MainWindow", argument_name, runtime_name],
            cwd=str(app_root),
            execution_mode="source-module",
        )

    logger.info(
        "Prepared runtime %s launch | name=%s | execution_mode=%s | command=%s | cwd=%s",
        spec.runtime_kind,
        spec.runtime_name,
        spec.execution_mode,
        spec.command,
        spec.cwd or "<inherit>",
    )
    return spec
