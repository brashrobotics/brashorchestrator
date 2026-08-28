"""Execution monitor: compare what was promised against what was observed.

An action declares effects. A backend reports an outcome. Those are two
different things, and conflating them is how a system ends up reporting that a
drill is on a shelf when it is on the floor.

The monitor therefore splits every effect into two categories:

*observed*
    a perception result confirmed it, so the fact is stored as verified

*claimed*
    the backend said the action succeeded but nothing observed the effect, so
    the fact is stored unverified

A task only reports success when its goal predicate is in the first category.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from brash_orchestrator_core.action_library import ActionSpec
from brash_orchestrator_core.backends.base import PrimitiveResult
from brash_orchestrator_core.task_graph import TaskNode
from brash_world_model.model import WorldModel, WorldStateDelta
from brash_world_model.predicates import (
    NEGATED_PREDICATES,
    Predicate,
    PredicateError,
    bind_predicate,
    parse_predicate,
)

#: Observation keys a backend may use to report confirmed predicates.
_VERIFIED_KEYS = ("verified_predicates", "verifiedPredicates", "observed_predicates")


@dataclass
class EffectReconciliation:
    """The outcome of comparing one node's declared effects to observation."""

    node_id: str
    verified: list[str] = field(default_factory=list)
    claimed: list[str] = field(default_factory=list)
    retracted: list[str] = field(default_factory=list)
    contradicted: list[str] = field(default_factory=list)
    confidence: float = 1.0

    @property
    def has_mismatch(self) -> bool:
        return bool(self.contradicted)

    @property
    def fully_verified(self) -> bool:
        return not self.claimed and not self.contradicted

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "verified": list(self.verified),
            "claimed": list(self.claimed),
            "retracted": list(self.retracted),
            "contradicted": list(self.contradicted),
            "confidence": round(self.confidence, 3),
        }


class ExecutionMonitor:
    def __init__(self, world: WorldModel, *, min_confidence: float = 0.6) -> None:
        self.world = world
        self.min_confidence = min_confidence
        self.reconciliations: list[EffectReconciliation] = []

    # ------------------------------------------------------------------
    def on_success(
        self, node: TaskNode, spec: ActionSpec, robot_id: str, result: PrimitiveResult
    ) -> EffectReconciliation:
        """Apply a successful node's effects, split by whether they were observed."""
        observations = dict(result.observed_effects)
        confidence = _confidence(observations)
        observed = _observed_predicates(observations)

        reconciliation = EffectReconciliation(node_id=node.id, confidence=confidence)
        verified_now: list[str] = []
        claimed_now: list[str] = []

        for template in spec.effects:
            try:
                effect = bind_predicate(
                    template, dict(node.parameters), assigned_robot=robot_id
                )
            except PredicateError:
                continue
            rendered = str(effect)
            if effect.name in NEGATED_PREDICATES:
                # Retractions need no positive observation; the absence of a
                # thing is established by the action that removed it.
                reconciliation.retracted.append(rendered)
                continue
            if _matches_observation(effect, observed) and confidence >= self.min_confidence:
                verified_now.append(rendered)
            else:
                claimed_now.append(rendered)

        # The verification predicate is the one that decides truth for this
        # action, so it is checked explicitly rather than relying on the effect
        # list happening to include it.
        if spec.verification:
            try:
                target = bind_predicate(
                    spec.verification, dict(node.parameters), assigned_robot=robot_id
                )
            except PredicateError:
                target = None
            if target is not None:
                rendered = str(target)
                if _matches_observation(target, observed) and confidence >= self.min_confidence:
                    if rendered not in verified_now:
                        verified_now.append(rendered)
                    if rendered in claimed_now:
                        claimed_now.remove(rendered)

        reconciliation.verified = verified_now
        reconciliation.claimed = claimed_now

        if verified_now:
            self.world.apply_delta(
                WorldStateDelta(
                    asserted=verified_now,
                    source="OBSERVATION",
                    confidence=confidence,
                    task_id=node.id,
                    node_id=node.id,
                )
            )
        if claimed_now or reconciliation.retracted:
            self.world.apply_delta(
                WorldStateDelta(
                    asserted=claimed_now + reconciliation.retracted,
                    source="BACKEND_CLAIM",
                    confidence=confidence,
                    task_id=node.id,
                    node_id=node.id,
                )
            )

        self._apply_extra_observations(node, observations)
        self.reconciliations.append(reconciliation)
        return reconciliation

    def on_failure(
        self, node: TaskNode, spec: ActionSpec, robot_id: str, result: PrimitiveResult
    ) -> EffectReconciliation:
        """Retract beliefs the failure contradicts.

        This is the step that makes closed-loop recovery work. When opening the
        box fails because the lid is not reachable, the belief that the lid was
        accessible was wrong, and leaving it in place would let the planner
        cheerfully retry the same impossible action.
        """
        observations = dict(result.observed_effects)
        reconciliation = EffectReconciliation(
            node_id=node.id, confidence=_confidence(observations)
        )

        contradicted = _contradicted_by(result.error_code, node, spec, robot_id)
        for rendered in contradicted:
            if self.world.fact(rendered) is not None:
                reconciliation.contradicted.append(rendered)

        if reconciliation.contradicted:
            self.world.apply_delta(
                WorldStateDelta(
                    retracted=list(reconciliation.contradicted),
                    source="OBSERVATION",
                    confidence=reconciliation.confidence,
                    task_id=node.id,
                    node_id=node.id,
                )
            )

        self._apply_extra_observations(node, observations)
        self.reconciliations.append(reconciliation)
        return reconciliation

    # ------------------------------------------------------------------
    def _apply_extra_observations(self, node: TaskNode, observations: Mapping[str, Any]) -> None:
        """Apply attribute updates a backend volunteered, such as shelf occupancy."""
        attributes = observations.get("attributes")
        if isinstance(attributes, Mapping):
            for path, value in attributes.items():
                try:
                    self.world.set_attribute(str(path), value)
                except Exception:  # noqa: BLE001 - a bad key must not stop the task
                    continue

        retract = observations.get("retracted_predicates")
        if isinstance(retract, (list, tuple)):
            self.world.apply_delta(
                WorldStateDelta(
                    retracted=[str(item) for item in retract],
                    source="OBSERVATION",
                    node_id=node.id,
                )
            )

    def goal_status(self, goal_predicate: str) -> tuple[bool, bool]:
        """Return ``(holds, verified)`` for the task's goal predicate."""
        try:
            goal = parse_predicate(goal_predicate)
        except PredicateError:
            return False, False
        return self.world.holds(goal), self.world.is_verified(goal)


def _confidence(observations: Mapping[str, Any]) -> float:
    raw = observations.get("confidence")
    if isinstance(raw, (int, float)):
        return max(0.0, min(1.0, float(raw)))
    # No stated confidence means the backend made a claim, not an observation.
    return 0.0


def _observed_predicates(observations: Mapping[str, Any]) -> set[Predicate]:
    collected: set[Predicate] = set()
    for key in _VERIFIED_KEYS:
        values = observations.get(key)
        if not isinstance(values, (list, tuple)):
            continue
        for item in values:
            try:
                collected.add(parse_predicate(str(item)))
            except PredicateError:
                continue
    return collected


def _matches_observation(effect: Predicate, observed: set[Predicate]) -> bool:
    return effect in observed


def _contradicted_by(
    error_code: str, node: TaskNode, spec: ActionSpec, robot_id: str
) -> list[str]:
    """Facts a specific failure disproves."""
    parameters = dict(node.parameters)
    container = parameters.get("container_id")
    target_object = parameters.get("object_id")
    shelf = parameters.get("shelf_id")

    contradicted: list[str] = []
    if error_code == "LID_NOT_ACCESSIBLE" and container:
        contradicted.append(f"lid_accessible({container})")
    elif error_code == "OBJECT_POSE_UNCERTAIN":
        if target_object:
            contradicted.append(f"pose_known({target_object})")
            contradicted.append(f"grasp_region_visible({target_object})")
        if container:
            contradicted.append(f"contents_visible({container})")
    elif error_code == "OBJECT_NOT_FOUND" and target_object:
        contradicted.append(f"pose_known({target_object})")
    elif error_code == "TARGET_OCCUPIED" and shelf:
        # Occupancy is an attribute, not a fact; the planner reads it through
        # shelf_has_free_space, so the attribute is what has to change.
        contradicted.append(f"inspected({shelf})")
    elif error_code in {"GRASP_FAILED", "SLIP_DETECTED"} and target_object:
        contradicted.append(f"held_by({target_object}, {robot_id})")
        if target_object:
            contradicted.append(f"pose_known({target_object})")
    return contradicted


def occupancy_update_for(error_code: str, node: TaskNode) -> dict[str, Any]:
    """Attribute changes implied by a failure, applied by the caller."""
    if error_code == "TARGET_OCCUPIED":
        shelf = node.parameters.get("shelf_id")
        if shelf:
            return {f"{shelf}.free_space": False}
    return {}
