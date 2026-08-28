"""Capability-aware scheduler.

The output is a deterministic execution plan, never model text. For each node it
picks the cheapest eligible body using

    J(robot, action) = w_time   * duration
                     + w_energy * energy
                     + w_risk   * risk
                     + w_move   * reposition
                     - w_succ   * predicted success

and records why every candidate won or lost, because "the fleet sent a different
robot than I expected" is a question an operator will ask.

The winner is a *preference*, not a binding. Fleet Control performs the final
allocation and may substitute another eligible robot if state changed between
planning and dispatch. Competing with that would only produce a plan that gets
overridden; expressing it as a preference means the factory order still moves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from brash_orchestrator_core.action_library import ActionLibrary, ActionSpec
from brash_orchestrator_core.capability_registry import CapabilityRegistry, RobotProfile
from brash_orchestrator_core.paths import config_path
from brash_orchestrator_core.task_graph import TaskGraph, TaskNode
from brash_world_model.model import PredicateContext, WorldModel

_RISK_ORDER = ("low", "medium", "high")

#: Availability values that make a robot unusable right now.
_BLOCKING_AVAILABILITY = frozenset({"busy", "charging", "quarantined", "faulted", "offline"})


class SchedulerError(RuntimeError):
    """Raised when the scheduler configuration is unusable."""


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    w_time: float = 1.0
    w_energy: float = 0.4
    w_risk: float = 1.6
    w_move: float = 0.8
    w_succ: float = 2.5
    duration_normalizer_s: float = 300.0
    energy_normalizer_wh: float = 60.0
    distance_normalizer_m: float = 50.0
    default_duration_s: float = 90.0
    duration_estimates: Mapping[str, float] = field(default_factory=dict)
    travel_speed_mps: float = 0.9
    default_risk: float = 0.2
    risk_estimates: Mapping[str, float] = field(default_factory=dict)
    min_battery_percent: float = 25.0
    risk_ceilings: Mapping[str, float] = field(default_factory=dict)
    min_success_probability: float = 0.35

    @classmethod
    def load(cls, path: str | Path | None = None) -> SchedulerConfig:
        resolved = Path(path) if path is not None else config_path("scheduler.yaml")
        document = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        if not isinstance(document, Mapping):
            raise SchedulerError(f"{resolved} is not a mapping")
        weights = document.get("weights") or {}
        normalizers = document.get("normalizers") or {}
        if float(normalizers.get("duration_normalizer_s", 300.0)) <= 0:
            raise SchedulerError("duration_normalizer_s must be positive")
        return cls(
            w_time=float(weights.get("w_time", 1.0)),
            w_energy=float(weights.get("w_energy", 0.4)),
            w_risk=float(weights.get("w_risk", 1.6)),
            w_move=float(weights.get("w_move", 0.8)),
            w_succ=float(weights.get("w_succ", 2.5)),
            duration_normalizer_s=float(normalizers.get("duration_normalizer_s", 300.0)),
            energy_normalizer_wh=float(normalizers.get("energy_normalizer_wh", 60.0)),
            distance_normalizer_m=float(normalizers.get("distance_normalizer_m", 50.0)),
            default_duration_s=float(document.get("default_duration_s", 90.0)),
            duration_estimates=dict(document.get("duration_estimates") or {}),
            travel_speed_mps=float(document.get("travel_speed_mps", 0.9)),
            default_risk=float(document.get("default_risk", 0.2)),
            risk_estimates=dict(document.get("risk_estimates") or {}),
            min_battery_percent=float(document.get("min_battery_percent", 25.0)),
            risk_ceilings=dict(document.get("risk_ceilings") or {}),
            min_success_probability=float(document.get("min_success_probability", 0.35)),
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    robot_id: str
    eligible: bool
    cost: float | None
    success_probability: float
    estimated_duration_s: float
    estimated_energy_wh: float
    risk: float
    reposition_distance_m: float
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "eligible": self.eligible,
            "cost": None if self.cost is None else round(self.cost, 4),
            "success_probability": round(self.success_probability, 4),
            "estimated_duration_s": round(self.estimated_duration_s, 1),
            "estimated_energy_wh": round(self.estimated_energy_wh, 2),
            "risk": round(self.risk, 3),
            "reposition_distance_m": round(self.reposition_distance_m, 2),
            "reasons": list(self.reasons),
        }


@dataclass
class Assignment:
    node_id: str
    action: str
    assigned_robot_id: str | None
    candidates: list[Candidate] = field(default_factory=list)
    rationale: str = ""

    @property
    def feasible(self) -> bool:
        return self.assigned_robot_id is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "action": self.action,
            "assigned_robot_id": self.assigned_robot_id,
            "rationale": self.rationale,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass
class ExecutionPlan:
    task_id: str
    goal_predicate: str
    assignments: dict[str, Assignment] = field(default_factory=dict)
    layers: list[list[str]] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return bool(self.assignments) and all(
            assignment.feasible for assignment in self.assignments.values()
        )

    @property
    def infeasible_nodes(self) -> list[str]:
        return [
            node_id
            for node_id, assignment in self.assignments.items()
            if not assignment.feasible
        ]

    def robot_for(self, node_id: str) -> str | None:
        assignment = self.assignments.get(node_id)
        return assignment.assigned_robot_id if assignment else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal_predicate": self.goal_predicate,
            "feasible": self.feasible,
            "layers": [list(layer) for layer in self.layers],
            "assignments": {
                node_id: assignment.to_dict()
                for node_id, assignment in self.assignments.items()
            },
        }


class Scheduler:
    def __init__(
        self,
        library: ActionLibrary,
        registry: CapabilityRegistry,
        config: SchedulerConfig | None = None,
    ) -> None:
        self.library = library
        self.registry = registry
        self.config = config or SchedulerConfig()

    def plan(
        self,
        graph: TaskGraph,
        world: WorldModel,
        *,
        constraints: Mapping[str, Any] | None = None,
    ) -> ExecutionPlan:
        """Assign every node, projecting robot positions as work proceeds."""
        constraints = dict(constraints or {})
        allowed = set(constraints.get("allowed_robot_ids") or self.registry.robot_ids)
        risk_ceiling = self._risk_ceiling(constraints)

        plan = ExecutionPlan(
            task_id=graph.task_id,
            goal_predicate=graph.goal_predicate,
            layers=graph.execution_layers(),
        )

        # Projected robot locations. Assigning a node moves its robot, so the
        # next node's reposition cost reflects where the fleet will actually be.
        projected_locations = {
            robot_id: world.robots[robot_id].location for robot_id in world.robots
        }

        for node_id in graph.topological_order():
            node = graph.node(node_id)
            spec = self.library.get(node.action)
            if spec is None:
                plan.assignments[node_id] = Assignment(
                    node_id=node_id,
                    action=node.action,
                    assigned_robot_id=None,
                    rationale=f"action {node.action!r} is not in the Action Library",
                )
                continue

            assignment = self._assign_node(
                node=node,
                spec=spec,
                world=world,
                allowed=allowed,
                risk_ceiling=risk_ceiling,
                projected_locations=projected_locations,
            )
            plan.assignments[node_id] = assignment
            if assignment.assigned_robot_id:
                destination = self._node_destination(spec, node, world)
                if destination:
                    projected_locations[assignment.assigned_robot_id] = destination

        return plan

    # ------------------------------------------------------------------
    def _assign_node(
        self,
        *,
        node: TaskNode,
        spec: ActionSpec,
        world: WorldModel,
        allowed: set[str],
        risk_ceiling: float,
        projected_locations: Mapping[str, str | None],
    ) -> Assignment:
        candidates: list[Candidate] = []
        for robot_id in self.registry.robot_ids:
            profile = self.registry[robot_id]
            candidates.append(
                self._score(
                    profile=profile,
                    node=node,
                    spec=spec,
                    world=world,
                    allowed=allowed,
                    risk_ceiling=risk_ceiling,
                    origin=projected_locations.get(robot_id),
                )
            )

        eligible = [candidate for candidate in candidates if candidate.eligible]
        # Ties break on robot_id so the same inputs always yield the same plan.
        eligible.sort(key=lambda candidate: (candidate.cost, candidate.robot_id))
        candidates.sort(
            key=lambda candidate: (
                not candidate.eligible,
                candidate.cost if candidate.cost is not None else float("inf"),
                candidate.robot_id,
            )
        )

        if not eligible:
            blocking = "; ".join(
                f"{candidate.robot_id}: {', '.join(candidate.reasons)}"
                for candidate in candidates
                if candidate.reasons
            )
            return Assignment(
                node_id=node.id,
                action=node.action,
                assigned_robot_id=None,
                candidates=candidates,
                rationale=f"no eligible robot. {blocking}" if blocking else "no eligible robot",
            )

        winner = eligible[0]
        rationale = (
            f"lowest cost {winner.cost:.3f} with predicted success "
            f"{winner.success_probability:.2f}"
        )
        if node.proposed_robot_id and node.proposed_robot_id != winner.robot_id:
            rationale += (
                f"; the interpreter proposed {node.proposed_robot_id} and the "
                "scheduler overrode it"
            )
        if len(eligible) > 1:
            runner_up = eligible[1]
            rationale += (
                f"; next best {runner_up.robot_id} at {runner_up.cost:.3f}"
            )
        return Assignment(
            node_id=node.id,
            action=node.action,
            assigned_robot_id=winner.robot_id,
            candidates=candidates,
            rationale=rationale,
        )

    def _score(
        self,
        *,
        profile: RobotProfile,
        node: TaskNode,
        spec: ActionSpec,
        world: WorldModel,
        allowed: set[str],
        risk_ceiling: float,
        origin: str | None,
    ) -> Candidate:
        config = self.config
        reasons: list[str] = []

        if profile.robot_id not in allowed:
            reasons.append("excluded by allowed_robot_ids")

        missing = profile.missing_capabilities(spec.required_capabilities)
        if missing:
            reasons.append(f"missing capability {', '.join(missing)}")

        record = world.robots.get(profile.robot_id)
        if record is not None:
            if record.availability.lower() in _BLOCKING_AVAILABILITY:
                reasons.append(f"availability is {record.availability}")
            if record.connection.lower() != "online":
                reasons.append(f"connection is {record.connection}")
            if record.battery_percent < config.min_battery_percent:
                reasons.append(
                    f"battery {record.battery_percent:.0f}% below minimum "
                    f"{config.min_battery_percent:.0f}%"
                )
            if record.fault_code:
                reasons.append(f"reported fault {record.fault_code}")

        success = profile.combined_success_prior(spec.required_capabilities) if not missing else 0.0
        risk = float(config.risk_estimates.get(spec.name, config.default_risk))
        duration = float(config.duration_estimates.get(spec.name, config.default_duration_s))

        target = self._node_station(spec, node, world)
        distance = world.distance_between(origin, target) if target else 0.0
        travel_duration = distance / max(config.travel_speed_mps, 0.1)
        total_duration = duration + travel_duration
        energy = total_duration * profile.energy_per_second_wh

        # Payload and reach are hard physical limits, not costs.
        limit = profile.payload_limit_kg(spec.required_capabilities)
        payload_mass = self._payload_mass(spec, node, world)
        if limit is not None and payload_mass > limit:
            reasons.append(
                f"payload {payload_mass:.1f}kg exceeds limit {limit:.1f}kg"
            )

        minimum, maximum = profile.reach_range_m(spec.required_capabilities)
        shelf_height = self._target_height(spec, node, world)
        if shelf_height is not None:
            if minimum is not None and shelf_height < minimum:
                reasons.append(f"target height {shelf_height:.2f}m below reach {minimum:.2f}m")
            if maximum is not None and shelf_height > maximum:
                reasons.append(f"target height {shelf_height:.2f}m above reach {maximum:.2f}m")

        context = PredicateContext(assigned_robot=profile.robot_id)
        if target and not world.holds(f"route_available({target})", context):
            reasons.append(f"no route to {target}")

        if risk > risk_ceiling:
            reasons.append(f"risk {risk:.2f} exceeds ceiling {risk_ceiling:.2f}")
        if not missing and success < config.min_success_probability:
            reasons.append(
                f"predicted success {success:.2f} below minimum "
                f"{config.min_success_probability:.2f}"
            )

        eligible = not reasons
        cost = (
            config.w_time * (total_duration / config.duration_normalizer_s)
            + config.w_energy * (energy / config.energy_normalizer_wh)
            + config.w_risk * risk
            + config.w_move * (distance / config.distance_normalizer_m)
            - config.w_succ * success
        ) if eligible else None

        return Candidate(
            robot_id=profile.robot_id,
            eligible=eligible,
            cost=cost,
            success_probability=success,
            estimated_duration_s=total_duration,
            estimated_energy_wh=energy,
            risk=risk,
            reposition_distance_m=distance,
            reasons=tuple(reasons),
        )

    # ------------------------------------------------------------------
    def _risk_ceiling(self, constraints: Mapping[str, Any]) -> float:
        requested = str(constraints.get("max_risk", "high"))
        if requested not in _RISK_ORDER:
            requested = "high"
        return float(self.config.risk_ceilings.get(requested, 1.0))

    def _node_station(self, spec: ActionSpec, node: TaskNode, world: WorldModel) -> str | None:
        """Where the robot has to be for this node to make sense."""
        execution = spec.execution
        reference = execution.pickup_from or execution.station_from
        if reference is None:
            return None
        value = node.parameters.get(reference)
        return world.resolve_location(str(value)) if value is not None else None

    def _node_destination(self, spec: ActionSpec, node: TaskNode, world: WorldModel) -> str | None:
        """Where the robot ends up, used to project the next reposition cost."""
        execution = spec.execution
        reference = execution.dropoff_to or execution.station_from
        if reference is None:
            return None
        value = node.parameters.get(reference)
        return world.resolve_location(str(value)) if value is not None else None

    def _payload_mass(self, spec: ActionSpec, node: TaskNode, world: WorldModel) -> float:
        reference = spec.execution.payload_from or spec.execution.object_from
        if reference is None:
            return 0.0
        value = node.parameters.get(reference)
        return world.object_mass(str(value)) if value is not None else 0.0

    def _target_height(self, spec: ActionSpec, node: TaskNode, world: WorldModel) -> float | None:
        reference = spec.execution.station_from or spec.execution.dropoff_to
        if reference is None:
            return None
        value = node.parameters.get(reference)
        location = world.locations.get(str(value)) if value is not None else None
        return location.height_m if location is not None else None


def assignment_table(plan: ExecutionPlan) -> list[tuple[str, str, str, str]]:
    """Rows of (node, action, robot, rationale) for operator display."""
    return [
        (
            node_id,
            plan.assignments[node_id].action,
            plan.assignments[node_id].assigned_robot_id or "-",
            plan.assignments[node_id].rationale,
        )
        for node_id in plan.assignments
    ]


def describe_plan(plan: ExecutionPlan, layers: Sequence[Sequence[str]] | None = None) -> str:
    """Human-readable plan summary used by the CLI and dry runs."""
    lines = [f"task {plan.task_id} -> {plan.goal_predicate}"]
    for index, layer in enumerate(layers or plan.layers, start=1):
        parallel = " (parallel)" if len(layer) > 1 else ""
        lines.append(f"  wave {index}{parallel}:")
        for node_id in layer:
            assignment = plan.assignments.get(node_id)
            if assignment is None:
                lines.append(f"    {node_id}: unassigned")
                continue
            lines.append(
                f"    {node_id} {assignment.action} -> "
                f"{assignment.assigned_robot_id or 'NO ELIGIBLE ROBOT'}"
            )
            lines.append(f"        {assignment.rationale}")
    return "\n".join(lines)
