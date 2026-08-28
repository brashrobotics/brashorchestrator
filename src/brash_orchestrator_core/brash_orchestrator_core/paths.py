"""Locate repository resources from either a source tree or a ROS install.

Under ``colcon`` the config, schema, and prompt directories are installed into
the package share directory; during ROS-free development they sit at the
repository root. Resolving both keeps every other module free of that concern.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

_RESOURCE_DIRS = ("config", "schemas", "prompts")


@lru_cache(maxsize=1)
def repository_root() -> Path:
    """Return the directory containing config/, schemas/, and prompts/."""
    override = os.environ.get("ORCH_RESOURCE_ROOT")
    if override:
        return Path(override).resolve()

    candidates: list[Path] = []
    # Source checkout: .../src/brash_orchestrator_core/brash_orchestrator_core/paths.py
    candidates.extend(Path(__file__).resolve().parents[i] for i in range(2, 5))

    try:  # Installed ROS package.
        from ament_index_python.packages import get_package_share_directory

        candidates.append(Path(get_package_share_directory("brash_orchestrator_core")))
    except Exception:  # noqa: BLE001 - ament is absent in ROS-free environments.
        pass

    for candidate in candidates:
        if all((candidate / name).is_dir() for name in _RESOURCE_DIRS):
            return candidate
    raise FileNotFoundError(
        "could not locate config/, schemas/, and prompts/. "
        "Set ORCH_RESOURCE_ROOT to the BrashOrchestrator repository root."
    )


def resource_path(*parts: str) -> Path:
    """Resolve a path relative to the repository root."""
    return repository_root().joinpath(*parts)


def config_path(name: str) -> Path:
    return resource_path("config", name)


def schema_path(name: str) -> Path:
    return resource_path("schemas", name)


def prompt_path(name: str) -> Path:
    return resource_path("prompts", name)
