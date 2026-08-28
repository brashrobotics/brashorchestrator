"""The orchestrator facade: one object that wires the whole pipeline together.

The CLI and the ROS 2 node are both thin shells over this class, which keeps the
end-to-end behaviour in one testable place instead of duplicated across two
entry points.

The pipeline is deliberately ordered so that each stage can only narrow what the
previous one allowed:

    goal -> interpret -> validate -> schedule -> safety -> execute -> verify

Nothing later in the chain can widen an earlier decision. The interpreter cannot
pick a robot, the scheduler cannot invent an action, and neither can overrule the
safety manager.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from brash_orchestrator_core.action_library import ActionLibrary
from brash_orchestrator_core.backends.base import ExecutionBackend
from brash_orchestrator_core.backends.fleet_control import (
    FleetControlBackend,
    FleetControlSettings,
)
from brash_orchestrator_core.capability_registry import CapabilityRegistry
from brash_orchestrator_core.events import EventLog
from brash_orchestrator_core.execution_manager import ExecutionManager, ExecutionOutcome
from brash_orchestrator_core.llm.provider import LlmError, LlmProvider
from brash_orchestrator_core.llm.settings import LlmSettings
from brash_orchestrator_core.paths import config_path
from brash_orchestrator_core.recovery_planner import RecoveryPlanner, RecoveryPolicies
from brash_orchestrator_core.scheduler import ExecutionPlan, Scheduler, SchedulerConfig
from brash_orchestrator_core.schema_registry import SchemaRegistry
from brash_orchestrator_core.task_graph import TaskGraph
from brash_orchestrator_core.task_interpreter import InterpretationResult, TaskInterpreter
from brash_orchestrator_core.task_validator import TaskValidator
from brash_orchestrator_core.trial_recorder import TrialRecorder
from brash_safety_manager.policy import SafetyManager, SafetyPolicy
from brash_world_model.model import WorldModel
from brash_world_model.twin_bundle_loader import (
    TwinBundleError,
    ground_world_model,
    load_twin_bundle,
)


@dataclass
class TaskResult:
    """Everything one submitted task produced."""

    task_id: str
    goal: str
    interpretation: InterpretationResult | None = None
    plan: ExecutionPlan | None = None
    outcome: ExecutionOutcome | None = None
    events: EventLog = field(default_factory=EventLog)
    rejection_code: str = ""
    rejection_detail: str = ""

    @property
    def success(self) -> bool:
        return bool(self.outcome and self.outcome.success)

    @property
    def failure_code(self) -> str:
        if self.rejection_code:
            return self.rejection_code
        return self.outcome.failure_code if self.outcome else "UNKNOWN_FAILURE"

    @property
    def summary(self) -> str:
        if self.rejection_detail:
            return self.rejection_detail
        return self.outcome.summary if self.outcome else "task produced no outcome"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "success": self.success,
            "failure_code": self.failure_code,
            "summary": self.summary,
            "interpretation": self.interpretation.to_dict() if self.interpretation else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "outcome": self.outcome.to_dict() if self.outcome else None,
            "events": self.events.to_list(),
        }


class Orchestrator:
    def __init__(
        self,
        *,
        world: WorldModel,
        library: ActionLibrary,
        registry: CapabilityRegistry,
        scheduler: Scheduler,
        validator: TaskValidator,
        backend: ExecutionBackend,
        llm_settings: LlmSettings,
        provider: LlmProvider,
        recovery: RecoveryPlanner,
        safety: SafetyManager,
        schemas: SchemaRegistry,
        recorder: TrialRecorder | None = None,
        dry_run: bool = False,
    ) -> None:
        self.world = world
        self.library = library
        self.registry = registry
        self.scheduler = scheduler
        self.validator = validator
        self.backend = backend
        self.llm_settings = llm_settings
        self.provider = provider
        self.recovery = recovery
        self.safety = safety
        self.schemas = schemas
        self.recorder = recorder
        self.dry_run = dry_run
        self.interpreter = TaskInterpreter(
            provider=provider,
            settings=llm_settings,
            library=library,
            registry=registry,
            validator=validator,
            schemas=schemas,
        )

    # ------------------------------------------------------------------
    @classmethod
    def build(
        cls,
        *,
        scenario_path: str | Path | None = None,
        twin_bundle_path: str | Path | None = None,
        llm_profile: str | None = None,
        backend: ExecutionBackend | None = None,
        fleet_settings: FleetControlSettings | None = None,
        dry_run: bool = False,
        record_trials: bool = True,
        approvals: Sequence[str] = (),
    ) -> Orchestrator:
        """Construct a fully wired orchestrator from configuration on disk."""
        library = ActionLibrary.load()
        registry = CapabilityRegistry.load()
        policies = RecoveryPolicies.load()
        schemas = SchemaRegistry.load()

        world = WorldModel.load(scenario_path or config_path("world_demo.yaml"))
        if twin_bundle_path:
            bundle = load_twin_bundle(twin_bundle_path)
            ungrounded = ground_world_model(world, bundle)
            if ungrounded:
                # Not fatal: planning still works, but the operator must know
                # which locations are not backed by a surveyed map.
                print(
                    "warning: the twin bundle did not ground these locations: "
                    + ", ".join(ungrounded)
                )

        llm_settings = LlmSettings.load(profile=llm_profile)
        provider = llm_settings.build_provider()

        validator = TaskValidator(
            library, registry, recovery_policy_codes=policies.error_codes
        )
        scheduler = Scheduler(library, registry, SchedulerConfig.load())
        recovery = RecoveryPlanner(
            policies=policies,
            library=library,
            provider=provider,
            settings=llm_settings,
            schemas=schemas,
        )
        safety = SafetyManager(SafetyPolicy.load(), approvals=set(approvals))

        return cls(
            world=world,
            library=library,
            registry=registry,
            scheduler=scheduler,
            validator=validator,
            backend=backend or FleetControlBackend(fleet_settings),
            llm_settings=llm_settings,
            provider=provider,
            recovery=recovery,
            safety=safety,
            schemas=schemas,
            recorder=TrialRecorder.from_env() if record_trials else None,
            dry_run=dry_run,
        )

    async def close(self) -> None:
        await self.provider.close()
        await self.backend.close()

    # ------------------------------------------------------------------
    async def run_task(
        self,
        *,
        task_id: str,
        goal: str,
        constraints: Mapping[str, Any] | None = None,
        approvals: Sequence[str] = (),
        events: EventLog | None = None,
    ) -> TaskResult:
        """Interpret, validate, schedule, execute, and verify one goal."""
        constraints = dict(constraints or {})
        request = {"task_id": task_id, "goal": goal, "constraints": constraints}
        log = events or EventLog(task_id=task_id)
        log.task_id = task_id
        result = TaskResult(task_id=task_id, goal=goal, events=log)
        world_before = self.world.snapshot()

        schema_errors = self.schemas.validation_errors("task_request", request)
        if schema_errors:
            result.rejection_code = "VALIDATION_FAILED"
            result.rejection_detail = "; ".join(str(error) for error in schema_errors)
            log.record(
                "TASK_REJECTED",
                error_code="VALIDATION_FAILED",
                detail=result.rejection_detail,
            )
            self._record(result, world_before, constraints)
            return result

        dry_run = bool(constraints.get("dry_run", self.dry_run))
        log.record("TASK_ACCEPTED", detail=goal)

        try:
            interpretation = await self.interpreter.interpret(
                task_id=task_id,
                goal=goal,
                world=self.world,
                constraints=constraints,
                approvals=list(approvals) + sorted(self.safety.approvals),
            )
        except LlmError as error:
            result.rejection_code = "VALIDATION_FAILED"
            result.rejection_detail = f"task interpretation failed: {error}"
            log.record(
                "GRAPH_REJECTED", error_code="VALIDATION_FAILED", detail=result.rejection_detail
            )
            self._record(result, world_before, constraints)
            return result

        result.interpretation = interpretation
        log.record(
            "GRAPH_GENERATED",
            detail=(
                f"{len(interpretation.attempts)} attempt(s) using "
                f"{self.llm_settings.profile_name}"
            ),
        )

        if not interpretation.ok or interpretation.graph is None:
            result.rejection_code = "VALIDATION_FAILED"
            result.rejection_detail = (
                "the generated task graph did not pass validation after "
                f"{len(interpretation.attempts)} attempt(s): "
                + interpretation.report.summary()
            )
            log.record(
                "GRAPH_REJECTED", error_code="VALIDATION_FAILED", detail=result.rejection_detail
            )
            self._record(result, world_before, constraints)
            return result

        graph = interpretation.graph
        log.record(
            "GRAPH_VALIDATED",
            detail=f"{len(graph)} nodes, goal {graph.goal_predicate}",
            # Carrying the graph on the event means a live observer can draw the
            # plan from the same stream everything else reads, instead of needing
            # a second channel that could disagree with it.
            observations={"task_graph": graph.to_dict()},
        )

        plan = self.scheduler.plan(graph, self.world, constraints=constraints)
        result.plan = plan
        if not plan.feasible:
            detail = "; ".join(
                f"{node_id}: {plan.assignments[node_id].rationale}"
                for node_id in plan.infeasible_nodes
            )
            result.rejection_code = "NO_CAPABLE_ROBOT"
            result.rejection_detail = f"no robot can execute every node. {detail}"
            log.record(
                "TASK_REJECTED", error_code="NO_CAPABLE_ROBOT", detail=result.rejection_detail
            )
            self._record(result, world_before, constraints)
            return result

        _log_plan(log, plan)

        manager = ExecutionManager(
            library=self.library,
            registry=self.registry,
            backend=self.backend,
            scheduler=self.scheduler,
            safety=self.safety,
            recovery=self.recovery,
            dry_run=dry_run,
            max_attempts=self.recovery.policies.max_attempts,
            max_recovery_actions=self.recovery.policies.max_recovery_actions_per_task,
        )
        result.outcome = await manager.execute(
            graph, plan, self.world, events=log, constraints=constraints
        )
        self._record(result, world_before, constraints)
        return result

    async def plan_task(
        self,
        *,
        task_id: str,
        goal: str,
        constraints: Mapping[str, Any] | None = None,
        approvals: Sequence[str] = (),
        events: EventLog | None = None,
    ) -> TaskResult:
        """Interpret, validate, and schedule without executing anything.

        Planning narrates itself through the same event vocabulary as execution,
        so an observer watching a dry plan and an observer watching a live task
        are reading one format rather than two.
        """
        constraints = dict(constraints or {})
        log = events or EventLog(task_id=task_id)
        log.task_id = task_id
        result = TaskResult(task_id=task_id, goal=goal, events=log)
        log.record("TASK_ACCEPTED", detail=goal)

        try:
            interpretation = await self.interpreter.interpret(
                task_id=task_id,
                goal=goal,
                world=self.world,
                constraints=constraints,
                approvals=list(approvals) + sorted(self.safety.approvals),
            )
        except LlmError as error:
            result.rejection_code = "VALIDATION_FAILED"
            result.rejection_detail = f"task interpretation failed: {error}"
            log.record(
                "GRAPH_REJECTED",
                error_code="VALIDATION_FAILED",
                detail=result.rejection_detail,
            )
            return result

        result.interpretation = interpretation
        log.record(
            "GRAPH_GENERATED",
            detail=(
                f"{len(interpretation.attempts)} attempt(s) using "
                f"{self.llm_settings.profile_name}"
            ),
        )
        if not interpretation.ok or interpretation.graph is None:
            result.rejection_code = "VALIDATION_FAILED"
            result.rejection_detail = interpretation.report.summary()
            log.record(
                "GRAPH_REJECTED",
                error_code="VALIDATION_FAILED",
                detail=result.rejection_detail,
            )
            return result

        graph = interpretation.graph
        log.record(
            "GRAPH_VALIDATED",
            detail=f"{len(graph)} nodes, goal {graph.goal_predicate}",
            observations={"task_graph": graph.to_dict()},
        )

        result.plan = self.scheduler.plan(graph, self.world, constraints=constraints)
        if not result.plan.feasible:
            result.rejection_code = "NO_CAPABLE_ROBOT"
            result.rejection_detail = "; ".join(
                f"{node_id}: {result.plan.assignments[node_id].rationale}"
                for node_id in result.plan.infeasible_nodes
            )
            log.record(
                "TASK_REJECTED",
                error_code="NO_CAPABLE_ROBOT",
                detail=result.rejection_detail,
            )
            return result

        _log_plan(log, result.plan)
        return result

    def validate_graph(
        self,
        graph: TaskGraph,
        *,
        constraints: Mapping[str, Any] | None = None,
        approvals: Sequence[str] = (),
    ):
        """Validate an externally supplied graph, bypassing the model."""
        return self.validator.validate(
            graph,
            self.world,
            constraints=constraints,
            approvals=list(approvals) + sorted(self.safety.approvals),
        )

    # ------------------------------------------------------------------
    def _record(
        self,
        result: TaskResult,
        world_before: Mapping[str, Any],
        constraints: Mapping[str, Any],
    ) -> None:
        if self.recorder is None:
            return
        self.recorder.record(
            task_id=result.task_id,
            goal=result.goal,
            constraints=constraints,
            world_before=world_before,
            interpretation=result.interpretation.to_dict() if result.interpretation else None,
            plan=result.plan.to_dict() if result.plan else None,
            outcome=result.outcome.to_dict() if result.outcome else None,
            llm_profile=self.llm_settings.profile_name,
            backend=self.backend.name,
        )


def _log_plan(log: EventLog, plan: ExecutionPlan) -> None:
    """Record the scheduler's choices, in prose and in structured form."""
    log.record(
        "PLAN_ASSIGNED",
        detail="; ".join(
            f"{node_id}->{assignment.assigned_robot_id}"
            for node_id, assignment in plan.assignments.items()
        ),
        # The node-to-robot map only. Candidate costs and rationales stay in the
        # plan itself, which an operator fetches when they want to ask why.
        observations={
            "assignments": {
                node_id: assignment.assigned_robot_id
                for node_id, assignment in plan.assignments.items()
            },
            "layers": [list(layer) for layer in plan.layers],
        },
    )


def derive_task_id(goal: str, explicit: str | None = None) -> str:
    """Derive a readable but unique task id from a goal.

    Uniqueness matters: Fleet Control treats a repeated ``externalReference`` as
    the same work order and returns the original result, so reusing a task id
    across runs would silently replay the previous execution instead of doing the
    work. The suffix is random rather than a timestamp, because two runs started
    in the same second are exactly the case a demo or a script produces.
    """
    if explicit:
        return str(explicit)
    words = [word for word in "".join(c if c.isalnum() else " " for c in goal).split()][:4]
    slug = "-".join(word.lower() for word in words) or "task"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def run_task_sync(orchestrator: Orchestrator, **kwargs: Any) -> TaskResult:
    """Blocking wrapper for callers that are not already in an event loop."""
    return asyncio.run(orchestrator.run_task(**kwargs))
