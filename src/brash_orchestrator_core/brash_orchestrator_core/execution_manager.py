"""Execution manager: walk the graph, honour dependencies, close the loop.

Nodes with no ordering relationship run concurrently, which is what lets the
humanoid inspect the destination shelf while the quadruped is still carrying the
box. Each node gets its own timeout, a failure consults the recovery planner, and
the whole task is cancellable at any point.

Two rules are enforced here rather than trusted elsewhere:

* The safety manager is asked before every submission, and its answer is final.
  It is not consulted by the LLM and cannot be overridden by a plan.
* A task reports success only when the goal predicate is *verified*. A graph
  whose every node succeeded but whose goal was never observed is a failure with
  a specific code, not a success.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from brash_orchestrator_core.action_library import ActionLibrary, ActionSpec
from brash_orchestrator_core.backends.base import (
    ExecutionBackend,
    PrimitiveRequest,
    PrimitiveResult,
)
from brash_orchestrator_core.capability_registry import CapabilityRegistry
from brash_orchestrator_core.events import EventLog, ExecutionEvent
from brash_orchestrator_core.execution_monitor import (
    ExecutionMonitor,
    occupancy_update_for,
)
from brash_orchestrator_core.scheduler import ExecutionPlan, Scheduler
from brash_orchestrator_core.task_graph import TaskGraph, TaskNode
from brash_world_model.model import WorldModel
from brash_world_model.predicates import bind_predicate, PredicateError

NODE_STATES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "SKIPPED", "CANCELLED")


class SafetyGate(Protocol):
    """The safety manager's veto, kept structural so it can be swapped in tests."""

    def evaluate(
        self, request: PrimitiveRequest, world: WorldModel
    ) -> tuple[bool, str, str]:
        """Return ``(permitted, error_code, detail)``."""


class RecoveryDecider(Protocol):
    """The recovery planner, as the execution manager needs it."""

    async def decide(
        self,
        *,
        node: TaskNode,
        spec: ActionSpec,
        result: PrimitiveResult,
        graph: TaskGraph,
        world: WorldModel,
        attempt: int,
        history: Sequence[ExecutionEvent],
        constraints: Mapping[str, Any],
    ) -> Any:
        """Return a recovery decision, or ``None`` when nothing is permitted."""


@dataclass
class NodeOutcome:
    node_id: str
    action: str
    state: str = "PENDING"
    error_code: str = "SUCCESS"
    assigned_robot_id: str | None = None
    attempts: int = 0
    duration_s: float = 0.0
    external_reference: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "action": self.action,
            "assigned_robot_id": self.assigned_robot_id,
            "state": self.state,
            "error_code": self.error_code,
            "attempts": self.attempts,
            "duration_s": round(self.duration_s, 2),
            "external_reference": self.external_reference,
        }


@dataclass
class ExecutionOutcome:
    task_id: str
    success: bool
    failure_code: str
    goal_predicate: str
    goal_verified: bool
    summary: str
    dry_run: bool
    started_at: str
    finished_at: str
    duration_s: float
    nodes: list[NodeOutcome] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    recovery_actions: list[dict[str, Any]] = field(default_factory=list)
    human_interventions: int = 0
    final_world_state: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "success": self.success,
            "failure_code": self.failure_code,
            "goal_predicate": self.goal_predicate,
            "goal_verified": self.goal_verified,
            "summary": self.summary,
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.duration_s, 2),
            "human_interventions": self.human_interventions,
            "recovery_actions": list(self.recovery_actions),
            "nodes": [node.to_dict() for node in self.nodes],
            "final_world_state": self.final_world_state,
            "events": list(self.events),
        }


class ExecutionManager:
    def __init__(
        self,
        *,
        library: ActionLibrary,
        registry: CapabilityRegistry,
        backend: ExecutionBackend,
        scheduler: Scheduler,
        safety: SafetyGate | None = None,
        recovery: RecoveryDecider | None = None,
        dry_run: bool = False,
        max_attempts: int = 3,
        max_recovery_actions: int = 6,
    ) -> None:
        self.library = library
        self.registry = registry
        self.backend = backend
        self.scheduler = scheduler
        self.safety = safety
        self.recovery = recovery
        self.dry_run = dry_run
        self.max_attempts = max_attempts
        self.max_recovery_actions = max_recovery_actions

    async def execute(
        self,
        graph: TaskGraph,
        plan: ExecutionPlan,
        world: WorldModel,
        *,
        events: EventLog | None = None,
        constraints: Mapping[str, Any] | None = None,
    ) -> ExecutionOutcome:
        constraints = dict(constraints or {})
        log = events or EventLog(task_id=graph.task_id)
        log.task_id = graph.task_id
        monitor = ExecutionMonitor(world)

        started_at = _now()
        loop = asyncio.get_running_loop()
        start_time = loop.time()

        outcomes: dict[str, NodeOutcome] = {
            node.id: NodeOutcome(
                node_id=node.id,
                action=node.action,
                assigned_robot_id=plan.robot_for(node.id),
            )
            for node in graph.nodes
        }
        recovery_actions: list[dict[str, Any]] = []
        interventions = 0
        deadline = (
            start_time + float(constraints["deadline_s"])
            if constraints.get("deadline_s")
            else None
        )

        if not plan.feasible:
            for node_id in plan.infeasible_nodes:
                outcomes[node_id].state = "FAILED"
                outcomes[node_id].error_code = "NO_CAPABLE_ROBOT"
                log.record(
                    "NODE_FAILED",
                    node_id=node_id,
                    error_code="NO_CAPABLE_ROBOT",
                    action=outcomes[node_id].action,
                    detail=plan.assignments[node_id].rationale,
                )
            return self._finalize(
                graph=graph,
                world=world,
                monitor=monitor,
                log=log,
                outcomes=outcomes,
                recovery_actions=recovery_actions,
                interventions=interventions,
                started_at=started_at,
                duration_s=loop.time() - start_time,
                failure_code="NO_CAPABLE_ROBOT",
            )

        cancelled = False
        expired = False
        # The frontier is recomputed every wave because recovery can insert
        # nodes and change the graph while it is running.
        while True:
            if cancelled:
                break
            if deadline is not None and loop.time() > deadline:
                expired = True
                break

            ready = self._ready_nodes(graph, outcomes)
            if not ready:
                break

            results = await asyncio.gather(
                *(
                    self._run_node(
                        node=graph.node(node_id),
                        graph=graph,
                        plan=plan,
                        world=world,
                        monitor=monitor,
                        log=log,
                        outcomes=outcomes,
                        constraints=constraints,
                        recovery_actions=recovery_actions,
                    )
                    for node_id in ready
                ),
                return_exceptions=True,
            )
            for node_id, result in zip(ready, results):
                if isinstance(result, asyncio.CancelledError):
                    cancelled = True
                elif isinstance(result, BaseException):
                    outcomes[node_id].state = "FAILED"
                    outcomes[node_id].error_code = "UNKNOWN_FAILURE"
                    outcomes[node_id].detail = str(result)
                    log.record(
                        "NODE_FAILED",
                        node_id=node_id,
                        error_code="UNKNOWN_FAILURE",
                        action=outcomes[node_id].action,
                        detail=str(result),
                    )
                elif result == "ESCALATED":
                    interventions += 1

            # A failed node makes everything downstream unreachable. Marking it
            # explicitly is what stops the loop and produces an honest report.
            for node_id, outcome in outcomes.items():
                if outcome.state != "FAILED":
                    continue
                for dependent in graph.descendants(node_id):
                    if outcomes[dependent].state == "PENDING":
                        outcomes[dependent].state = "SKIPPED"
                        outcomes[dependent].error_code = "PRECONDITION_FAILED"
                        log.record(
                            "NODE_SKIPPED",
                            node_id=dependent,
                            error_code="PRECONDITION_FAILED",
                            action=outcomes[dependent].action,
                            detail=f"upstream node {node_id} failed",
                        )

        # Work that never got its turn is abandoned, not pending. Leaving it
        # PENDING would report a task as neither finished nor failed.
        if cancelled or expired:
            reason = "the task was cancelled" if cancelled else "the task deadline expired"
            for node_id, outcome in outcomes.items():
                if outcome.state != "PENDING":
                    continue
                outcome.state = "CANCELLED"
                outcome.error_code = "CANCELLED" if cancelled else "TIMEOUT"
                outcome.detail = f"never started because {reason}"
                log.record(
                    "NODE_CANCELLED",
                    node_id=node_id,
                    error_code=outcome.error_code,
                    action=outcome.action,
                    detail=outcome.detail,
                )

        failure_code = self._first_failure_code(outcomes)
        if expired:
            failure_code = "TIMEOUT"
        if cancelled:
            failure_code = "CANCELLED"
        return self._finalize(
            graph=graph,
            world=world,
            monitor=monitor,
            log=log,
            outcomes=outcomes,
            recovery_actions=recovery_actions,
            interventions=interventions,
            started_at=started_at,
            duration_s=loop.time() - start_time,
            failure_code=failure_code,
        )

    # ------------------------------------------------------------------
    def _ready_nodes(self, graph: TaskGraph, outcomes: Mapping[str, NodeOutcome]) -> list[str]:
        ready: list[str] = []
        for node in graph.nodes:
            outcome = outcomes.get(node.id)
            if outcome is None or outcome.state != "PENDING":
                continue
            if all(
                outcomes.get(dependency) is not None
                and outcomes[dependency].state == "SUCCEEDED"
                for dependency in node.depends_on
            ):
                ready.append(node.id)
        return ready

    async def _run_node(
        self,
        *,
        node: TaskNode,
        graph: TaskGraph,
        plan: ExecutionPlan,
        world: WorldModel,
        monitor: ExecutionMonitor,
        log: EventLog,
        outcomes: dict[str, NodeOutcome],
        constraints: Mapping[str, Any],
        recovery_actions: list[dict[str, Any]],
    ) -> str:
        outcome = outcomes[node.id]
        spec = self.library.get(node.action)
        if spec is None:
            outcome.state = "FAILED"
            outcome.error_code = "VALIDATION_FAILED"
            log.record(
                "NODE_FAILED",
                node_id=node.id,
                error_code="VALIDATION_FAILED",
                action=node.action,
                detail=f"action {node.action!r} is not in the Action Library",
            )
            return "FAILED"

        outcome.state = "RUNNING"
        loop = asyncio.get_running_loop()

        while True:
            outcome.attempts += 1
            attempt = outcome.attempts
            robot_id = plan.robot_for(node.id)
            if robot_id is None:
                outcome.state = "FAILED"
                outcome.error_code = "NO_CAPABLE_ROBOT"
                log.record(
                    "NODE_FAILED",
                    node_id=node.id,
                    error_code="NO_CAPABLE_ROBOT",
                    action=node.action,
                    attempt=attempt,
                )
                return "FAILED"
            outcome.assigned_robot_id = robot_id

            request = self._build_request(
                node, spec, robot_id, world, attempt, graph.task_id
            )
            log.record(
                "NODE_STARTED",
                node_id=node.id,
                action=node.action,
                robot_id=robot_id,
                attempt=attempt,
                external_reference=request.external_reference,
                detail=f"{spec.execution.kind} primitive {spec.execution.primitive}",
            )

            permitted, code, detail = self._check_safety(request, world)
            if not permitted:
                outcome.state = "FAILED"
                outcome.error_code = code
                outcome.detail = detail
                log.record(
                    "SAFETY_BLOCKED",
                    node_id=node.id,
                    error_code=code,
                    action=node.action,
                    robot_id=robot_id,
                    attempt=attempt,
                    detail=detail,
                )
                return "FAILED"

            node_started = loop.time()
            if self.dry_run:
                result = PrimitiveResult.succeeded(
                    detail="dry run: no work order was submitted",
                    metrics={"duration_s": 0.0},
                )
            else:
                try:
                    result = await asyncio.wait_for(
                        self.backend.execute(request),
                        timeout=spec.timeout_s + 5.0,
                    )
                except asyncio.TimeoutError:
                    await self.backend.cancel(request)
                    result = PrimitiveResult.failed(
                        "TIMEOUT",
                        detail=f"backend did not return within {spec.timeout_s:.0f}s",
                    )
                except asyncio.CancelledError:
                    await self.backend.cancel(request)
                    outcome.state = "CANCELLED"
                    outcome.error_code = "CANCELLED"
                    log.record(
                        "NODE_CANCELLED",
                        node_id=node.id,
                        error_code="CANCELLED",
                        action=node.action,
                        robot_id=robot_id,
                        attempt=attempt,
                    )
                    raise
            outcome.duration_s += loop.time() - node_started
            outcome.external_reference = result.external_reference

            if result.success:
                return self._on_node_success(
                    node=node,
                    spec=spec,
                    robot_id=robot_id,
                    result=result,
                    monitor=monitor,
                    log=log,
                    outcome=outcome,
                    attempt=attempt,
                )

            outcome.error_code = result.error_code
            outcome.detail = result.detail
            log.record(
                "NODE_FAILED",
                node_id=node.id,
                error_code=result.error_code,
                action=node.action,
                robot_id=robot_id,
                attempt=attempt,
                detail=result.detail,
                external_reference=result.external_reference,
                observations=result.observed_effects,
                metrics=result.metrics,
            )
            monitor.on_failure(node, spec, robot_id, result)
            for path, value in occupancy_update_for(result.error_code, node).items():
                try:
                    world.set_attribute(path, value)
                except Exception:  # noqa: BLE001
                    pass

            decision = await self._decide_recovery(
                node=node,
                spec=spec,
                result=result,
                graph=graph,
                world=world,
                attempt=attempt,
                log=log,
                constraints=constraints,
            )
            if decision is None:
                outcome.state = "FAILED"
                log.record(
                    "RECOVERY_EXHAUSTED",
                    node_id=node.id,
                    error_code=result.error_code,
                    action=node.action,
                    attempt=attempt,
                    detail="no permitted recovery option remains",
                )
                return "FAILED"

            recovery_actions.append(decision.to_dict())
            if len(recovery_actions) > self.max_recovery_actions:
                outcome.state = "FAILED"
                log.record(
                    "RECOVERY_EXHAUSTED",
                    node_id=node.id,
                    error_code=result.error_code,
                    action=node.action,
                    detail=(
                        f"recovery budget of {self.max_recovery_actions} actions "
                        "for this task is spent"
                    ),
                )
                return "FAILED"

            applied = self._apply_recovery(
                decision=decision,
                node=node,
                graph=graph,
                plan=plan,
                world=world,
                outcomes=outcomes,
                log=log,
                constraints=constraints,
            )
            if applied == "ESCALATED":
                outcome.state = "FAILED"
                outcome.error_code = result.error_code
                return "ESCALATED"
            if applied == "RESET":
                # A node was inserted upstream, so this node goes back to
                # PENDING and waits for it. The attempt count is not rolled
                # back, so max_attempts still bounds the total tries.
                outcome.state = "PENDING"
                return "DEFERRED"
            if applied == "STOP":
                outcome.state = "FAILED"
                return "FAILED"

            if outcome.attempts >= self.max_attempts:
                outcome.state = "FAILED"
                log.record(
                    "RECOVERY_EXHAUSTED",
                    node_id=node.id,
                    error_code=result.error_code,
                    action=node.action,
                    detail=f"attempt limit {self.max_attempts} reached",
                )
                return "FAILED"

    def _on_node_success(
        self,
        *,
        node: TaskNode,
        spec: ActionSpec,
        robot_id: str,
        result: PrimitiveResult,
        monitor: ExecutionMonitor,
        log: EventLog,
        outcome: NodeOutcome,
        attempt: int,
    ) -> str:
        outcome.state = "SUCCEEDED"
        outcome.error_code = "SUCCESS"
        log.record(
            "NODE_SUCCEEDED",
            node_id=node.id,
            action=node.action,
            robot_id=robot_id,
            attempt=attempt,
            external_reference=result.external_reference,
            observations=result.observed_effects,
            metrics=result.metrics,
            detail=result.detail,
        )

        if self.dry_run:
            # A dry run must not fabricate observations, so effects are recorded
            # as claims and the goal stays unverified.
            self._apply_dry_run_effects(node, spec, robot_id, monitor)
            return "SUCCEEDED"

        reconciliation = monitor.on_success(node, spec, robot_id, result)
        if reconciliation.verified:
            log.record(
                "EFFECT_VERIFIED",
                node_id=node.id,
                action=node.action,
                robot_id=robot_id,
                attempt=attempt,
                detail="; ".join(reconciliation.verified),
                observations=reconciliation.to_dict(),
            )
        if reconciliation.claimed:
            log.record(
                "EFFECT_MISMATCH",
                node_id=node.id,
                action=node.action,
                robot_id=robot_id,
                attempt=attempt,
                detail=(
                    "reported succeeded but no observation confirmed: "
                    + "; ".join(reconciliation.claimed)
                ),
                observations=reconciliation.to_dict(),
            )
        return "SUCCEEDED"

    def _apply_dry_run_effects(
        self, node: TaskNode, spec: ActionSpec, robot_id: str, monitor: ExecutionMonitor
    ) -> None:
        from brash_world_model.model import WorldStateDelta

        asserted: list[str] = []
        for template in spec.effects:
            try:
                asserted.append(
                    str(bind_predicate(template, dict(node.parameters), assigned_robot=robot_id))
                )
            except PredicateError:
                continue
        if asserted:
            monitor.world.apply_delta(
                WorldStateDelta(asserted=asserted, source="BACKEND_CLAIM", node_id=node.id)
            )

    # ------------------------------------------------------------------
    def _build_request(
        self,
        node: TaskNode,
        spec: ActionSpec,
        robot_id: str,
        world: WorldModel,
        attempt: int,
        task_id: str,
    ) -> PrimitiveRequest:
        execution = spec.execution
        profile = self.registry.get(robot_id)
        fleet_robot_id = profile.fleet_robot_id if profile else robot_id

        pickup = self._location_for(execution.pickup_from, node, world)
        dropoff = self._location_for(execution.dropoff_to, node, world)
        station = self._location_for(execution.station_from, node, world)
        payload_source = execution.payload_from or execution.object_from
        payload_kg = (
            world.object_mass(str(node.parameters[payload_source]))
            if payload_source and payload_source in node.parameters
            else 0.0
        )

        object_id = (
            str(node.parameters[execution.object_from])
            if execution.object_from and execution.object_from in node.parameters
            else None
        )
        container_id = (
            str(node.parameters[execution.container_from])
            if execution.container_from and execution.container_from in node.parameters
            else None
        )
        grasp_region = None
        if object_id:
            world_object = world.objects.get(object_id)
            grasp_region = world_object.preferred_grasp_region if world_object else None

        verification: str | None = None
        if spec.verification:
            try:
                verification = str(
                    bind_predicate(
                        spec.verification, dict(node.parameters), assigned_robot=robot_id
                    )
                )
            except PredicateError:
                verification = None

        return PrimitiveRequest(
            task_id=task_id,
            node_id=node.id,
            action_id=spec.name,
            primitive=execution.primitive,
            kind=execution.kind,
            robot_id=fleet_robot_id,
            parameters=dict(node.parameters),
            timeout_s=spec.timeout_s,
            fleet_capabilities=execution.fleet_capabilities,
            pickup=pickup,
            dropoff=dropoff,
            station=station,
            payload_kg=payload_kg,
            object_id=object_id,
            container_id=container_id,
            grasp_region=grasp_region,
            attempt=attempt,
            verification=verification,
        )

    def _location_for(self, reference: str | None, node: TaskNode, world: WorldModel):
        if reference is None or reference not in node.parameters:
            return None
        value = str(node.parameters[reference])
        location_id = world.resolve_location(value)
        if location_id is None:
            return None
        return world.mission_location(location_id)

    def _check_safety(
        self, request: PrimitiveRequest, world: WorldModel
    ) -> tuple[bool, str, str]:
        if self.safety is None:
            return True, "SUCCESS", ""
        return self.safety.evaluate(request, world)

    # ------------------------------------------------------------------
    async def _decide_recovery(
        self,
        *,
        node: TaskNode,
        spec: ActionSpec,
        result: PrimitiveResult,
        graph: TaskGraph,
        world: WorldModel,
        attempt: int,
        log: EventLog,
        constraints: Mapping[str, Any],
    ):
        if self.recovery is None or attempt >= self.max_attempts:
            return None
        decision = await self.recovery.decide(
            node=node,
            spec=spec,
            result=result,
            graph=graph,
            world=world,
            attempt=attempt,
            history=log.for_node(node.id),
            constraints=constraints,
        )
        if decision is not None:
            log.record(
                "RECOVERY_PROPOSED",
                node_id=node.id,
                error_code=result.error_code,
                action=node.action,
                attempt=attempt,
                detail=f"{decision.candidate_id} ({decision.strategy}): {decision.reason}",
                observations=decision.to_dict(),
            )
        return decision

    def _apply_recovery(
        self,
        *,
        decision,
        node: TaskNode,
        graph: TaskGraph,
        plan: ExecutionPlan,
        world: WorldModel,
        outcomes: dict[str, NodeOutcome],
        log: EventLog,
        constraints: Mapping[str, Any],
    ) -> str:
        strategy = decision.strategy

        if strategy == "escalate":
            log.record(
                "APPROVAL_REQUIRED",
                node_id=node.id,
                error_code="PRECONDITION_FAILED",
                action=node.action,
                detail=decision.reason,
            )
            return "ESCALATED"

        if strategy == "abort":
            return "STOP"

        if strategy == "retry":
            log.record(
                "RECOVERY_APPLIED",
                node_id=node.id,
                action=node.action,
                detail=f"retrying with {plan.robot_for(node.id)}",
            )
            return "CONTINUE"

        if strategy == "reassign":
            excluded = {plan.robot_for(node.id)} | set(decision.excluded_robot_ids)
            replacement = self._reassign(node, plan, world, excluded, constraints)
            if replacement is None:
                log.record(
                    "RECOVERY_EXHAUSTED",
                    node_id=node.id,
                    error_code="NO_CAPABLE_ROBOT",
                    action=node.action,
                    detail="no other eligible robot is available",
                )
                return "STOP"
            log.record(
                "RECOVERY_APPLIED",
                node_id=node.id,
                action=node.action,
                robot_id=replacement,
                detail=f"reassigned from {', '.join(sorted(r for r in excluded if r))}",
            )
            return "CONTINUE"

        if strategy == "alternate_target":
            node.parameters[decision.parameter] = decision.value
            self._replan_node(node, plan, world, constraints)
            log.record(
                "RECOVERY_APPLIED",
                node_id=node.id,
                action=node.action,
                detail=f"{decision.parameter} changed to {decision.value}",
            )
            return "CONTINUE"

        if strategy == "insert_before":
            inserted = decision.node
            graph.insert_before(node.id, inserted)
            outcomes[inserted.id] = NodeOutcome(node_id=inserted.id, action=inserted.action)
            self._replan_node(inserted, plan, world, constraints)
            log.record(
                "RECOVERY_APPLIED",
                node_id=node.id,
                action=node.action,
                detail=(
                    f"inserted {inserted.id} {inserted.action} before {node.id}; "
                    f"{node.id} now waits for it"
                ),
            )
            return "RESET"

        return "STOP"

    def _reassign(
        self,
        node: TaskNode,
        plan: ExecutionPlan,
        world: WorldModel,
        excluded: set[str | None],
        constraints: Mapping[str, Any],
    ) -> str | None:
        assignment = plan.assignments.get(node.id)
        if assignment is None:
            return None
        for candidate in assignment.candidates:
            if candidate.eligible and candidate.robot_id not in excluded:
                assignment.assigned_robot_id = candidate.robot_id
                assignment.rationale = (
                    f"reassigned after failure; cost {candidate.cost:.3f}"
                    if candidate.cost is not None
                    else "reassigned after failure"
                )
                return candidate.robot_id
        return None

    def _replan_node(
        self,
        node: TaskNode,
        plan: ExecutionPlan,
        world: WorldModel,
        constraints: Mapping[str, Any],
    ) -> None:
        """Score a newly inserted or retargeted node against current state."""
        single = TaskGraph(
            task_id=plan.task_id, goal_predicate=plan.goal_predicate, nodes=[
                TaskNode(
                    id=node.id,
                    action=node.action,
                    parameters=dict(node.parameters),
                    required_capabilities=node.required_capabilities,
                    depends_on=(),
                )
            ]
        )
        refreshed = self.scheduler.plan(single, world, constraints=constraints)
        assignment = refreshed.assignments.get(node.id)
        if assignment is not None:
            plan.assignments[node.id] = assignment

    # ------------------------------------------------------------------
    def _first_failure_code(self, outcomes: Mapping[str, NodeOutcome]) -> str:
        for outcome in outcomes.values():
            if outcome.state in {"FAILED", "CANCELLED"} and outcome.error_code != "SUCCESS":
                return outcome.error_code
        return "SUCCESS"

    def _finalize(
        self,
        *,
        graph: TaskGraph,
        world: WorldModel,
        monitor: ExecutionMonitor,
        log: EventLog,
        outcomes: dict[str, NodeOutcome],
        recovery_actions: list[dict[str, Any]],
        interventions: int,
        started_at: str,
        duration_s: float,
        failure_code: str,
    ) -> ExecutionOutcome:
        holds, verified = monitor.goal_status(graph.goal_predicate)
        all_succeeded = all(
            outcome.state == "SUCCEEDED" for outcome in outcomes.values()
        )
        # A dry run cannot verify anything because nothing was executed. It
        # succeeds when the plan is executable, and says so.
        success = all_succeeded and failure_code == "SUCCESS" and (verified or self.dry_run)

        if success:
            summary = (
                f"dry run: all {len(outcomes)} nodes are executable; "
                f"{graph.goal_predicate} was not verified because nothing was executed"
                if self.dry_run
                else f"{graph.goal_predicate} verified after {len(outcomes)} nodes "
                f"in {duration_s:.0f}s"
            )
            log.record("TASK_SUCCEEDED", detail=summary)
        else:
            if all_succeeded and not verified:
                # This is the case worth being loud about: every action reported
                # success and the goal was still never observed.
                failure_code = "VALIDATION_FAILED" if holds else "OBJECT_POSE_UNCERTAIN"
                summary = (
                    f"every node reported success but {graph.goal_predicate} was never "
                    "verified by an observation, so the task is not reported complete"
                )
            else:
                failed = [
                    f"{outcome.node_id} {outcome.action} [{outcome.error_code}]"
                    for outcome in outcomes.values()
                    if outcome.state in {"FAILED", "CANCELLED"}
                ]
                if failure_code == "SUCCESS":
                    # Nothing failed outright and the task still did not finish,
                    # so reporting SUCCESS alongside a failed task would be a lie.
                    failure_code = "UNKNOWN_FAILURE"
                summary = f"task did not complete: {'; '.join(failed) or failure_code}"
            log.record("TASK_FAILED", error_code=failure_code or "UNKNOWN_FAILURE", detail=summary)

        return ExecutionOutcome(
            task_id=graph.task_id,
            success=success,
            failure_code="SUCCESS" if success else (failure_code or "UNKNOWN_FAILURE"),
            goal_predicate=graph.goal_predicate,
            goal_verified=verified,
            summary=summary,
            dry_run=self.dry_run,
            started_at=started_at,
            finished_at=_now(),
            duration_s=duration_s,
            nodes=[outcomes[node.id] for node in graph.nodes if node.id in outcomes],
            events=log.to_list(),
            recovery_actions=recovery_actions,
            human_interventions=interventions,
            final_world_state=world.snapshot(),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
