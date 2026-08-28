"""Cost-based assignment.

The scheduler's job is to produce the same plan for the same inputs and to
explain itself. Both are asserted here, along with the physical limits that are
treated as hard constraints rather than expensive options.
"""

from __future__ import annotations

import copy

import pytest

from brash_orchestrator_core.capability_registry import CapabilityRegistry
from brash_orchestrator_core.scheduler import (
    Scheduler,
    SchedulerConfig,
    SchedulerError,
    describe_plan,
)
from brash_orchestrator_core.task_graph import TaskGraph, TaskNode
from brash_world_model.model import WorldModel


@pytest.fixture
def graph(drill_graph: TaskGraph) -> TaskGraph:
    return copy.deepcopy(drill_graph)


def test_the_plan_covers_every_node(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    plan = scheduler.plan(graph, world)
    assert set(plan.assignments) == set(graph.node_ids)
    assert plan.feasible


def test_work_is_split_along_capability_lines(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    """The point of a heterogeneous fleet: each body does what only it can."""
    plan = scheduler.plan(graph, world)
    by_action = {
        plan.assignments[node.id].action: plan.robot_for(node.id) for node in graph.nodes
    }
    assert by_action["transport_container"] == "b2w_z1_01"
    assert by_action["open_container"] == "g1_brainco_01"
    assert by_action["pick_tool_from_container"] == "g1_brainco_01"


def test_the_same_inputs_always_produce_the_same_plan(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    first = scheduler.plan(graph, world.copy())
    second = scheduler.plan(copy.deepcopy(graph), world.copy())
    assert {node: first.robot_for(node) for node in first.assignments} == {
        node: second.robot_for(node) for node in second.assignments
    }


def test_every_assignment_explains_itself(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    """"Why that robot" is the first question an operator asks."""
    plan = scheduler.plan(graph, world)
    for assignment in plan.assignments.values():
        assert assignment.rationale
        assert assignment.candidates, "losing candidates must be recorded too"


def test_losing_candidates_record_why_they_lost(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    plan = scheduler.plan(graph, world)
    opener = next(a for a in plan.assignments.values() if a.action == "open_container")
    rejected = [c for c in opener.candidates if not c.eligible]
    assert rejected
    assert all(candidate.reasons for candidate in rejected)
    assert any("missing capability" in reason for c in rejected for reason in c.reasons)


def test_an_interpreter_preference_is_overridden_and_the_override_is_stated(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    node = next(n for n in graph.nodes if n.action == "open_container")
    node.proposed_robot_id = "b2w_z1_01"
    plan = scheduler.plan(graph, world)

    assert plan.robot_for(node.id) == "g1_brainco_01"
    assert "overrode" in plan.assignments[node.id].rationale


# -- hard physical limits -------------------------------------------------
def test_payload_is_a_hard_limit_not_a_cost(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    world.objects["box_07"].mass_kg = 400.0
    plan = scheduler.plan(graph, world)

    transport = next(n for n in graph.nodes if n.action == "transport_container")
    assert plan.robot_for(transport.id) is None
    assert not plan.feasible
    assert transport.id in plan.infeasible_nodes
    assert "payload" in plan.assignments[transport.id].rationale


def test_shelf_height_beyond_reach_makes_a_robot_ineligible(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    world.locations["shelf_B12"].height_m = 4.0
    plan = scheduler.plan(graph, world)

    place = next(n for n in graph.nodes if n.action == "place_object_on_shelf")
    assert plan.robot_for(place.id) is None
    assert "reach" in plan.assignments[place.id].rationale


def test_a_flat_battery_makes_a_robot_ineligible(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    world.robots["b2w_z1_01"].battery_percent = 5.0
    plan = scheduler.plan(graph, world)

    transport = next(n for n in graph.nodes if n.action == "transport_container")
    assert plan.robot_for(transport.id) is None
    assert "battery" in plan.assignments[transport.id].rationale


def test_an_offline_robot_is_ineligible(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    world.robots["g1_brainco_01"].connection = "offline"
    plan = scheduler.plan(graph, world)
    opener = next(n for n in graph.nodes if n.action == "open_container")
    assert plan.robot_for(opener.id) is None


def test_a_reported_fault_makes_a_robot_ineligible(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    world.robots["g1_brainco_01"].fault_code = "E-STOP"
    plan = scheduler.plan(graph, world)
    opener = next(n for n in graph.nodes if n.action == "open_container")
    assert "E-STOP" in plan.assignments[opener.id].rationale


def test_a_blocked_route_makes_the_destination_unreachable(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    world.blocked_locations.add("shelf_B12")
    plan = scheduler.plan(graph, world)
    place = next(n for n in graph.nodes if n.action == "place_object_on_shelf")
    assert "no route" in plan.assignments[place.id].rationale


# -- constraints ----------------------------------------------------------
def test_allowed_robot_ids_excludes_everyone_else(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    plan = scheduler.plan(graph, world, constraints={"allowed_robot_ids": ["b2w_z1_01"]})
    opener = next(n for n in graph.nodes if n.action == "open_container")
    assert plan.robot_for(opener.id) is None
    assert "allowed_robot_ids" in plan.assignments[opener.id].rationale


def test_a_low_risk_constraint_rejects_risky_actions(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    plan = scheduler.plan(graph, world, constraints={"max_risk": "low"})
    pick = next(n for n in graph.nodes if n.action == "pick_tool_from_container")
    assert plan.robot_for(pick.id) is None
    assert "risk" in plan.assignments[pick.id].rationale


# -- the cost function ----------------------------------------------------
def _one_node_registry(success_prior: float) -> CapabilityRegistry:
    return CapabilityRegistry.from_mapping(
        {
            "robots": {
                "r1": {
                    "type": "humanoid",
                    "capabilities": {
                        "open_container": {"enabled": True, "success_prior": success_prior}
                    },
                }
            }
        }
    )


def test_cost_falls_when_predicted_success_rises(
    library, world: WorldModel
) -> None:
    """The cost function must reward the body more likely to actually succeed."""
    single = TaskGraph(
        task_id="t",
        goal_predicate="state(box_07, open)",
        nodes=[
            TaskNode(
                id="solo",
                action="open_container",
                parameters={"container_id": "box_07"},
                required_capabilities=("open_container",),
            )
        ],
    )
    config = SchedulerConfig.load()

    def cost_for(prior: float) -> float:
        plan = Scheduler(library, _one_node_registry(prior), config).plan(single, world)
        return plan.assignments["solo"].candidates[0].cost

    assert cost_for(0.95) < cost_for(0.55)


def test_a_longer_trip_costs_more(
    scheduler: Scheduler, world: WorldModel
) -> None:
    near = TaskGraph(
        task_id="t",
        goal_predicate="inspected(handover_station_02)",
        nodes=[
            TaskNode(
                id="a",
                action="inspect_target_shelf",
                parameters={"shelf_id": "handover_station_02"},
                required_capabilities=("verify_object_pose",),
            )
        ],
    )
    far = TaskGraph(
        task_id="t",
        goal_predicate="inspected(shelf_B12)",
        nodes=[
            TaskNode(
                id="a",
                action="inspect_target_shelf",
                parameters={"shelf_id": "shelf_B12"},
                required_capabilities=("verify_object_pose",),
            )
        ],
    )
    near_cost = scheduler.plan(near, world).assignments["a"].candidates[0].cost
    far_cost = scheduler.plan(far, world).assignments["a"].candidates[0].cost
    assert far_cost > near_cost


def test_the_plan_projects_robot_movement_between_nodes(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    """A robot already at the shelf should not be charged to travel there again."""
    plan = scheduler.plan(graph, world)
    place = next(n for n in graph.nodes if n.action == "place_object_on_shelf")
    verify = next(n for n in graph.nodes if n.action == "verify_object_pose")

    place_distance = next(
        c for c in plan.assignments[place.id].candidates if c.robot_id == "g1_brainco_01"
    ).reposition_distance_m
    verify_distance = next(
        c for c in plan.assignments[verify.id].candidates if c.robot_id == "g1_brainco_01"
    ).reposition_distance_m
    assert verify_distance < place_distance
    assert verify_distance == pytest.approx(0.0)


def test_an_unknown_action_yields_an_infeasible_assignment(
    scheduler: Scheduler, world: WorldModel
) -> None:
    graph = TaskGraph(
        task_id="t",
        goal_predicate="on(drill_01, shelf_B12)",
        nodes=[TaskNode(id="a", action="levitate")],
    )
    plan = scheduler.plan(graph, world)
    assert not plan.feasible
    assert "Action Library" in plan.assignments["a"].rationale


def test_describe_plan_shows_the_parallel_wave(
    scheduler: Scheduler, graph: TaskGraph, world: WorldModel
) -> None:
    text = describe_plan(scheduler.plan(graph, world))
    assert "(parallel)" in text
    assert "b2w_z1_01" in text and "g1_brainco_01" in text


def test_a_zero_duration_normalizer_is_rejected(tmp_path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text("normalizers:\n  duration_normalizer_s: 0\n", encoding="utf-8")
    with pytest.raises(SchedulerError, match="duration_normalizer_s"):
        SchedulerConfig.load(path)
