"""Recovery planner: deterministic policy first, LLM as a ranker only.

Failure handling is where most of the value of an orchestrator lives, and also
where a language model is most tempting and most dangerous. The split here is
structural rather than a matter of prompt discipline:

1. A deterministic policy turns the reported error code into a closed list of
   candidate recovery options, already filtered against the world model, the
   task constraints, and the attempt history.
2. The LLM only chooses among those candidates by identifier. It cannot invent
   an action, cannot change parameters, and cannot escalate its own authority.
3. If the model picks something that is not on the list, or is unavailable, the
   configured policy order is used instead.

So the worst thing a bad ranking can do is pick a permitted option that turns
out to be the wrong one, which is a cost, not a hazard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from brash_orchestrator_core.action_library import ActionLibrary
from brash_orchestrator_core.backends.base import PrimitiveResult
from brash_orchestrator_core.events import ExecutionEvent
from brash_orchestrator_core.llm.provider import LlmError, LlmProvider, LlmRequest
from brash_orchestrator_core.llm.settings import LlmSettings
from brash_orchestrator_core.paths import config_path
from brash_orchestrator_core.schema_registry import SchemaRegistry
from brash_orchestrator_core.task_graph import TaskGraph, TaskNode
from brash_world_model.model import WorldModel

STRATEGIES = frozenset(
    {"retry", "reassign", "insert_before", "alternate_target", "escalate", "abort"}
)

#: ``$failed.<name>`` reads a parameter from the node that failed;
#: ``$observation.<key>|<default>`` reads a backend observation.
_FAILED_REFERENCE = re.compile(r"^\$failed\.(?P<name>[a-z_][a-z0-9_]*)$")
_OBSERVATION_REFERENCE = re.compile(
    r"^\$observation\.(?P<key>[a-z_][a-z0-9_]*)(?:\|(?P<default>.+))?$"
)


class RecoveryConfigError(RuntimeError):
    """Raised when the recovery policy file is malformed."""


@dataclass(frozen=True, slots=True)
class PolicyCandidate:
    candidate_id: str
    strategy: str
    rationale: str = ""
    action: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    parameter: str | None = None
    allowed_values: tuple[str, ...] = ()
    requires_constraint: str | None = None
    require_payload_released: bool = False


@dataclass
class RecoveryPolicies:
    max_attempts: int = 3
    max_recovery_actions_per_task: int = 6
    use_llm_ranking: bool = True
    policies: dict[str, tuple[PolicyCandidate, ...]] = field(default_factory=dict)

    @property
    def error_codes(self) -> tuple[str, ...]:
        return tuple(sorted(self.policies))

    def candidates_for(self, error_code: str) -> tuple[PolicyCandidate, ...]:
        return self.policies.get(error_code, ())

    @classmethod
    def load(cls, path: str | Path | None = None) -> RecoveryPolicies:
        resolved = Path(path) if path is not None else config_path("recovery_policies.yaml")
        document = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        if not isinstance(document, Mapping):
            raise RecoveryConfigError(f"{resolved} is not a mapping")

        defaults = document.get("defaults") or {}
        policies: dict[str, tuple[PolicyCandidate, ...]] = {}
        for error_code, raw in (document.get("policies") or {}).items():
            entries = (raw or {}).get("candidates") or []
            parsed: list[PolicyCandidate] = []
            for entry in entries:
                if not isinstance(entry, Mapping):
                    raise RecoveryConfigError(
                        f"policy {error_code!r} has a non-mapping candidate"
                    )
                strategy = str(entry.get("strategy", ""))
                if strategy not in STRATEGIES:
                    raise RecoveryConfigError(
                        f"policy {error_code!r} candidate "
                        f"{entry.get('candidate_id')!r} has unknown strategy {strategy!r}"
                    )
                if strategy == "insert_before" and not entry.get("action"):
                    raise RecoveryConfigError(
                        f"policy {error_code!r} candidate {entry.get('candidate_id')!r} "
                        "uses insert_before without naming an action"
                    )
                if strategy == "alternate_target" and not entry.get("parameter"):
                    raise RecoveryConfigError(
                        f"policy {error_code!r} candidate {entry.get('candidate_id')!r} "
                        "uses alternate_target without naming a parameter"
                    )
                parsed.append(
                    PolicyCandidate(
                        candidate_id=str(entry.get("candidate_id", f"{error_code}_{len(parsed)}")),
                        strategy=strategy,
                        rationale=str(entry.get("rationale", "")).strip(),
                        action=entry.get("action"),
                        parameters=dict(entry.get("parameters") or {}),
                        parameter=entry.get("parameter"),
                        allowed_values=tuple(entry.get("allowed_values") or ()),
                        requires_constraint=entry.get("requires_constraint"),
                        require_payload_released=bool(entry.get("require_payload_released", False)),
                    )
                )
            policies[str(error_code)] = tuple(parsed)

        return cls(
            max_attempts=int(defaults.get("max_attempts", 3)),
            max_recovery_actions_per_task=int(defaults.get("max_recovery_actions_per_task", 6)),
            use_llm_ranking=bool(defaults.get("use_llm_ranking", True)),
            policies=policies,
        )


@dataclass
class RecoveryOption:
    """A candidate the planner has confirmed is possible right now."""

    candidate_id: str
    strategy: str
    rationale: str
    node: TaskNode | None = None
    parameter: str | None = None
    value: Any = None
    excluded_robot_ids: tuple[str, ...] = ()

    def summary(self) -> str:
        if self.strategy == "insert_before" and self.node is not None:
            detail = f"insert {self.node.action}({_render(self.node.parameters)}) first"
        elif self.strategy == "alternate_target":
            detail = f"retarget {self.parameter} to {self.value}"
        else:
            detail = self.strategy
        return f"{self.candidate_id}: {detail}"

    def to_prompt_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "strategy": self.strategy,
            "rationale": self.rationale,
        }
        if self.node is not None:
            payload["inserted_action"] = self.node.action
            payload["inserted_parameters"] = dict(self.node.parameters)
        if self.parameter is not None:
            payload["parameter"] = self.parameter
            payload["new_value"] = self.value
        return payload


@dataclass
class RecoveryDecision:
    candidate_id: str
    strategy: str
    reason: str
    node: TaskNode | None = None
    parameter: str | None = None
    value: Any = None
    excluded_robot_ids: tuple[str, ...] = ()
    ranked_by: str = "policy"
    confidence: float = 1.0
    considered: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "strategy": self.strategy,
            "reason": self.reason,
            "ranked_by": self.ranked_by,
            "confidence": round(self.confidence, 3),
            "considered": list(self.considered),
        }
        if self.node is not None:
            payload["inserted_node"] = self.node.to_dict()
        if self.parameter is not None:
            payload["parameter"] = self.parameter
            payload["value"] = self.value
        return payload


class RecoveryPlanner:
    def __init__(
        self,
        *,
        policies: RecoveryPolicies,
        library: ActionLibrary,
        provider: LlmProvider | None = None,
        settings: LlmSettings | None = None,
        schemas: SchemaRegistry | None = None,
    ) -> None:
        self.policies = policies
        self.library = library
        self.provider = provider
        self.settings = settings
        self.schemas = schemas
        self._system_prompt: str | None = None

    # ------------------------------------------------------------------
    async def decide(
        self,
        *,
        node: TaskNode,
        spec,
        result: PrimitiveResult,
        graph: TaskGraph,
        world: WorldModel,
        attempt: int,
        history: Sequence[ExecutionEvent] = (),
        constraints: Mapping[str, Any] | None = None,
    ) -> RecoveryDecision | None:
        constraints = dict(constraints or {})
        options = self.build_options(
            node=node,
            result=result,
            graph=graph,
            world=world,
            history=history,
            constraints=constraints,
        )
        if not options:
            return None

        considered = [option.candidate_id for option in options]
        if len(options) == 1 or not self.policies.use_llm_ranking or self.provider is None:
            chosen = options[0]
            return _to_decision(
                chosen,
                reason=chosen.rationale or "first permitted policy option",
                ranked_by="policy",
                considered=considered,
            )

        ranked = await self._rank(
            node=node, result=result, options=options, history=history, world=world
        )
        if ranked is None:
            chosen = options[0]
            return _to_decision(
                chosen,
                reason=chosen.rationale or "policy order used; ranking unavailable",
                ranked_by="policy",
                considered=considered,
            )

        selected_id, confidence, reason = ranked
        for option in options:
            if option.candidate_id == selected_id:
                return _to_decision(
                    option,
                    reason=reason,
                    ranked_by="llm",
                    confidence=confidence,
                    considered=considered,
                )
        # The model named something outside the closed set, so the deterministic
        # order stands. This is the guardrail doing its job, not an error.
        chosen = options[0]
        return _to_decision(
            chosen,
            reason=(
                f"ranking returned unknown candidate {selected_id!r}, "
                "so the configured policy order was used"
            ),
            ranked_by="policy",
            considered=considered,
        )

    # ------------------------------------------------------------------
    def build_options(
        self,
        *,
        node: TaskNode,
        result: PrimitiveResult,
        graph: TaskGraph,
        world: WorldModel,
        history: Sequence[ExecutionEvent] = (),
        constraints: Mapping[str, Any] | None = None,
    ) -> list[RecoveryOption]:
        """Turn the matching policy into options that are possible right now."""
        constraints = dict(constraints or {})
        attempted = _already_attempted(history)
        options: list[RecoveryOption] = []
        next_id = graph.next_node_id()

        for candidate in self.policies.candidates_for(result.error_code):
            if candidate.candidate_id in attempted:
                # Repeating an option that already failed for this node wastes a
                # physical attempt.
                continue
            if candidate.requires_constraint and not constraints.get(candidate.requires_constraint):
                continue
            if candidate.require_payload_released and _holds_payload(history, world):
                continue

            if candidate.strategy == "insert_before":
                inserted = self._build_inserted_node(candidate, node, result, next_id)
                if inserted is None:
                    continue
                options.append(
                    RecoveryOption(
                        candidate_id=candidate.candidate_id,
                        strategy=candidate.strategy,
                        rationale=candidate.rationale,
                        node=inserted,
                    )
                )
                next_id = _increment(next_id)
                continue

            if candidate.strategy == "alternate_target":
                value = self._pick_alternate(candidate, node, world)
                if value is None:
                    continue
                options.append(
                    RecoveryOption(
                        candidate_id=candidate.candidate_id,
                        strategy=candidate.strategy,
                        rationale=candidate.rationale,
                        parameter=candidate.parameter,
                        value=value,
                    )
                )
                continue

            excluded: tuple[str, ...] = ()
            if candidate.strategy == "reassign":
                excluded = tuple(
                    event.robot_id
                    for event in history
                    if event.is_failure and event.robot_id
                )
            options.append(
                RecoveryOption(
                    candidate_id=candidate.candidate_id,
                    strategy=candidate.strategy,
                    rationale=candidate.rationale,
                    excluded_robot_ids=excluded,
                )
            )
        return options

    def _build_inserted_node(
        self,
        candidate: PolicyCandidate,
        node: TaskNode,
        result: PrimitiveResult,
        node_id: str,
    ) -> TaskNode | None:
        spec = self.library.get(str(candidate.action))
        if spec is None:
            # A policy naming an action the library does not define is a config
            # error, but it must not take down a running task.
            return None

        parameters: dict[str, Any] = {}
        for name, raw in candidate.parameters.items():
            value = _resolve_reference(raw, node, result)
            if value is None:
                return None
            parameters[name] = value
        for name in spec.parameters:
            if name in parameters:
                continue
            if name in node.parameters:
                parameters[name] = node.parameters[name]
        if set(parameters) != set(spec.parameters):
            return None

        return TaskNode(
            id=node_id,
            action=spec.name,
            parameters=parameters,
            required_capabilities=spec.required_capabilities,
            depends_on=(),
        )

    def _pick_alternate(
        self, candidate: PolicyCandidate, node: TaskNode, world: WorldModel
    ) -> str | None:
        parameter = str(candidate.parameter)
        current = node.parameters.get(parameter)
        for value in candidate.allowed_values:
            if value == current:
                continue
            location = world.locations.get(value)
            # Only offer a target that exists and is actually free; offering an
            # unknown location would just fail validation a moment later.
            if location is not None and location.free_space:
                return value
        return None

    # ------------------------------------------------------------------
    async def _rank(
        self,
        *,
        node: TaskNode,
        result: PrimitiveResult,
        options: Sequence[RecoveryOption],
        history: Sequence[ExecutionEvent],
        world: WorldModel,
    ) -> tuple[str, float, str] | None:
        if self.provider is None or self.settings is None:
            return None
        schemas = self.schemas or SchemaRegistry.load()
        if self._system_prompt is None:
            try:
                self._system_prompt = self.settings.prompt_text("recovery_system")
            except (LlmError, OSError):
                return None

        candidate_ids = [option.candidate_id for option in options]
        user = "\n\n".join(
            [
                "## FAILED NODE\n" + _as_json(
                    {
                        "node_id": node.id,
                        "action": node.action,
                        "parameters": dict(node.parameters),
                        "error_code": result.error_code,
                        "detail": result.detail,
                    }
                ),
                "## OBSERVATIONS\n" + _as_json(dict(result.observed_effects)),
                "## FAILURE HISTORY\n"
                + _as_json(
                    [
                        {
                            "event": event.event,
                            "error_code": event.error_code,
                            "robot_id": event.robot_id,
                            "attempt": event.attempt,
                            "detail": event.detail,
                        }
                        for event in history
                        if event.is_failure
                    ]
                ),
                "## WORLD STATE\n" + _as_json(world.summary()),
                "## CANDIDATES\n" + _as_json([option.to_prompt_dict() for option in options]),
                "Choose exactly one candidate_id from CANDIDATES.",
            ]
        )

        try:
            response = await self.provider.complete_json(
                LlmRequest(
                    system=self._system_prompt,
                    user=user,
                    schema=schemas.get("recovery_proposal"),
                    schema_name="recovery_proposal",
                    temperature=self.settings.temperature,
                    max_tokens=512,
                    context={"candidate_ids": candidate_ids, "node_id": node.id},
                )
            )
        except LlmError:
            # An unreachable model must never block physical recovery.
            return None

        if schemas.validation_errors("recovery_proposal", response.payload):
            return None
        payload = response.payload
        return (
            str(payload.get("selected_candidate_id", "")),
            float(payload.get("confidence", 0.5)),
            str(payload.get("reason", "")),
        )


def _to_decision(
    option: RecoveryOption,
    *,
    reason: str,
    ranked_by: str,
    considered: Sequence[str],
    confidence: float = 1.0,
) -> RecoveryDecision:
    return RecoveryDecision(
        candidate_id=option.candidate_id,
        strategy=option.strategy,
        reason=reason or option.rationale,
        node=option.node,
        parameter=option.parameter,
        value=option.value,
        excluded_robot_ids=option.excluded_robot_ids,
        ranked_by=ranked_by,
        confidence=confidence,
        considered=list(considered),
    )


def _resolve_reference(raw: Any, node: TaskNode, result: PrimitiveResult) -> Any:
    """Resolve ``$failed.x`` and ``$observation.x|default`` placeholders."""
    if not isinstance(raw, str):
        return raw

    failed = _FAILED_REFERENCE.match(raw)
    if failed:
        return node.parameters.get(failed.group("name"))

    observation = _OBSERVATION_REFERENCE.match(raw)
    if observation:
        value = result.observed_effects.get(observation.group("key"))
        if value is not None:
            return value
        default = observation.group("default")
        if default is None:
            return None
        try:
            return float(default) if "." in default else int(default)
        except ValueError:
            return default
    return raw


def _already_attempted(history: Sequence[ExecutionEvent]) -> set[str]:
    attempted: set[str] = set()
    for event in history:
        if event.event in {"RECOVERY_APPLIED", "RECOVERY_PROPOSED"}:
            candidate_id = event.observations.get("candidate_id")
            if candidate_id:
                attempted.add(str(candidate_id))
    return attempted


def _holds_payload(history: Sequence[ExecutionEvent], world: WorldModel) -> bool:
    """True when the robot that just failed still physically holds something.

    Sending the work to another robot while this one is still carrying the
    payload would strand the object, so those options are withheld until a
    release has been confirmed.
    """
    for event in reversed(list(history)):
        if event.is_failure and event.robot_id:
            robot = world.robots.get(event.robot_id)
            return bool(robot and robot.payload_object_ids)
    return False


def _increment(node_id: str) -> str:
    digits = "".join(character for character in node_id if character.isdigit())
    prefix = node_id[: len(node_id) - len(digits)] or "n"
    return f"{prefix}{int(digits or 0) + 1}"


def _render(parameters: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in parameters.items())


def _as_json(value: Any) -> str:
    import json

    return json.dumps(value, indent=2, default=str)
