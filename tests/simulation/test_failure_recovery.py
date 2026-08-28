"""The four failure scenarios from README section 15, run end to end.

Recovery is most of the value of an orchestrator, and it is the part that cannot
be demonstrated by a system that always succeeds. Each test here scripts a
specific failure into the fleet stub and then asserts on what the orchestrator
did about it: which recovery it chose, what reached the fleet afterwards, and
whether the world model ended up in a state anyone should trust.

Two properties are asserted repeatedly on purpose, because they are the ones a
plausible-looking recovery loop quietly violates:

* the recovery only ever uses actions from the Action Library, and
* a task is never reported complete unless the goal was actually observed.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from brash_orchestrator_core.capability_registry import parse_robot_manifest
from brash_orchestrator_core.orchestrator import Orchestrator, TaskResult
from tools.fleet_stub.server import FaultInjection, StubRobot, parse_fault_request


@pytest.fixture(autouse=True)
async def _close(orchestrator: Orchestrator):
    yield
    await orchestrator.close()


def _stages(result: TaskResult) -> list[str]:
    return [event.event for event in result.events.events]


def _events(result: TaskResult, kind: str):
    return [event for event in result.events.events if event.event == kind]


def _node(result: TaskResult, action: str):
    return next(node for node in result.outcome.nodes if node.action == action)


def _primitives(fleet_stub) -> list[str]:
    return [
        (submission.get("manipulation") or {}).get("primitive", "transport")
        for submission in fleet_stub.stub.submissions
    ]


def _add_second_quadruped(orchestrator: Orchestrator, fleet_stub) -> None:
    """Register a second B2-W, the way `RegisterRobot` would at runtime.

    README section 15 example 4 is about a fleet with more than one transporter,
    which the single-robot demo scenario cannot express.
    """
    from brash_world_model.model import RobotRecord

    manifest = orchestrator.registry["b2w_z1_01"]
    orchestrator.registry.register(
        parse_robot_manifest(
            "b2w_z1_02",
            {
                "type": manifest.type,
                "vendor": manifest.vendor,
                "fleet_robot_id": "b2w_z1_02",
                "energy_per_second_wh": manifest.energy_per_second_wh,
                "capabilities": {
                    name: {
                        "enabled": capability.enabled,
                        "success_prior": capability.success_prior,
                        **(
                            {"max_mass_kg": capability.max_mass_kg}
                            if capability.max_mass_kg is not None
                            else {}
                        ),
                        **dict(capability.qualifiers),
                    }
                    for name, capability in manifest.capabilities.items()
                },
            },
        )
    )
    orchestrator.world.robots["b2w_z1_02"] = RobotRecord(
        robot_id="b2w_z1_02", location="charging_area_b2w", battery_percent=88.0
    )
    fleet_stub.stub.robots["b2w_z1_02"] = StubRobot(robot_id="b2w_z1_02")


# ==========================================================================
# Example 1: the box lid is not facing G1
# ==========================================================================
@pytest.fixture
def lid_not_accessible(fleet_stub):
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="open_container",
            error_code="LID_NOT_ACCESSIBLE",
            remaining=1,
            observations={"recommended_yaw_change_deg": 90},
        )
    )
    return fleet_stub


@pytest.fixture
async def lid_recovery(
    orchestrator: Orchestrator, drill_goal: str, lid_not_accessible
) -> TaskResult:
    return await orchestrator.run_task(task_id="t-lid", goal=drill_goal)


def test_an_inaccessible_lid_does_not_end_the_task(lid_recovery: TaskResult) -> None:
    assert lid_recovery.success
    assert lid_recovery.outcome.goal_verified


def test_the_failure_is_recorded_before_anything_is_attempted_about_it(
    lid_recovery: TaskResult,
) -> None:
    """An operator has to be able to see the original failure, not just the fix."""
    failure = _events(lid_recovery, "NODE_FAILED")[0]
    assert failure.error_code == "LID_NOT_ACCESSIBLE"
    assert failure.action == "open_container"

    stages = _stages(lid_recovery)
    assert stages.index("NODE_FAILED") < stages.index("RECOVERY_PROPOSED")
    assert stages.index("RECOVERY_PROPOSED") < stages.index("RECOVERY_APPLIED")


def test_repositioning_is_inserted_before_the_failed_open(
    lid_recovery: TaskResult,
) -> None:
    proposal = _events(lid_recovery, "RECOVERY_PROPOSED")[0]
    assert proposal.observations["strategy"] == "insert_before"
    assert proposal.observations["candidate_id"] == "reposition_then_retry"

    inserted = _node(lid_recovery, "reposition_container")
    assert inserted.state == "SUCCEEDED"


def test_the_repositioning_goes_to_the_arm_that_can_do_it(
    lid_recovery: TaskResult,
) -> None:
    """G1 cannot reposition a box, so the recovery has to change body."""
    assert _node(lid_recovery, "reposition_container").assigned_robot_id == "b2w_z1_01"
    assert _node(lid_recovery, "open_container").assigned_robot_id == "g1_brainco_01"


def test_the_yaw_the_robot_reported_is_the_yaw_it_is_asked_for(
    lid_recovery: TaskResult, fleet_stub
) -> None:
    """The failing robot said 90 degrees; guessing a default would waste a try."""
    reposition = next(
        submission
        for submission in fleet_stub.stub.submissions
        if (submission.get("manipulation") or {}).get("primitive") == "reposition_container"
    )
    assert reposition["manipulation"]["parameters"]["yaw_change_deg"] == 90


def test_the_open_is_retried_only_after_the_box_has_moved(
    lid_recovery: TaskResult, fleet_stub
) -> None:
    order = _primitives(fleet_stub)
    first_open = order.index("open_container")
    assert order.index("reposition_container") > first_open
    assert order.index("reposition_container") < len(order) - 1
    assert order[order.index("reposition_container") + 1] == "open_container"


def test_the_new_pose_is_physically_verified_not_assumed(
    lid_recovery: TaskResult, orchestrator: Orchestrator
) -> None:
    assert orchestrator.world.is_verified("lid_accessible(box_07)")


def test_the_retry_is_counted_against_the_attempt_budget(
    lid_recovery: TaskResult,
) -> None:
    """Recovery must not reset the counter, or a loop can never terminate."""
    assert _node(lid_recovery, "open_container").attempts == 2


async def test_a_lid_that_never_opens_fails_honestly(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="open_container", error_code="LID_NOT_ACCESSIBLE", remaining=99
        )
    )
    result = await orchestrator.run_task(task_id="t-lid-stuck", goal=drill_goal)

    assert not result.success
    assert result.failure_code == "LID_NOT_ACCESSIBLE"
    assert not result.outcome.goal_verified
    assert not orchestrator.world.holds("on(drill_01, shelf_B12)")
    # Downstream work is abandoned rather than attempted on a closed box.
    assert _node(result, "pick_tool_from_container").state == "SKIPPED"


async def test_recovery_is_bounded(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="open_container", error_code="LID_NOT_ACCESSIBLE", remaining=99
        )
    )
    result = await orchestrator.run_task(task_id="t-lid-stuck", goal=drill_goal)

    assert _node(result, "open_container").attempts <= orchestrator.recovery.policies.max_attempts
    assert "RECOVERY_EXHAUSTED" in _stages(result)


# ==========================================================================
# Example 2: the drill handle is not visible
# ==========================================================================
async def test_an_uncertain_pose_is_re_perceived_before_grasping_again(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="pick_tool_from_container",
            error_code="OBJECT_POSE_UNCERTAIN",
            remaining=1,
        )
    )
    result = await orchestrator.run_task(task_id="t-pose", goal=drill_goal)

    assert result.success
    proposal = _events(result, "RECOVERY_PROPOSED")[0]
    assert proposal.observations["strategy"] == "insert_before"
    # A new viewpoint, not a blind retry of the same grasp.
    assert _node(result, "detect_tool_in_container").state == "SUCCEEDED"


async def test_the_pose_recovery_only_uses_library_actions(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    """The model ranks a closed set; it cannot introduce a primitive."""
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="pick_tool_from_container",
            error_code="OBJECT_POSE_UNCERTAIN",
            remaining=1,
        )
    )
    result = await orchestrator.run_task(task_id="t-pose", goal=drill_goal)

    for node in result.outcome.nodes:
        assert orchestrator.library.get(node.action) is not None


async def test_teleoperation_is_not_offered_without_the_constraint(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    """Handing control to a human is a policy decision, not a fallback."""
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="pick_tool_from_container",
            error_code="OBJECT_POSE_UNCERTAIN",
            remaining=99,
        )
    )
    result = await orchestrator.run_task(task_id="t-pose-stuck", goal=drill_goal)

    assert not result.success
    assert all(node.action != "request_teleoperation" for node in result.outcome.nodes)


async def test_teleoperation_is_offered_when_the_operator_allows_it(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="pick_tool_from_container",
            error_code="OBJECT_POSE_UNCERTAIN",
            remaining=99,
        )
    )
    orchestrator.safety.grant("request_teleoperation")
    result = await orchestrator.run_task(
        task_id="t-pose-teleop",
        goal=drill_goal,
        constraints={"allow_teleoperation": True},
    )

    candidates = {
        candidate_id
        for event in _events(result, "RECOVERY_PROPOSED")
        for candidate_id in event.observations.get("considered", ())
    }
    assert "teleoperate_pick" in candidates


# ==========================================================================
# Example 3: shelf B12 is full
# ==========================================================================
def _add_free_neighbour_shelf(orchestrator: Orchestrator) -> None:
    """`shelf_B13` is a permitted substitute in the inventory policy."""
    orchestrator.world.locations["shelf_B13"] = replace(
        orchestrator.world.locations["shelf_B12"],
        location_id="shelf_B13",
        station_id="SHELF-B13",
        free_space=True,
    )


async def test_a_full_shelf_moves_to_a_permitted_neighbour(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    _add_free_neighbour_shelf(orchestrator)
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="place_object_on_shelf", error_code="TARGET_OCCUPIED", remaining=1
        )
    )
    result = await orchestrator.run_task(task_id="t-shelf", goal=drill_goal)

    assert result.success
    proposal = _events(result, "RECOVERY_PROPOSED")[0]
    assert proposal.observations["strategy"] == "alternate_target"
    assert proposal.observations["value"] == "shelf_B13"
    assert orchestrator.world.holds("on(drill_01, shelf_B13)")


async def test_the_original_shelf_is_recorded_as_full(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    """Otherwise the next task plans straight back into the same failure."""
    _add_free_neighbour_shelf(orchestrator)
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="place_object_on_shelf", error_code="TARGET_OCCUPIED", remaining=1
        )
    )
    await orchestrator.run_task(task_id="t-shelf", goal=drill_goal)

    assert orchestrator.world.locations["shelf_B12"].free_space is False


async def test_a_shelf_outside_the_inventory_policy_is_never_substituted(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    """`exception_station` is free and reachable, and still not allowed."""
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="place_object_on_shelf", error_code="TARGET_OCCUPIED", remaining=99
        )
    )
    result = await orchestrator.run_task(task_id="t-shelf-stuck", goal=drill_goal)

    values = {
        event.observations.get("value") for event in _events(result, "RECOVERY_PROPOSED")
    }
    assert "exception_station" not in values


async def test_with_no_alternative_the_operator_is_asked(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="place_object_on_shelf", error_code="TARGET_OCCUPIED", remaining=99
        )
    )
    result = await orchestrator.run_task(task_id="t-shelf-stuck", goal=drill_goal)

    assert not result.success
    assert "APPROVAL_REQUIRED" in _stages(result)
    assert result.outcome.human_interventions == 1


async def test_an_unplaceable_drill_is_never_reported_as_shelved(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    """README section 15: never complete without verification."""
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="place_object_on_shelf", error_code="TARGET_OCCUPIED", remaining=99
        )
    )
    result = await orchestrator.run_task(task_id="t-shelf-stuck", goal=drill_goal)

    assert not result.success
    assert not result.outcome.goal_verified
    assert not orchestrator.world.holds("on(drill_01, shelf_B12)")


# ==========================================================================
# Example 4: B2-W fails
# ==========================================================================
async def test_a_faulted_transporter_hands_the_carry_to_another_one(
    orchestrator: Orchestrator, move_goal: str, fleet_stub
) -> None:
    _add_second_quadruped(orchestrator, fleet_stub)
    fleet_stub.stub.add_fault(
        FaultInjection(primitive="transport", error_code="ROBOT_FAULT", remaining=1)
    )
    result = await orchestrator.run_task(task_id="t-fault", goal=move_goal)

    assert result.success
    proposal = _events(result, "RECOVERY_PROPOSED")[0]
    assert proposal.observations["strategy"] == "reassign"
    assert _node(result, "transport_container").assigned_robot_id == "b2w_z1_02"


async def test_the_replacement_is_named_in_the_work_order(
    orchestrator: Orchestrator, move_goal: str, fleet_stub
) -> None:
    _add_second_quadruped(orchestrator, fleet_stub)
    fleet_stub.stub.add_fault(
        FaultInjection(primitive="transport", error_code="ROBOT_FAULT", remaining=1)
    )
    await orchestrator.run_task(task_id="t-fault", goal=move_goal)

    preferences = [
        submission.get("preferredRobotId") for submission in fleet_stub.stub.submissions
    ]
    assert "b2w_z1_02" in preferences


async def test_a_robot_still_holding_the_box_is_not_silently_replaced(
    orchestrator: Orchestrator, move_goal: str, fleet_stub
) -> None:
    """Sending a second robot after a box the first one is gripping is how you
    break a box, so the handover needs an operator."""
    _add_second_quadruped(orchestrator, fleet_stub)
    orchestrator.world.robots["b2w_z1_01"].payload_object_ids.append("box_07")
    fleet_stub.stub.add_fault(
        FaultInjection(primitive="transport", error_code="ROBOT_FAULT", remaining=99)
    )
    result = await orchestrator.run_task(task_id="t-fault-held", goal=move_goal)

    assert not result.success
    assert "APPROVAL_REQUIRED" in _stages(result)
    strategies = {
        event.observations.get("strategy") for event in _events(result, "RECOVERY_PROPOSED")
    }
    assert "reassign" not in strategies


async def test_with_no_second_robot_the_fault_is_reported_not_absorbed(
    orchestrator: Orchestrator, move_goal: str, fleet_stub
) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(primitive="transport", error_code="ROBOT_FAULT", remaining=99)
    )
    result = await orchestrator.run_task(task_id="t-fault-alone", goal=move_goal)

    assert not result.success
    assert result.failure_code == "ROBOT_FAULT"


# ==========================================================================
# Properties that hold across every scenario
# ==========================================================================
@pytest.mark.parametrize(
    "primitive,error_code",
    [
        ("open_container", "LID_NOT_ACCESSIBLE"),
        ("pick_tool_from_container", "OBJECT_POSE_UNCERTAIN"),
        ("place_object_on_shelf", "TARGET_OCCUPIED"),
        ("transport", "ROBOT_FAULT"),
        ("transport", "PATH_BLOCKED"),
        ("open_container", "GRASP_FAILED"),
        ("pick_tool_from_container", "SLIP_DETECTED"),
        ("place_object_on_shelf", "TIMEOUT"),
    ],
)
async def test_a_permanent_failure_never_reports_success(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub, primitive: str, error_code: str
) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(primitive=primitive, error_code=error_code, remaining=99)
    )
    result = await orchestrator.run_task(task_id=f"t-{error_code.lower()}", goal=drill_goal)

    assert not result.success
    assert not result.outcome.goal_verified
    assert not orchestrator.world.is_verified("verified_at(drill_01, shelf_B12)")
    assert _stages(result)[-1] == "TASK_FAILED"


@pytest.mark.parametrize(
    "primitive,error_code",
    [
        ("open_container", "LID_NOT_ACCESSIBLE"),
        ("pick_tool_from_container", "OBJECT_POSE_UNCERTAIN"),
        ("open_container", "GRASP_FAILED"),
    ],
)
async def test_one_transient_failure_is_absorbed(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub, primitive: str, error_code: str
) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(primitive=primitive, error_code=error_code, remaining=1)
    )
    result = await orchestrator.run_task(task_id=f"t-{error_code.lower()}", goal=drill_goal)

    assert result.success
    assert result.outcome.goal_verified
    assert result.outcome.recovery_actions


async def test_every_recovery_is_visible_in_the_result(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    """A silent recovery is indistinguishable from a system that got lucky."""
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="open_container", error_code="LID_NOT_ACCESSIBLE", remaining=1
        )
    )
    result = await orchestrator.run_task(task_id="t-visible", goal=drill_goal)

    assert len(result.outcome.recovery_actions) == len(_events(result, "RECOVERY_PROPOSED"))
    for action in result.outcome.recovery_actions:
        assert action["reason"]
        assert action["strategy"]


# ==========================================================================
# Declaring a fault over HTTP
# ==========================================================================
def test_a_bare_fault_object_is_accepted() -> None:
    """`curl` users write the object directly far more often than a list."""
    faults = parse_fault_request(
        {"primitive": "open_container", "errorCode": "LID_NOT_ACCESSIBLE"}
    )
    assert len(faults) == 1
    assert faults[0].error_code == "LID_NOT_ACCESSIBLE"
    assert faults[0].remaining == 1


@pytest.mark.parametrize("key", ["errorCode", "error_code"])
def test_either_spelling_of_the_error_code_works(key: str) -> None:
    faults = parse_fault_request({"primitive": "open_container", key: "GRASP_FAILED"})
    assert faults[0].error_code == "GRASP_FAILED"


def test_several_faults_can_be_declared_at_once() -> None:
    faults = parse_fault_request(
        {
            "faults": [
                {"primitive": "open_container", "errorCode": "LID_NOT_ACCESSIBLE"},
                {"primitive": "transport", "errorCode": "PATH_BLOCKED", "remaining": 3},
            ]
        }
    )
    assert [fault.remaining for fault in faults] == [1, 3]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"faults": []},
        {"typo": "open_container"},
        {"primitive": "open_container"},
        {"errorCode": "GRASP_FAILED"},
    ],
)
def test_a_declaration_that_injects_nothing_is_refused(body: dict) -> None:
    """Silently accepting this yields a green run that proved nothing."""
    with pytest.raises(ValueError):
        parse_fault_request(body)


async def test_a_recovered_task_still_conforms_to_the_result_contract(
    orchestrator: Orchestrator, drill_goal: str, fleet_stub
) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="open_container", error_code="LID_NOT_ACCESSIBLE", remaining=1
        )
    )
    result = await orchestrator.run_task(task_id="t-contract", goal=drill_goal)

    assert orchestrator.schemas.validation_errors(
        "execution_result", result.outcome.to_dict()
    ) == []
    for event in result.events.to_list():
        assert orchestrator.schemas.validation_errors("execution_event", event) == []
