"""Resolve the active LLM profile from config and environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from brash_orchestrator_core.llm.provider import LlmError, LlmProvider, build_provider
from brash_orchestrator_core.paths import config_path, repository_root

_ENV_OVERRIDES = {
    "base_url": "ORCH_LLM_BASE_URL",
    "model": "ORCH_LLM_MODEL",
    "api_key": "ORCH_LLM_API_KEY",
    "decoding": "ORCH_LLM_DECODING",
}


@dataclass
class LlmSettings:
    profile_name: str
    profile: dict[str, Any]
    prompts: dict[str, str] = field(default_factory=dict)
    max_examples: int = 2

    @property
    def max_repair_attempts(self) -> int:
        return int(self.profile.get("max_repair_attempts", 2))

    @property
    def temperature(self) -> float:
        return float(self.profile.get("temperature", 0.0))

    @property
    def top_p(self) -> float:
        return float(self.profile.get("top_p", 1.0))

    @property
    def max_tokens(self) -> int:
        return int(self.profile.get("max_tokens", 2048))

    @property
    def is_mock(self) -> bool:
        return self.profile.get("provider") == "mock"

    def build_provider(self) -> LlmProvider:
        return build_provider(self.profile)

    def prompt_text(self, key: str) -> str:
        relative = self.prompts.get(key)
        if not relative:
            raise LlmError(f"no prompt configured for {key!r}")
        return Path(relative).read_text(encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path | None = None, *, profile: str | None = None) -> LlmSettings:
        resolved = Path(path) if path is not None else config_path("llm.yaml")
        document = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        if not isinstance(document, Mapping):
            raise LlmError(f"{resolved} is not a mapping")

        profiles = document.get("profiles")
        if not isinstance(profiles, Mapping) or not profiles:
            raise LlmError(f"{resolved} defines no profiles")

        name = (
            profile
            or os.environ.get("ORCH_LLM_PROFILE")
            or str(document.get("active_profile", ""))
        )
        if name not in profiles:
            raise LlmError(
                f"LLM profile {name!r} is not defined in {resolved}. "
                f"Available profiles: {', '.join(sorted(profiles))}"
            )

        selected = dict(profiles[name] or {})
        for key, variable in _ENV_OVERRIDES.items():
            value = os.environ.get(variable)
            if value:
                selected[key] = value
        timeout = os.environ.get("ORCH_LLM_TIMEOUT_S")
        if timeout:
            selected["timeout_s"] = float(timeout)

        root = repository_root()
        if selected.get("examples_path"):
            selected["examples_path"] = str(root / str(selected["examples_path"]))

        raw_prompts = document.get("prompts") or {}
        prompts = {
            key: str(root / str(value))
            for key, value in raw_prompts.items()
            if isinstance(value, str)
        }
        return cls(
            profile_name=name,
            profile=selected,
            prompts=prompts,
            max_examples=int(raw_prompts.get("max_examples", 2)),
        )
