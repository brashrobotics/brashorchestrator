"""The whole pipeline, from an English sentence to a verified world state.

These run the real `Orchestrator` over the real HTTP client against the fleet
stub. Only the model is substituted, and only because a test suite that needs a
GPU served is a test suite nobody runs. Everything downstream of the model --
validation, scheduling, safety, work-order construction, polling, reconciliation
-- is the path production takes.
"""

from __future__ import annotations

import pytest

from brash_orchestrator_core.orchestrator import Orchestrator, TaskResult


@pytest.fixture(autouse=True)
async def _close(orchestrator: Orchestrator):
    yield
    await orchestrator.close()


@pytest.fixture
async def drill(orchestrator: Orchestrator, drill_goal: str) -> TaskResult:
    """One completed run of the drill task, for the many assertions about it."""
    return await orchestrator.run_task(task_id="t-drill", goal=drill_goal)


def _stages(result: TaskResult) -> list[str]:
    return [event.event for event in result.events.events]


def _placements(world) -> set[str]:
    return {
        fact["predicate"]
        for fact in world.snapshot()["facts"]
        if fact["predicate"].startswith(("at(", "inside("))
    }


# -- the happy path --------------------------------------------------------
def test_the_drill_task_runs_from_english_to_a_verified_shelf(drill: TaskResult) -> None:
    assert drill.success
    assert drill.failure_code == "SUCCESS"
    assert drill.outcome.goal_predicate == "verified_at(drill_01, shelf_B12)"
    assert drill.outcome.goal_verified
    assert all(node.state == "SUCCEEDED" for node in drill.outcome.nodes)


def test_the_world_ends_where_the_goal_said_it_should(
    drill: TaskResult, orchestrator: Orchestrator
) -> None:
    """The point of the whole exercise: the drill is on the shelf, and observed."""
    world = orchestrator.world
    assert world.holds("on(drill_01, shelf_B12)")
    assert world.is_verified("on(drill_01, shelf_B12)")
    # It is no longer in the box, and containment agrees with the predicate.
    assert not world.holds("inside(drill_01, box_07)")
    assert world.resolve_location("drill_01") == "shelf_B12"


def test_the_box_ends_open_at_the_handover_station(
    drill: TaskResult, orchestrator: Orchestrator
) -> None:
    assert orchestrator.world.holds("at(box_07, handover_station_02)")
    assert orchestrator.world.holds("state(box_07, open)")


def test_the_narrative_covers_every_stage(drill: TaskResult) -> None:
    """One event stream has to be enough to reconstruct what happened."""
    stages = _stages(drill)
    assert stages[0] == "TASK_ACCEPTED"
    assert stages[-1] == "TASK_SUCCEEDED"
    for stage in ("GRAPH_GENERATED", "GRAPH_VALIDATED", "PLAN_ASSIGNED"):
        assert stage in stages
    assert stages.count("NODE_STARTED") == len(drill.outcome.nodes)
    assert stages.count("NODE_SUCCEEDED") == len(drill.outcome.nodes)


def test_every_event_conforms_to_the_published_contract(
    drill: TaskResult, orchestrator: Orchestrator
) -> None:
    for event in drill.events.to_list():
        assert orchestrator.schemas.validation_errors("execution_event", event) == []


def test_the_result_conforms_to_the_published_contract(
    drill: TaskResult, orchestrator: Orchestrator
) -> None:
    errors = orchestrator.schemas.validation_errors(
        "execution_result", drill.outcome.to_dict()
    )
    assert errors == []


# -- ordering and concurrency ----------------------------------------------
def test_dependencies_are_respected(drill: TaskResult) -> None:
    order = [
        event.node_id for event in drill.events.events if event.event == "NODE_SUCCEEDED"
    ]
    for node in drill.interpretation.graph.nodes:
        for dependency in node.depends_on:
            assert order.index(dependency) < order.index(node.id)


def test_independent_nodes_overlap(drill: TaskResult) -> None:
    """`n3 inspect_target_shelf` has no ordering relation to the transport chain.

    Serializing it behind a multi-minute carry would cost real time for no
    reason, so both no-dependency nodes have to start in the first wave.
    """
    started = [
        event.node_id for event in drill.events.events if event.event == "NODE_STARTED"
    ]
    assert set(started[:2]) == {"n1", "n3"}


# -- work reaching the fleet ------------------------------------------------
def test_every_node_became_a_fleet_work_order(drill: TaskResult, fleet_stub) -> None:
    """No node may be quietly satisfied inside the orchestrator."""
    assert len(fleet_stub.stub.work_orders) == len(drill.outcome.nodes)
    references = {order.external_reference for order in fleet_stub.stub.work_orders.values()}
    assert all(reference.startswith("brash-task:t-drill:") for reference in references)


def test_manipulation_nodes_travel_on_the_extension(drill: TaskResult, fleet_stub) -> None:
    kinds = {
        submission.get("kind", "TRANSPORT") for submission in fleet_stub.stub.submissions
    }
    assert kinds == {"TRANSPORT", "MANIPULATION"}


def test_the_scheduler_splits_the_work_by_capability(drill: TaskResult) -> None:
    """The quadruped carries, the humanoid manipulates, and nobody said so."""
    by_action = {node.action: node.assigned_robot_id for node in drill.outcome.nodes}
    assert by_action["transport_container"] == "b2w_z1_01"
    assert by_action["open_container"] == "g1_brainco_01"
    assert by_action["pick_tool_from_container"] == "g1_brainco_01"


# -- dry run ----------------------------------------------------------------
async def test_a_dry_run_submits_nothing(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    result = await orchestrator.run_task(
        task_id="t-dry", goal=drill_goal, constraints={"dry_run": True}
    )
    assert result.success
    assert not fleet_stub.stub.work_orders


async def test_a_dry_run_never_claims_verification(
    orchestrator: Orchestrator, drill_goal: str
) -> None:
    """A plan that was not executed cannot have observed anything."""
    result = await orchestrator.run_task(
        task_id="t-dry", goal=drill_goal, constraints={"dry_run": True}
    )
    assert result.outcome.dry_run
    assert not result.outcome.goal_verified
    assert "not verified" in result.summary
    assert not orchestrator.world.is_verified("at(drill_01, shelf_B12)")


# -- planning without executing ---------------------------------------------
async def test_planning_alone_touches_neither_fleet_nor_world(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    before = orchestrator.world.snapshot()
    result = await orchestrator.plan_task(task_id="t-plan", goal=drill_goal)

    assert result.plan is not None and result.plan.feasible
    assert not fleet_stub.stub.work_orders
    assert orchestrator.world.snapshot() == before


async def test_a_plan_names_a_robot_for_every_node(
    orchestrator: Orchestrator, drill_goal: str
) -> None:
    result = await orchestrator.plan_task(task_id="t-plan", goal=drill_goal)
    for node in result.interpretation.graph.nodes:
        assert result.plan.robot_for(node.id)


# -- other goals ------------------------------------------------------------
async def test_a_transport_only_goal_needs_no_manipulator(
    orchestrator: Orchestrator, move_goal: str, fleet_stub
) -> None:
    """Moving a box needs perception and a carry, and no hands."""
    result = await orchestrator.run_task(task_id="t-move", goal=move_goal)

    assert result.success
    carry = next(node for node in result.outcome.nodes if node.action == "transport_container")
    assert carry.assigned_robot_id == "b2w_z1_01"
    assert all(
        (submission.get("manipulation") or {}).get("readOnly", True)
        for submission in fleet_stub.stub.submissions
    )


async def test_a_read_only_goal_moves_nothing(
    orchestrator: Orchestrator, inspect_goal: str
) -> None:
    """Inspection records what it saw and relocates nothing."""
    before = _placements(orchestrator.world)
    result = await orchestrator.run_task(task_id="t-inspect", goal=inspect_goal)

    assert result.success
    assert orchestrator.world.holds("inspected(shelf_B12)")
    assert _placements(orchestrator.world) == before


# -- rejection paths --------------------------------------------------------
async def test_a_goal_the_model_cannot_ground_is_rejected_not_guessed(
    orchestrator: Orchestrator, fleet_stub
) -> None:
    result = await orchestrator.run_task(
        task_id="t-nonsense", goal="Reticulate the splines in the east annex."
    )

    assert not result.success
    assert result.failure_code == "VALIDATION_FAILED"
    assert not fleet_stub.stub.work_orders
    assert result.summary != "task produced no outcome"


async def test_a_malformed_request_is_rejected_before_the_model_is_called(
    orchestrator: Orchestrator,
) -> None:
    result = await orchestrator.run_task(task_id="", goal="")

    assert result.failure_code == "VALIDATION_FAILED"
    assert _stages(result) == ["TASK_REJECTED"]


# -- safety -----------------------------------------------------------------
async def test_a_human_in_the_cell_stops_the_task_at_the_gate(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    """The safety manager holds the veto, and no plan can talk it round."""
    orchestrator.world.robots["g1_brainco_01"].human_distance_m = 0.4

    result = await orchestrator.run_task(task_id="t-drill", goal=drill_goal)

    assert not result.success
    assert result.failure_code == "HUMAN_IN_SAFETY_ZONE"
    assert "SAFETY_BLOCKED" in _stages(result)
    submitted = {
        (submission.get("manipulation") or {}).get("primitive")
        for submission in fleet_stub.stub.submissions
    }
    assert "open_container" not in submitted


async def test_a_safety_block_is_not_retried_into_submission(
    orchestrator: Orchestrator, drill_goal: str
) -> None:
    orchestrator.world.robots["g1_brainco_01"].human_distance_m = 0.4

    result = await orchestrator.run_task(task_id="t-drill", goal=drill_goal)

    blocked = [
        node for node in result.outcome.nodes if node.error_code == "HUMAN_IN_SAFETY_ZONE"
    ]
    assert blocked and all(node.attempts == 1 for node in blocked)


# -- state carried between tasks --------------------------------------------
async def test_a_second_task_starts_from_where_the_first_one_finished(
    orchestrator: Orchestrator, move_goal: str, drill_goal: str
) -> None:
    """The world model is the orchestrator's memory, so this has to hold."""
    await orchestrator.run_task(task_id="t-move", goal=move_goal)
    assert orchestrator.world.holds("at(box_07, handover_station_02)")

    result = await orchestrator.run_task(task_id="t-drill", goal=drill_goal)
    assert result.success
