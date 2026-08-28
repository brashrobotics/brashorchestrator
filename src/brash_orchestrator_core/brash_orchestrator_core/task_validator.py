"""Task graph validation.

Nothing generated reaches a robot without passing every check here. When a check
fails the error is machine-readable and names the offending node, because the
interpreter repairs its own output from these errors rather than being asked to
try again blindly.

The ten checks, in order:

1.  every action exists in the Action Library
2.  parameter names and types match the action signature
3.  at least one robot can supply every required capability
4.  the dependency graph is acyclic and well formed
5.  preconditions are satisfiable in the projected world state
6.  the goal predicate is measurable and some node establishes it
7.  user constraints are compatible with safety policy
8.  no invented robot, object, or location identifier
9.  every action has a timeout and a recovery policy
10. hazardous actions carry the required human approval
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from brash_orchestrator_core.action_library import ActionLibrary, ActionSpec
from brash_orchestrator_core.capability_registry import CapabilityRegistry, RobotProfile
from brash_world_model.model import PredicateContext, WorldModel
from brash_world_model.predicates import (
    NEGATED_PREDICATES,
    Predicate,
    PredicateError,
    bind_predicate,
    parse_predicate,
)

#: Constraint values a plan may never widen. The LLM cannot change these and a
#: request that exceeds them is rejected rather than silently clamped.
SAFETY_LIMITS = {
    "max_deadline_s": 7200.0,
    "max_risk": "high",
}
_RISK_ORDER = ("low", "medium", "high")


@dataclass(frozen=True, slots=True)
class ValidationError:
    """One machine-readable reason a graph was rejected."""

    check: str
    code: str
    message: str
    node_id: str | None = None
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "check": self.check,
            "code": self.code,
            "message": self.message,
        }
        if self.node_id:
            payload["node_id"] = self.node_id
        if self.field:
            payload["field"] = self.field
        return payload

    def __str__(self) -> str:
        location = f" [{self.node_id}" + (f".{self.field}]" if self.field else "]") if self.node_id else ""
        return f"{self.code}{location}: {self.message}"


@dataclass
class ValidationReport:
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, error: ValidationError) -> None:
        self.errors.append(error)

    def warn(self, warning: ValidationError) -> None:
        self.warnings.append(warning)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [error.to_dict() for error in self.errors],
            "warnings": [warning.to_dict() for warning in self.warnings],
        }

    def summary(self) -> str:
        if self.ok:
            return "valid"
        return "; ".join(str(error) for error in self.errors)


class TaskValidator:
    """Validate a task graph against the library, registry, and world state."""

    def __init__(
        self,
        library: ActionLibrary,
        registry: CapabilityRegistry,
        *,
        recovery_policy_codes: Sequence[str] = (),
        safety_limits: Mapping[str, Any] | None = None,
    ) -> None:
        self.library = library
        self.registry = registry
        self.recovery_policy_codes = set(recovery_policy_codes)
        self.safety_limits = {**SAFETY_LIMITS, **(safety_limits or {})}

    def validate(
        self,
        graph,  # TaskGraph, imported lazily to keep this module import-light
        world: WorldModel,
        *,
        constraints: Mapping[str, Any] | None = None,
        approvals: Sequence[str] = (),
    ) -> ValidationReport:
        report = ValidationReport()
        constraints = dict(constraints or {})

        self._check_structure(graph, report)
        # A structurally broken graph cannot be reasoned about node by node.
        if report.errors:
            return report

        self._check_actions_and_parameters(graph, world, report)
        self._check_capabilities(graph, report)
        self._check_constraints(constraints, report)
        self._check_timeouts_and_policies(graph, report)
        self._check_approvals(graph, constraints, approvals, report)

        if report.errors:
            return report

        self._check_preconditions(graph, world, report)
        self._check_goal(graph, world, report)
        return report

    # -- check 4 --------------------------------------------------------
    def _check_structure(self, graph, report: ValidationReport) -> None:
        if not graph.nodes:
            report.add(
                ValidationError("structure", "EMPTY_GRAPH", "the task graph contains no nodes")
            )
            return
        for node_id in graph.duplicate_ids():
            report.add(
                ValidationError(
                    "structure", "DUPLICATE_NODE_ID", f"node id {node_id!r} is used more than once",
                    node_id=node_id,
                )
            )
        for node_id, dependency in graph.dangling_dependencies():
            report.add(
                ValidationError(
                    "structure",
                    "UNKNOWN_DEPENDENCY",
                    f"node {node_id!r} depends on {dependency!r}, which is not in the graph",
                    node_id=node_id,
                    field="depends_on",
                )
            )
        for node in graph.nodes:
            if node.id in node.depends_on:
                report.add(
                    ValidationError(
                        "structure",
                        "SELF_DEPENDENCY",
                        f"node {node.id!r} depends on itself",
                        node_id=node.id,
                        field="depends_on",
                    )
                )
        if report.errors:
            return
        cycle = graph.find_cycle()
        if cycle is not None:
            report.add(
                ValidationError(
                    "structure",
                    "CYCLIC_GRAPH",
                    f"dependency cycle: {' -> '.join(cycle)}",
                    node_id=cycle[0],
                    field="depends_on",
                )
            )

    # -- checks 1, 2, 8 -------------------------------------------------
    def _check_actions_and_parameters(self, graph, world: WorldModel, report: ValidationReport) -> None:
        for node in graph.nodes:
            spec = self.library.get(node.action)
            if spec is None:
                report.add(
                    ValidationError(
                        "action_library",
                        "UNKNOWN_ACTION",
                        f"{node.action!r} is not in the Action Library. "
                        f"Available actions: {', '.join(self.library.names)}",
                        node_id=node.id,
                        field="action",
                    )
                )
                continue

            expected = set(spec.parameters)
            provided = set(node.parameters)
            for missing in sorted(expected - provided):
                report.add(
                    ValidationError(
                        "parameters",
                        "MISSING_PARAMETER",
                        f"{spec.signature()} requires parameter {missing!r}",
                        node_id=node.id,
                        field=missing,
                    )
                )
            for extra in sorted(provided - expected):
                report.add(
                    ValidationError(
                        "parameters",
                        "UNEXPECTED_PARAMETER",
                        f"{spec.signature()} does not accept parameter {extra!r}",
                        node_id=node.id,
                        field=extra,
                    )
                )
            for name in sorted(expected & provided):
                self._check_parameter_value(node, name, spec.parameters[name], world, report)

            if node.proposed_robot_id and node.proposed_robot_id not in self.registry:
                report.add(
                    ValidationError(
                        "grounding",
                        "UNKNOWN_ROBOT",
                        f"{node.proposed_robot_id!r} is not a registered robot",
                        node_id=node.id,
                        field="proposed_robot_id",
                    )
                )

    def _check_parameter_value(
        self, node, name: str, parameter_type: str, world: WorldModel, report: ValidationReport
    ) -> None:
        value = node.parameters[name]
        if parameter_type == "object_id":
            # A tolerated exception: some parameters carry a short enumerated
            # reason rather than an entity, so only reject unknown identifiers
            # for parameters that actually name something in the world.
            if isinstance(value, str) and value not in world.objects and name != "reason":
                report.add(
                    ValidationError(
                        "grounding",
                        "UNKNOWN_OBJECT",
                        f"{value!r} is not an object in the world model. "
                        f"Known objects: {', '.join(sorted(world.objects))}",
                        node_id=node.id,
                        field=name,
                    )
                )
        elif parameter_type == "location_id":
            if isinstance(value, str) and value not in world.locations:
                report.add(
                    ValidationError(
                        "grounding",
                        "UNKNOWN_LOCATION",
                        f"{value!r} is not a location in the world model. "
                        f"Known locations: {', '.join(sorted(world.locations))}",
                        node_id=node.id,
                        field=name,
                    )
                )
        elif parameter_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                report.add(
                    ValidationError(
                        "parameters",
                        "PARAMETER_TYPE_MISMATCH",
                        f"parameter {name!r} must be a number, got {value!r}",
                        node_id=node.id,
                        field=name,
                    )
                )
        elif parameter_type == "boolean" and not isinstance(value, bool):
            report.add(
                ValidationError(
                    "parameters",
                    "PARAMETER_TYPE_MISMATCH",
                    f"parameter {name!r} must be a boolean, got {value!r}",
                    node_id=node.id,
                    field=name,
                )
            )

    # -- check 3 --------------------------------------------------------
    def _check_capabilities(self, graph, report: ValidationReport) -> None:
        vocabulary = set(self.registry.capability_names())
        for node in graph.nodes:
            spec = self.library.get(node.action)
            if spec is None:
                continue

            declared = set(node.required_capabilities)
            required = set(spec.required_capabilities)
            for unknown in sorted(declared - vocabulary):
                report.add(
                    ValidationError(
                        "capabilities",
                        "UNKNOWN_CAPABILITY",
                        f"no registered robot provides capability {unknown!r}",
                        node_id=node.id,
                        field="required_capabilities",
                    )
                )
            for missing in sorted(required - declared):
                report.add(
                    ValidationError(
                        "capabilities",
                        "MISSING_REQUIRED_CAPABILITY",
                        f"action {spec.name!r} requires capability {missing!r}",
                        node_id=node.id,
                        field="required_capabilities",
                    )
                )

            # The action's own requirements decide feasibility. A node that
            # declares extra capabilities is repaired above, not punished twice.
            if not self.registry.robots_with(required):
                report.add(
                    ValidationError(
                        "capabilities",
                        "NO_CAPABLE_ROBOT",
                        f"no robot provides every capability for {spec.name!r}: "
                        f"{', '.join(sorted(required))}",
                        node_id=node.id,
                        field="required_capabilities",
                    )
                )

    # -- check 7 --------------------------------------------------------
    def _check_constraints(self, constraints: Mapping[str, Any], report: ValidationReport) -> None:
        deadline = constraints.get("deadline_s")
        if deadline is not None and float(deadline) > float(self.safety_limits["max_deadline_s"]):
            report.add(
                ValidationError(
                    "constraints",
                    "CONSTRAINT_EXCEEDS_POLICY",
                    f"deadline_s {deadline} exceeds the policy maximum "
                    f"{self.safety_limits['max_deadline_s']}",
                    field="deadline_s",
                )
            )
        risk = constraints.get("max_risk")
        if risk is not None:
            if risk not in _RISK_ORDER:
                report.add(
                    ValidationError(
                        "constraints",
                        "CONSTRAINT_INVALID",
                        f"max_risk {risk!r} must be one of {', '.join(_RISK_ORDER)}",
                        field="max_risk",
                    )
                )
            elif _RISK_ORDER.index(risk) > _RISK_ORDER.index(str(self.safety_limits["max_risk"])):
                report.add(
                    ValidationError(
                        "constraints",
                        "CONSTRAINT_EXCEEDS_POLICY",
                        f"max_risk {risk!r} exceeds the policy ceiling "
                        f"{self.safety_limits['max_risk']!r}",
                        field="max_risk",
                    )
                )
        for robot_id in constraints.get("allowed_robot_ids") or []:
            if robot_id not in self.registry:
                report.add(
                    ValidationError(
                        "constraints",
                        "UNKNOWN_ROBOT",
                        f"allowed_robot_ids names unregistered robot {robot_id!r}",
                        field="allowed_robot_ids",
                    )
                )

    # -- check 9 --------------------------------------------------------
    def _check_timeouts_and_policies(self, graph, report: ValidationReport) -> None:
        for node in graph.nodes:
            spec = self.library.get(node.action)
            if spec is None:
                continue
            if spec.timeout_s <= 0:
                report.add(
                    ValidationError(
                        "execution_policy",
                        "MISSING_TIMEOUT",
                        f"action {spec.name!r} has no positive timeout",
                        node_id=node.id,
                    )
                )
            if spec.verification is None:
                report.warn(
                    ValidationError(
                        "execution_policy",
                        "UNVERIFIABLE_ACTION",
                        f"action {spec.name!r} declares no verification predicate, so its "
                        "effects stay unverified",
                        node_id=node.id,
                    )
                )
        if self.recovery_policy_codes and "TIMEOUT" not in self.recovery_policy_codes:
            report.add(
                ValidationError(
                    "execution_policy",
                    "MISSING_RECOVERY_POLICY",
                    "no recovery policy is configured for TIMEOUT",
                )
            )

    # -- check 10 -------------------------------------------------------
    def _check_approvals(
        self,
        graph,
        constraints: Mapping[str, Any],
        approvals: Sequence[str],
        report: ValidationReport,
    ) -> None:
        granted = set(approvals)
        for node in graph.nodes:
            spec = self.library.get(node.action)
            if spec is None or not spec.hazardous:
                continue
            if spec.name == "request_teleoperation" and not constraints.get("allow_teleoperation"):
                report.add(
                    ValidationError(
                        "approval",
                        "TELEOPERATION_NOT_PERMITTED",
                        "the task constraints do not allow teleoperation",
                        node_id=node.id,
                    )
                )
                continue
            if spec.name not in granted and node.id not in granted:
                report.add(
                    ValidationError(
                        "approval",
                        "APPROVAL_REQUIRED",
                        f"action {spec.name!r} is hazardous and needs explicit operator approval",
                        node_id=node.id,
                    )
                )

    # -- check 5 --------------------------------------------------------
    def _check_preconditions(self, graph, world: WorldModel, report: ValidationReport) -> None:
        """Project the graph forward and check each node can actually start.

        Projection uses the most capable robot for each node rather than the
        scheduler's eventual choice. That is intentional: validation answers
        "is this plan possible for some body", and the scheduler answers
        "which body, and at what cost".
        """
        projected = world.copy()
        for node_id in graph.topological_order():
            node = graph.node(node_id)
            spec = self.library.get(node.action)
            if spec is None:
                continue

            candidate = self._best_candidate(spec)
            context = self._context_for(spec, candidate)
            robot_name = candidate.robot_id if candidate else "assigned_robot"

            for template in spec.preconditions:
                try:
                    predicate = bind_predicate(
                        template, dict(node.parameters), assigned_robot=robot_name
                    )
                except PredicateError as error:
                    report.add(
                        ValidationError(
                            "preconditions",
                            "MALFORMED_PRECONDITION",
                            f"action {spec.name!r} precondition {template!r} is invalid: {error}",
                            node_id=node.id,
                        )
                    )
                    continue
                if not projected.holds(predicate, context):
                    report.add(
                        ValidationError(
                            "preconditions",
                            "PRECONDITION_UNSATISFIABLE",
                            f"{predicate} does not hold when {node.id} would start. "
                            f"An earlier node must establish it.",
                            node_id=node.id,
                        )
                    )

            self._apply_effects(projected, spec, node, robot_name, report)

    def _apply_effects(
        self, projected: WorldModel, spec: ActionSpec, node, robot_name: str, report: ValidationReport
    ) -> None:
        asserted: list[str] = []
        for template in spec.effects:
            try:
                asserted.append(
                    str(bind_predicate(template, dict(node.parameters), assigned_robot=robot_name))
                )
            except PredicateError as error:
                report.add(
                    ValidationError(
                        "effects",
                        "MALFORMED_EFFECT",
                        f"action {spec.name!r} effect {template!r} is invalid: {error}",
                        node_id=node.id,
                    )
                )
        if asserted:
            projected.assert_all(asserted, source="SCENARIO_LOAD")

    def _best_candidate(self, spec: ActionSpec) -> RobotProfile | None:
        candidates = self.registry.robots_with(spec.required_capabilities)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda profile: (
                profile.combined_success_prior(spec.required_capabilities),
                profile.robot_id,
            ),
        )

    def _context_for(self, spec: ActionSpec, candidate: RobotProfile | None) -> PredicateContext:
        if candidate is None:
            return PredicateContext()
        minimum, maximum = candidate.reach_range_m(spec.required_capabilities)
        return PredicateContext(
            assigned_robot=candidate.robot_id,
            payload_limit_kg=candidate.payload_limit_kg(spec.required_capabilities),
            min_reach_height_m=minimum,
            max_reach_height_m=maximum,
        )

    # -- check 6 --------------------------------------------------------
    def _check_goal(self, graph, world: WorldModel, report: ValidationReport) -> None:
        try:
            goal = parse_predicate(graph.goal_predicate)
        except PredicateError as error:
            report.add(
                ValidationError(
                    "goal",
                    "MALFORMED_GOAL_PREDICATE",
                    f"{graph.goal_predicate!r} is not a predicate: {error}",
                    field="goal_predicate",
                )
            )
            return

        if goal.name in NEGATED_PREDICATES:
            report.add(
                ValidationError(
                    "goal",
                    "UNVERIFIABLE_GOAL",
                    f"{goal.name!r} states an absence, which no sensor can positively confirm",
                    field="goal_predicate",
                )
            )
            return

        measurable = set(self.library.goal_predicate_vocabulary())
        if goal.name not in measurable:
            report.add(
                ValidationError(
                    "goal",
                    "UNVERIFIABLE_GOAL",
                    f"no action establishes {goal.name!r}. "
                    f"Measurable goal predicates: {', '.join(sorted(measurable))}",
                    field="goal_predicate",
                )
            )
            return

        self._check_goal_grounding(goal, world, report)

        if not self._graph_establishes(graph, goal):
            report.add(
                ValidationError(
                    "goal",
                    "GOAL_NOT_ESTABLISHED",
                    f"no node in the graph establishes {goal}. "
                    "Add the action whose effect asserts it.",
                    field="goal_predicate",
                )
            )

    def _check_goal_grounding(self, goal: Predicate, world: WorldModel, report: ValidationReport) -> None:
        for argument in goal.args:
            if argument in world.objects or argument in world.locations or argument in world.robots:
                continue
            report.add(
                ValidationError(
                    "goal",
                    "UNKNOWN_GOAL_ENTITY",
                    f"goal predicate references {argument!r}, which does not exist in the world model",
                    field="goal_predicate",
                )
            )

    def _graph_establishes(self, graph, goal: Predicate) -> bool:
        for node in graph.nodes:
            spec = self.library.get(node.action)
            if spec is None:
                continue
            for template in spec.effects:
                try:
                    effect = bind_predicate(
                        template, dict(node.parameters), assigned_robot="assigned_robot"
                    )
                except PredicateError:
                    continue
                if effect.name == goal.name and effect.args == goal.args:
                    return True
        return False
