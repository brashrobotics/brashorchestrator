"""Execution control: concurrency, timeouts, deadlines, and cancellation.

These use a scripted in-process backend rather than the fleet stub. The fleet
stub is the right tool for proving the HTTP contract, and the wrong one for
proving that a node hanging for a minute is abandoned after five seconds: that
needs a backend whose timing the test dictates exactly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

import pytest

from brash_orchestrator_core.action_library import ActionLibrary
from brash_orchestrator_core.backends.base import (
    BackendHealth,
    ExecutionBackend,
    PrimitiveRequest,
    PrimitiveResult,
)
from brash_orchestrator_core.execution_manager import ExecutionManager
from brash_orchestrator_core.task_graph import TaskGraph


@dataclass
class ScriptedBackend(ExecutionBackend):
    """A backend whose timing and outcomes the test controls."""

    name: str = "scripted"
    delays: dict[str, float] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    executed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    #: Node ids currently mid-execution, so overlap can be observed rather than
    #: inferred from wall-clock timings that flake on a loaded machine.
    in_flight: set[str] = field(default_factory=set)
    peak_in_flight: int = 0

    async def health(self) -> BackendHealth:
        return BackendHealth(
            reachable=True, supported_kinds=frozenset({"transport", "manipulation", "perception"})
        )

    async def execute(self, request: PrimitiveRequest) -> PrimitiveResult:
        self.executed.append(request.node_id)
        self.in_flight.add(request.node_id)
        self.peak_in_flight = max(self.peak_in_flight, len(self.in_flight))
        try:
            await asyncio.sleep(self.delays.get(request.node_id, 0.0))
        finally:
            self.in_flight.discard(request.node_id)

        error_code = self.failures.get(request.node_id)
        if error_code:
            return PrimitiveResult.failed(error_code, detail=f"scripted {error_code}")
        return PrimitiveResult.succeeded(
            observed_effects={"verified_predicates": [request.verification]}
            if request.verification
            else {},
            metrics={"duration_s": self.delays.get(request.node_id, 0.0)},
            external_reference=request.external_reference,
        )

    async def cancel(self, request: PrimitiveRequest) -> None:
        self.cancelled.append(request.node_id)


@pytest.fixture
def backend() -> ScriptedBackend:
    return ScriptedBackend()


@pytest.fixture
def manager(library, registry, scheduler, backend: ScriptedBackend) -> ExecutionManager:
    return ExecutionManager(
        library=library,
        registry=registry,
        backend=backend,
        scheduler=scheduler,
        max_attempts=1,
    )


@pytest.fixture
def plan(scheduler, drill_graph: TaskGraph, world):
    return scheduler.plan(drill_graph, world)


async def _run(manager, graph, plan, world, **kwargs):
    return await manager.execute(graph, plan, world, **kwargs)


# -- concurrency -----------------------------------------------------------
async def test_independent_nodes_really_run_at_the_same_time(
    manager: ExecutionManager, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    backend.delays = {"n1": 0.05, "n3": 0.05}
    await _run(manager, drill_graph, plan, world)

    assert backend.peak_in_flight >= 2


async def test_a_dependent_node_never_starts_early(
    manager: ExecutionManager, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    await _run(manager, drill_graph, plan, world)

    for node in drill_graph.nodes:
        for dependency in node.depends_on:
            assert backend.executed.index(dependency) < backend.executed.index(node.id)


async def test_a_failed_node_stops_everything_downstream(
    manager: ExecutionManager, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    backend.failures = {"n2": "PATH_BLOCKED"}
    outcome = await _run(manager, drill_graph, plan, world)

    states = {node.node_id: node.state for node in outcome.nodes}
    assert states["n2"] == "FAILED"
    assert states["n4"] == "SKIPPED"
    assert states["n5"] == "SKIPPED"
    # The unrelated branch was allowed to finish.
    assert states["n3"] == "SUCCEEDED"
    assert "n4" not in backend.executed


async def test_a_skipped_node_says_why(
    manager: ExecutionManager, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    backend.failures = {"n2": "PATH_BLOCKED"}
    outcome = await _run(manager, drill_graph, plan, world)

    skipped = next(
        event for event in outcome.events if event["event"] == "NODE_SKIPPED"
    )
    assert "upstream node n2 failed" in skipped["detail"]


# -- timeouts ---------------------------------------------------------------
async def test_a_node_that_hangs_is_abandoned_and_cancelled(
    library, registry, scheduler, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    """Without this the whole task waits forever on one stuck work order."""
    backend.delays = {"n1": 30.0}
    impatient = ActionLibrary(
        actions={
            **library.actions,
            "locate_object": replace(library.get("locate_object"), timeout_s=0.05),
        }
    )
    manager = ExecutionManager(
        library=impatient, registry=registry, backend=backend, scheduler=scheduler, max_attempts=1
    )
    outcome = await _run(manager, drill_graph, plan, world)

    failed = next(node for node in outcome.nodes if node.node_id == "n1")
    assert failed.state == "FAILED"
    assert failed.error_code == "TIMEOUT"
    assert "n1" in backend.cancelled


async def test_a_task_deadline_stops_the_next_wave(
    manager: ExecutionManager, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    backend.delays = {"n1": 0.08, "n3": 0.08}
    outcome = await _run(
        manager, drill_graph, plan, world, constraints={"deadline_s": 0.05}
    )

    assert not outcome.success
    assert outcome.failure_code == "TIMEOUT"
    # The first wave was already in flight; nothing after it was started.
    assert "n4" not in backend.executed


# -- cancellation -----------------------------------------------------------
async def test_cancelling_the_task_reaches_the_backend(
    manager: ExecutionManager, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    backend.delays = {"n1": 5.0, "n3": 5.0}
    task = asyncio.create_task(_run(manager, drill_graph, plan, world))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(backend.cancelled) == {"n1", "n3"}


async def test_nothing_new_is_submitted_after_a_cancellation(
    manager: ExecutionManager, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    backend.delays = {"n1": 5.0, "n3": 5.0}
    task = asyncio.create_task(_run(manager, drill_graph, plan, world))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.05)
    assert set(backend.executed) == {"n1", "n3"}


# -- honest reporting -------------------------------------------------------
async def test_a_backend_that_succeeds_without_observing_does_not_complete_the_task(
    manager: ExecutionManager, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    """Every node green and the goal never seen is a failure, loudly."""

    async def execute(request: PrimitiveRequest) -> PrimitiveResult:
        backend.executed.append(request.node_id)
        return PrimitiveResult.succeeded(external_reference=request.external_reference)

    backend.execute = execute  # type: ignore[method-assign]
    outcome = await _run(manager, drill_graph, plan, world)

    assert all(node.state == "SUCCEEDED" for node in outcome.nodes)
    assert not outcome.success
    assert not outcome.goal_verified
    assert "never verified" in outcome.summary


async def test_an_exploding_backend_is_reported_not_propagated(
    manager: ExecutionManager, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    """A bug in a backend must degrade the task, not crash the orchestrator."""

    async def execute(request: PrimitiveRequest) -> PrimitiveResult:
        raise RuntimeError("backend exploded")

    backend.execute = execute  # type: ignore[method-assign]
    outcome = await _run(manager, drill_graph, plan, world)

    assert not outcome.success
    assert outcome.failure_code == "UNKNOWN_FAILURE"
    assert any("backend exploded" in node.detail for node in outcome.nodes)


async def test_an_infeasible_plan_is_refused_before_anything_is_submitted(
    manager: ExecutionManager, backend: ScriptedBackend, drill_graph, scheduler, world
) -> None:
    world.objects["box_07"].mass_kg = 500.0
    infeasible = scheduler.plan(drill_graph, world)
    assert not infeasible.feasible

    outcome = await _run(manager, drill_graph, infeasible, world)

    assert outcome.failure_code == "NO_CAPABLE_ROBOT"
    assert not backend.executed


# -- dry run ----------------------------------------------------------------
async def test_a_dry_run_reaches_no_backend(
    library, registry, scheduler, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    manager = ExecutionManager(
        library=library,
        registry=registry,
        backend=backend,
        scheduler=scheduler,
        dry_run=True,
    )
    outcome = await _run(manager, drill_graph, plan, world)

    assert outcome.success
    assert not backend.executed


async def test_dry_run_effects_are_recorded_as_claims_not_observations(
    library, registry, scheduler, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    """Otherwise a rehearsal quietly convinces the world model it happened."""
    manager = ExecutionManager(
        library=library,
        registry=registry,
        backend=backend,
        scheduler=scheduler,
        dry_run=True,
    )
    await _run(manager, drill_graph, plan, world)

    assert world.holds("on(drill_01, shelf_B12)")
    assert not world.is_verified("on(drill_01, shelf_B12)")


# -- safety -----------------------------------------------------------------
async def test_the_safety_gate_is_consulted_for_every_submission(
    library, registry, scheduler, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    seen: list[str] = []

    class RecordingGate:
        def evaluate(self, request, _world):
            seen.append(request.node_id)
            return True, "SUCCESS", ""

    manager = ExecutionManager(
        library=library,
        registry=registry,
        backend=backend,
        scheduler=scheduler,
        safety=RecordingGate(),
        max_attempts=1,
    )
    await _run(manager, drill_graph, plan, world)

    assert seen == backend.executed


async def test_a_safety_refusal_prevents_submission(
    library, registry, scheduler, backend: ScriptedBackend, drill_graph, plan, world
) -> None:
    class RefusingGate:
        def evaluate(self, request, _world):
            if request.node_id == "n4":
                return False, "SAFETY_POLICY_VIOLATION", "not on my watch"
            return True, "SUCCESS", ""

    manager = ExecutionManager(
        library=library,
        registry=registry,
        backend=backend,
        scheduler=scheduler,
        safety=RefusingGate(),
        max_attempts=1,
    )
    outcome = await _run(manager, drill_graph, plan, world)

    assert "n4" not in backend.executed
    assert not outcome.success
    assert outcome.failure_code == "SAFETY_POLICY_VIOLATION"
