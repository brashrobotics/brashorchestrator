"""The Action Library: the closed vocabulary of executable primitives.

A capability describes what a robot can generally do. An action is the concrete,
parameterized, verifiable thing that gets executed. Anything absent from this
library cannot be planned, scheduled, or executed, which is what stops a
generated plan from inventing behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from brash_orchestrator_core.paths import config_path

#: Parameter types the validator understands. ``object_id`` and ``location_id``
#: are checked against the world model, so a hallucinated identifier fails.
PARAMETER_TYPES = frozenset({"object_id", "location_id", "number", "boolean", "string"})

EXECUTION_KINDS = frozenset({"transport", "manipulation", "perception"})


class ActionLibraryError(RuntimeError):
    """Raised when the action catalogue is malformed."""


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    """How an action reaches the fleet."""

    kind: str
    primitive: str
    fleet_capabilities: tuple[str, ...] = ()
    pickup_from: str | None = None
    dropoff_to: str | None = None
    payload_from: str | None = None
    station_from: str | None = None
    object_from: str | None = None
    container_from: str | None = None

    @property
    def is_transport(self) -> bool:
        return self.kind == "transport"


@dataclass(frozen=True, slots=True)
class ActionSpec:
    name: str
    required_capabilities: tuple[str, ...]
    parameters: Mapping[str, str]
    preconditions: tuple[str, ...]
    effects: tuple[str, ...]
    timeout_s: float
    execution: ExecutionSpec
    verification: str | None = None
    hazardous: bool = False

    def signature(self) -> str:
        rendered = ", ".join(f"{name}: {kind}" for name, kind in self.parameters.items())
        return f"{self.name}({rendered})"


@dataclass
class ActionLibrary:
    actions: dict[str, ActionSpec] = field(default_factory=dict)

    def __contains__(self, name: object) -> bool:
        return name in self.actions

    def __getitem__(self, name: str) -> ActionSpec:
        try:
            return self.actions[name]
        except KeyError as error:
            raise ActionLibraryError(f"unknown action {name!r}") from error

    def get(self, name: str) -> ActionSpec | None:
        return self.actions.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.actions))

    def capability_vocabulary(self) -> tuple[str, ...]:
        """Every capability name any action can ask for."""
        names = {
            capability
            for action in self.actions.values()
            for capability in action.required_capabilities
        }
        return tuple(sorted(names))

    def goal_predicate_vocabulary(self) -> tuple[str, ...]:
        """Predicates an action can establish, so a goal can be verified.

        A goal predicate outside this set is unreachable by construction, which
        is the cheapest possible way to reject an unverifiable goal.
        """
        from brash_world_model.predicates import parse_predicate

        names = set()
        for action in self.actions.values():
            for effect in action.effects:
                predicate = parse_predicate(effect)
                if not predicate.name.startswith("not_"):
                    names.add(predicate.name)
        return tuple(sorted(names))

    def prompt_view(self) -> list[dict[str, Any]]:
        """Compact action schemas for the interpreter prompt."""
        return [
            {
                "action": action.name,
                "parameters": dict(action.parameters),
                "required_capabilities": list(action.required_capabilities),
                "preconditions": list(action.preconditions),
                "effects": list(action.effects),
            }
            for action in (self.actions[name] for name in self.names)
        ]

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> ActionLibrary:
        raw_actions = document.get("actions")
        if not isinstance(raw_actions, Mapping) or not raw_actions:
            raise ActionLibraryError("action catalogue must define a non-empty 'actions' mapping")

        library = cls()
        for name, raw in raw_actions.items():
            if not isinstance(raw, Mapping):
                raise ActionLibraryError(f"action {name!r} must be a mapping")
            library.actions[name] = _parse_action(str(name), raw)
        return library

    @classmethod
    def load(cls, path: str | Path | None = None) -> ActionLibrary:
        resolved = Path(path) if path is not None else config_path("actions.yaml")
        document = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        if not isinstance(document, Mapping):
            raise ActionLibraryError(f"{resolved} is not a mapping")
        return cls.from_mapping(document)


def _parse_action(name: str, raw: Mapping[str, Any]) -> ActionSpec:
    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, Mapping):
        raise ActionLibraryError(f"action {name!r} has non-mapping parameters")
    for parameter_name, parameter_type in parameters.items():
        if parameter_type not in PARAMETER_TYPES:
            raise ActionLibraryError(
                f"action {name!r} parameter {parameter_name!r} has unsupported type "
                f"{parameter_type!r}; expected one of {sorted(PARAMETER_TYPES)}"
            )

    raw_execution = raw.get("execution")
    if not isinstance(raw_execution, Mapping):
        raise ActionLibraryError(f"action {name!r} must declare an 'execution' block")
    kind = str(raw_execution.get("kind", ""))
    if kind not in EXECUTION_KINDS:
        raise ActionLibraryError(
            f"action {name!r} has execution kind {kind!r}; expected one of {sorted(EXECUTION_KINDS)}"
        )
    if kind == "transport" and not (
        raw_execution.get("pickup_from") and raw_execution.get("dropoff_to")
    ):
        raise ActionLibraryError(
            f"transport action {name!r} must declare pickup_from and dropoff_to"
        )

    execution = ExecutionSpec(
        kind=kind,
        primitive=str(raw_execution.get("primitive", name)),
        fleet_capabilities=tuple(raw_execution.get("fleet_capabilities", ())),
        pickup_from=raw_execution.get("pickup_from"),
        dropoff_to=raw_execution.get("dropoff_to"),
        payload_from=raw_execution.get("payload_from"),
        station_from=raw_execution.get("station_from"),
        object_from=raw_execution.get("object_from"),
        container_from=raw_execution.get("container_from"),
    )

    for reference_field in (
        execution.pickup_from,
        execution.dropoff_to,
        execution.payload_from,
        execution.station_from,
        execution.object_from,
        execution.container_from,
    ):
        if reference_field is not None and reference_field not in parameters:
            raise ActionLibraryError(
                f"action {name!r} execution references unknown parameter {reference_field!r}"
            )

    timeout_s = float(raw.get("timeout_s", 0.0))
    if timeout_s <= 0:
        raise ActionLibraryError(f"action {name!r} must declare a positive timeout_s")

    return ActionSpec(
        name=name,
        required_capabilities=tuple(raw.get("required_capabilities", ())),
        parameters=dict(parameters),
        preconditions=tuple(raw.get("preconditions", ())),
        effects=tuple(raw.get("effects", ())),
        timeout_s=timeout_s,
        execution=execution,
        verification=raw.get("verification"),
        hazardous=bool(raw.get("hazardous", False)),
    )
