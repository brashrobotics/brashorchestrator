"""Reconciling declared effects with observed ones.

The distinction this module enforces is the difference between "the robot said it
worked" and "something measured that it worked". Every test below is about
keeping those apart.
"""

from __future__ import annotations

from brash_orchestrator_core.action_library import ActionLibrary
from brash_orchestrator_core.backends.base import PrimitiveResult
from brash_orchestrator_core.execution_monitor import (
    ExecutionMonitor,
    occupancy_update_for,
)
from brash_orchestrator_core.task_graph import TaskNode
from brash_world_model.model import WorldModel

ROBOT = "g1_brainco_01"


def _open_box() -> TaskNode:
    return TaskNode(
        id="n4",
        action="open_container",
        parameters={"container_id": "box_07"},
        required_capabilities=("open_container",),
    )


def _place_drill() -> TaskNode:
    return TaskNode(
        id="n6",
        action="place_object_on_shelf",
        parameters={"object_id": "drill_01", "shelf_id": "shelf_B12"},
        required_capabilities=("place_object_on_shelf",),
    )


# -- success without observation ------------------------------------------
def test_a_bare_success_leaves_every_effect_claimed(
    world: WorldModel, library: ActionLibrary
) -> None:
    monitor = ExecutionMonitor(world)
    reconciliation = monitor.on_success(
        _open_box(), library["open_container"], ROBOT, PrimitiveResult.succeeded()
    )

    assert reconciliation.claimed
    assert not reconciliation.verified
    assert not reconciliation.fully_verified
    # The belief is recorded so planning can continue, but not as evidence.
    assert world.holds("state(box_07, open)")
    assert not world.is_verified("state(box_07, open)")


def test_a_confirmed_predicate_is_stored_as_verified(
    world: WorldModel, library: ActionLibrary
) -> None:
    monitor = ExecutionMonitor(world)
    result = PrimitiveResult.succeeded(
        observed_effects={
            "confidence": 0.95,
            "verified_predicates": ["state(box_07, open)"],
        }
    )
    reconciliation = monitor.on_success(_open_box(), library["open_container"], ROBOT, result)

    assert "state(box_07, open)" in reconciliation.verified
    assert world.is_verified("state(box_07, open)")


def test_a_low_confidence_observation_is_not_treated_as_evidence(
    world: WorldModel, library: ActionLibrary
) -> None:
    monitor = ExecutionMonitor(world, min_confidence=0.6)
    result = PrimitiveResult.succeeded(
        observed_effects={"confidence": 0.2, "verified_predicates": ["state(box_07, open)"]}
    )
    reconciliation = monitor.on_success(_open_box(), library["open_container"], ROBOT, result)

    assert "state(box_07, open)" in reconciliation.claimed
    assert not world.is_verified("state(box_07, open)")


def test_only_the_confirmed_effects_become_verified(
    world: WorldModel, library: ActionLibrary
) -> None:
    """Opening a box confirms the lid; it does not confirm you can see inside."""
    monitor = ExecutionMonitor(world)
    result = PrimitiveResult.succeeded(
        observed_effects={"confidence": 0.9, "verified_predicates": ["state(box_07, open)"]}
    )
    reconciliation = monitor.on_success(_open_box(), library["open_container"], ROBOT, result)

    assert reconciliation.verified == ["state(box_07, open)"]
    assert "contents_visible(box_07)" in reconciliation.claimed


def test_an_alternate_observation_key_is_accepted(
    world: WorldModel, library: ActionLibrary
) -> None:
    monitor = ExecutionMonitor(world)
    result = PrimitiveResult.succeeded(
        observed_effects={"confidence": 0.9, "observed_predicates": ["state(box_07, open)"]}
    )
    reconciliation = monitor.on_success(_open_box(), library["open_container"], ROBOT, result)
    assert reconciliation.verified == ["state(box_07, open)"]


def test_a_malformed_observed_predicate_is_ignored_not_fatal(
    world: WorldModel, library: ActionLibrary
) -> None:
    monitor = ExecutionMonitor(world)
    result = PrimitiveResult.succeeded(
        observed_effects={"confidence": 0.9, "verified_predicates": ["the box is open"]}
    )
    reconciliation = monitor.on_success(_open_box(), library["open_container"], ROBOT, result)
    assert reconciliation.claimed


def test_retractions_need_no_positive_observation(
    world: WorldModel, library: ActionLibrary
) -> None:
    """The absence of a thing is established by the action that removed it."""
    world.assert_all(["held_by(drill_01, g1_brainco_01)"])
    monitor = ExecutionMonitor(world)
    result = PrimitiveResult.succeeded(
        observed_effects={
            "confidence": 0.9,
            "verified_predicates": ["on(drill_01, shelf_B12)"],
        }
    )
    reconciliation = monitor.on_success(
        _place_drill(), library["place_object_on_shelf"], ROBOT, result
    )

    assert "not_held(drill_01)" in reconciliation.retracted
    assert not world.holds("held_by(drill_01, g1_brainco_01)")


def test_the_assigned_robot_is_bound_into_effects(
    world: WorldModel, library: ActionLibrary
) -> None:
    node = TaskNode(
        id="n5",
        action="pick_tool_from_container",
        parameters={"object_id": "drill_01", "container_id": "box_07"},
    )
    monitor = ExecutionMonitor(world)
    result = PrimitiveResult.succeeded(
        observed_effects={
            "confidence": 0.9,
            "verified_predicates": [f"held_by(drill_01, {ROBOT})"],
        }
    )
    reconciliation = monitor.on_success(
        node, library["pick_tool_from_container"], ROBOT, result
    )
    assert f"held_by(drill_01, {ROBOT})" in reconciliation.verified


# -- failure retracts contradicted beliefs ---------------------------------
def test_a_lid_failure_retracts_the_lid_accessible_belief(
    world: WorldModel, library: ActionLibrary
) -> None:
    """Otherwise the planner cheerfully retries the same impossible action."""
    world.assert_all(["lid_accessible(box_07)"])
    monitor = ExecutionMonitor(world)
    reconciliation = monitor.on_failure(
        _open_box(),
        library["open_container"],
        ROBOT,
        PrimitiveResult.failed("LID_NOT_ACCESSIBLE"),
    )

    assert "lid_accessible(box_07)" in reconciliation.contradicted
    assert reconciliation.has_mismatch
    assert not world.holds("lid_accessible(box_07)")


def test_an_uncertain_pose_retracts_what_the_plan_assumed_it_could_see(
    world: WorldModel, library: ActionLibrary
) -> None:
    world.assert_all(["pose_known(drill_01)", "contents_visible(box_07)"])
    node = TaskNode(
        id="n5",
        action="pick_tool_from_container",
        parameters={"object_id": "drill_01", "container_id": "box_07"},
    )
    monitor = ExecutionMonitor(world)
    monitor.on_failure(
        node,
        library["pick_tool_from_container"],
        ROBOT,
        PrimitiveResult.failed("OBJECT_POSE_UNCERTAIN"),
    )

    assert not world.holds("pose_known(drill_01)")
    assert not world.holds("contents_visible(box_07)")


def test_a_slip_retracts_the_grasp(world: WorldModel, library: ActionLibrary) -> None:
    world.assert_all([f"held_by(drill_01, {ROBOT})"])
    node = TaskNode(
        id="n6",
        action="place_object_on_shelf",
        parameters={"object_id": "drill_01", "shelf_id": "shelf_B12"},
    )
    monitor = ExecutionMonitor(world)
    monitor.on_failure(
        node, library["place_object_on_shelf"], ROBOT, PrimitiveResult.failed("SLIP_DETECTED")
    )
    assert not world.holds(f"held_by(drill_01, {ROBOT})")


def test_a_failure_only_retracts_facts_that_were_actually_held(
    world: WorldModel, library: ActionLibrary
) -> None:
    monitor = ExecutionMonitor(world)
    reconciliation = monitor.on_failure(
        _open_box(),
        library["open_container"],
        ROBOT,
        PrimitiveResult.failed("LID_NOT_ACCESSIBLE"),
    )
    assert reconciliation.contradicted == []


def test_an_unrelated_failure_retracts_nothing(
    world: WorldModel, library: ActionLibrary
) -> None:
    world.assert_all(["lid_accessible(box_07)"])
    monitor = ExecutionMonitor(world)
    monitor.on_failure(
        _open_box(), library["open_container"], ROBOT, PrimitiveResult.failed("TIMEOUT")
    )
    assert world.holds("lid_accessible(box_07)")


# -- volunteered observations ---------------------------------------------
def test_attribute_observations_are_applied(
    world: WorldModel, library: ActionLibrary
) -> None:
    monitor = ExecutionMonitor(world)
    result = PrimitiveResult.succeeded(
        observed_effects={"confidence": 0.9, "attributes": {"shelf_B12.free_space": False}}
    )
    monitor.on_success(
        TaskNode(id="n3", action="inspect_target_shelf", parameters={"shelf_id": "shelf_B12"}),
        library["inspect_target_shelf"],
        ROBOT,
        result,
    )
    assert not world.holds("shelf_has_free_space(shelf_B12)")


def test_a_bad_attribute_key_does_not_stop_the_task(
    world: WorldModel, library: ActionLibrary
) -> None:
    monitor = ExecutionMonitor(world)
    result = PrimitiveResult.succeeded(
        observed_effects={"confidence": 0.9, "attributes": {"nonsense": 1}}
    )
    monitor.on_success(_open_box(), library["open_container"], ROBOT, result)


def test_a_backend_can_retract_a_predicate_it_observed_to_be_false(
    world: WorldModel, library: ActionLibrary
) -> None:
    world.assert_all(["lid_accessible(box_07)"])
    monitor = ExecutionMonitor(world)
    result = PrimitiveResult.succeeded(
        observed_effects={
            "confidence": 0.9,
            "retracted_predicates": ["lid_accessible(box_07)"],
        }
    )
    monitor.on_success(_open_box(), library["open_container"], ROBOT, result)
    assert not world.holds("lid_accessible(box_07)")


def test_target_occupied_marks_the_shelf_full() -> None:
    node = TaskNode(id="n6", action="place_object_on_shelf", parameters={"shelf_id": "shelf_B12"})
    assert occupancy_update_for("TARGET_OCCUPIED", node) == {"shelf_B12.free_space": False}


def test_other_failures_imply_no_occupancy_change() -> None:
    node = TaskNode(id="n6", action="place_object_on_shelf", parameters={"shelf_id": "shelf_B12"})
    assert occupancy_update_for("GRASP_FAILED", node) == {}


# -- goal status -----------------------------------------------------------
def test_goal_status_separates_holding_from_being_verified(world: WorldModel) -> None:
    monitor = ExecutionMonitor(world)
    assert monitor.goal_status("verified_at(drill_01, shelf_B12)") == (False, False)

    world.assert_all(["verified_at(drill_01, shelf_B12)"], source="BACKEND_CLAIM")
    assert monitor.goal_status("verified_at(drill_01, shelf_B12)") == (True, False)

    world.assert_all(["verified_at(drill_01, shelf_B12)"], source="OBSERVATION")
    assert monitor.goal_status("verified_at(drill_01, shelf_B12)") == (True, True)


def test_a_malformed_goal_predicate_is_reported_as_unmet(world: WorldModel) -> None:
    monitor = ExecutionMonitor(world)
    assert monitor.goal_status("put the drill away") == (False, False)


def test_every_reconciliation_is_kept_for_the_trial_record(
    world: WorldModel, library: ActionLibrary
) -> None:
    monitor = ExecutionMonitor(world)
    monitor.on_success(_open_box(), library["open_container"], ROBOT, PrimitiveResult.succeeded())
    monitor.on_failure(
        _open_box(), library["open_container"], ROBOT, PrimitiveResult.failed("TIMEOUT")
    )
    assert len(monitor.reconciliations) == 2
    assert all("node_id" in item.to_dict() for item in monitor.reconciliations)
