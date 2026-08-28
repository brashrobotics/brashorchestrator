"""Capability registry: what each robot can measurably do.

Capabilities are typed and quantified rather than free text so the scheduler can
compare bodies without any vendor-specific branch. Adding a robot means adding a
manifest entry, never editing the planner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from brash_orchestrator_core.paths import config_path

#: Keys that carry a typed limit rather than a free-form qualifier.
_LIMIT_KEYS = frozenset(
    {
        "enabled",
        "success_prior",
        "max_mass_kg",
        "min_height_m",
        "max_height_m",
        "pose_tolerance_m",
        "yaw_tolerance_deg",
    }
)


class CapabilityRegistryError(RuntimeError):
    """Raised when a robot manifest is malformed."""


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    enabled: bool = True
    success_prior: float = 0.8
    max_mass_kg: float | None = None
    min_height_m: float | None = None
    max_height_m: float | None = None
    pose_tolerance_m: float | None = None
    yaw_tolerance_deg: float | None = None
    qualifiers: Mapping[str, Any] = field(default_factory=dict)

    def supports_value(self, qualifier: str, value: str) -> bool:
        """Check a value against a qualifier list, ignoring absent qualifiers."""
        allowed = self.qualifiers.get(qualifier)
        if allowed is None:
            return True
        if isinstance(allowed, Mapping):
            return value in allowed
        return value in allowed


@dataclass(frozen=True, slots=True)
class RobotProfile:
    robot_id: str
    type: str
    vendor: str
    fleet_robot_id: str
    capabilities: Mapping[str, Capability]
    frame_id: str = ""
    status_topic: str = ""
    embodied_namespace: str = ""
    energy_per_second_wh: float = 0.25

    def capability(self, name: str) -> Capability | None:
        found = self.capabilities.get(name)
        return found if found is not None and found.enabled else None

    def has_all(self, names: Iterable[str]) -> bool:
        return all(self.capability(name) is not None for name in names)

    def missing_capabilities(self, names: Iterable[str]) -> list[str]:
        return [name for name in names if self.capability(name) is None]

    def payload_limit_kg(self, names: Iterable[str]) -> float | None:
        """Tightest mass limit across the requested capabilities."""
        limits = [
            capability.max_mass_kg
            for name in names
            if (capability := self.capability(name)) is not None
            and capability.max_mass_kg is not None
        ]
        return min(limits) if limits else None

    def reach_range_m(self, names: Iterable[str]) -> tuple[float | None, float | None]:
        minimums = [
            capability.min_height_m
            for name in names
            if (capability := self.capability(name)) is not None
            and capability.min_height_m is not None
        ]
        maximums = [
            capability.max_height_m
            for name in names
            if (capability := self.capability(name)) is not None
            and capability.max_height_m is not None
        ]
        return (max(minimums) if minimums else None, min(maximums) if maximums else None)

    def combined_success_prior(self, names: Iterable[str]) -> float:
        """Joint prior across the capabilities an action needs.

        Treating them as independent is a deliberate simplification: it is
        pessimistic, which is the safe direction, and it is replaced by a learned
        context-dependent model once real trial data exists.
        """
        probability = 1.0
        for name in names:
            capability = self.capability(name)
            if capability is None:
                return 0.0
            probability *= capability.success_prior
        return probability


@dataclass
class CapabilityRegistry:
    robots: dict[str, RobotProfile] = field(default_factory=dict)

    def __contains__(self, robot_id: object) -> bool:
        return robot_id in self.robots

    def __getitem__(self, robot_id: str) -> RobotProfile:
        try:
            return self.robots[robot_id]
        except KeyError as error:
            raise CapabilityRegistryError(f"unknown robot {robot_id!r}") from error

    def get(self, robot_id: str) -> RobotProfile | None:
        return self.robots.get(robot_id)

    @property
    def robot_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.robots))

    def capability_names(self) -> tuple[str, ...]:
        names = {
            name
            for profile in self.robots.values()
            for name, capability in profile.capabilities.items()
            if capability.enabled
        }
        return tuple(sorted(names))

    def robots_with(self, capabilities: Iterable[str]) -> list[RobotProfile]:
        wanted = list(capabilities)
        return [
            self.robots[robot_id]
            for robot_id in self.robot_ids
            if self.robots[robot_id].has_all(wanted)
        ]

    def register(self, profile: RobotProfile) -> None:
        self.robots[profile.robot_id] = profile

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> CapabilityRegistry:
        raw_robots = document.get("robots")
        if not isinstance(raw_robots, Mapping) or not raw_robots:
            raise CapabilityRegistryError("robot manifest must define a non-empty 'robots' mapping")
        registry = cls()
        for robot_id, raw in raw_robots.items():
            if not isinstance(raw, Mapping):
                raise CapabilityRegistryError(f"robot {robot_id!r} must be a mapping")
            registry.register(_parse_robot(str(robot_id), raw))
        return registry

    @classmethod
    def load(cls, path: str | Path | None = None) -> CapabilityRegistry:
        resolved = Path(path) if path is not None else config_path("robots.yaml")
        document = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        if not isinstance(document, Mapping):
            raise CapabilityRegistryError(f"{resolved} is not a mapping")
        return cls.from_mapping(document)


def _parse_capability(name: str, raw: Mapping[str, Any] | bool) -> Capability:
    if isinstance(raw, bool):
        return Capability(name=name, enabled=raw)
    qualifiers = {key: value for key, value in raw.items() if key not in _LIMIT_KEYS}
    return Capability(
        name=name,
        enabled=bool(raw.get("enabled", True)),
        success_prior=float(raw.get("success_prior", 0.8)),
        max_mass_kg=_optional_float(raw.get("max_mass_kg")),
        min_height_m=_optional_float(raw.get("min_height_m")),
        max_height_m=_optional_float(raw.get("max_height_m")),
        pose_tolerance_m=_optional_float(raw.get("pose_tolerance_m")),
        yaw_tolerance_deg=_optional_float(raw.get("yaw_tolerance_deg")),
        qualifiers=qualifiers,
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def parse_robot_manifest(robot_id: str, raw: Mapping[str, Any]) -> RobotProfile:
    """Parse a single robot manifest, as used by runtime registration."""
    return _parse_robot(robot_id, raw)


def _parse_robot(robot_id: str, raw: Mapping[str, Any]) -> RobotProfile:
    raw_capabilities = raw.get("capabilities") or {}
    if not isinstance(raw_capabilities, Mapping):
        raise CapabilityRegistryError(f"robot {robot_id!r} has non-mapping capabilities")
    capabilities = {
        str(name): _parse_capability(str(name), value)
        for name, value in raw_capabilities.items()
    }
    return RobotProfile(
        robot_id=robot_id,
        type=str(raw.get("type", "robot")),
        vendor=str(raw.get("vendor", "unknown")),
        fleet_robot_id=str(raw.get("fleet_robot_id", robot_id)),
        capabilities=capabilities,
        frame_id=str(raw.get("frame_id", "")),
        status_topic=str(raw.get("status_topic", "")),
        embodied_namespace=str(raw.get("embodied_namespace", "")),
        energy_per_second_wh=float(raw.get("energy_per_second_wh", 0.25)),
    )
