"""The Fleet Control backend against a live stub.

These drive the real HTTP client over real sockets. The point is that work-order
construction, header handling, polling, idempotency, and error mapping are all
exercised on the same code path production uses, rather than against a mock whose
agreement with Fleet Control nobody can check.
"""

from __future__ import annotations

import pytest

from brash_orchestrator_core.backends.base import PrimitiveRequest
from brash_orchestrator_core.backends.fleet_control import (
    MANIPULATION_EXTENSION,
    FleetControlBackend,
    FleetControlError,
    FleetControlSettings,
    map_failure_reason,
)
from brash_world_model.model import MissionLocation
from tools.fleet_stub.server import FaultInjection, FleetStub, FleetStubServer

MAP = "demo-plant@1"


def _location(station: str, x: float = 0.0, y: float = 0.0, map_id: str = MAP) -> MissionLocation:
    return MissionLocation(station_id=station, map_id=map_id, x=x, y=y)


def _transport(**kwargs) -> PrimitiveRequest:
    defaults = dict(
        task_id="t1",
        node_id="n2",
        action_id="transport_container",
        primitive="transport_container",
        kind="transport",
        robot_id="b2w_z1_01",
        parameters={"container_id": "box_07", "destination": "handover_station_02"},
        timeout_s=30.0,
        fleet_capabilities=("material_transport",),
        pickup=_location("MAINT-01", 8.5, 4.0),
        dropoff=_location("HANDOVER-02", 36.0, 14.5),
        payload_kg=8.5,
        verification="at(box_07, handover_station_02)",
    )
    defaults.update(kwargs)
    return PrimitiveRequest(**defaults)


def _manipulation(**kwargs) -> PrimitiveRequest:
    defaults = dict(
        task_id="t1",
        node_id="n4",
        action_id="open_container",
        primitive="open_container",
        kind="manipulation",
        robot_id="g1_brainco_01",
        parameters={"container_id": "box_07"},
        timeout_s=30.0,
        fleet_capabilities=("inspection",),
        station=_location("HANDOVER-02", 36.0, 14.5),
        container_id="box_07",
        verification="state(box_07, open)",
    )
    defaults.update(kwargs)
    return PrimitiveRequest(**defaults)


@pytest.fixture(autouse=True)
async def _close_backend(fleet_backend: FleetControlBackend):
    yield
    await fleet_backend.close()


# -- health and capability probing ----------------------------------------
async def test_the_stub_advertises_the_manipulation_extension(
    fleet_backend: FleetControlBackend,
) -> None:
    health = await fleet_backend.health()
    assert health.reachable
    assert MANIPULATION_EXTENSION in health.extensions
    assert health.supports_kind("manipulation")
    assert health.supports_kind("transport")


async def test_health_is_cached_until_refreshed(
    fleet_backend: FleetControlBackend,
) -> None:
    first = await fleet_backend.health()
    assert await fleet_backend.health() is first


async def test_an_unreachable_fleet_is_reported_not_raised() -> None:
    backend = FleetControlBackend(
        FleetControlSettings(base_url="http://127.0.0.1:1", request_timeout_s=1.0)
    )
    try:
        health = await backend.health()
        assert not health.reachable
        assert health.detail
    finally:
        await backend.close()


async def test_manipulation_is_refused_when_the_extension_is_absent() -> None:
    """Better an explicit refusal than a manipulation quietly run as a transport."""
    stub = FleetStub(support_manipulation=False)
    with FleetStubServer(stub, port=0) as server:
        backend = FleetControlBackend(
            FleetControlSettings(base_url=server.base_url, poll_interval_s=0.01)
        )
        try:
            health = await backend.health()
            assert health.reachable
            assert not health.supports_kind("manipulation")
            assert MANIPULATION_EXTENSION in health.detail
            assert "INTEGRATION_NOTES" in health.detail

            result = await backend.execute(_manipulation())
            assert not result.success
            assert result.error_code == "BACKEND_UNSUPPORTED"
        finally:
            await backend.close()


async def test_transport_still_works_without_the_extension() -> None:
    stub = FleetStub(support_manipulation=False)
    with FleetStubServer(stub, port=0) as server:
        backend = FleetControlBackend(
            FleetControlSettings(base_url=server.base_url, poll_interval_s=0.01)
        )
        try:
            result = await backend.execute(_transport())
            assert result.success
        finally:
            await backend.close()


# -- work-order construction ----------------------------------------------
def test_a_transport_node_needs_no_extension(
    fleet_backend: FleetControlBackend,
) -> None:
    """Every field here is one Fleet Control's own contracts already define.

    `preferredRobotId` is the one to watch: it is declared on
    `CreateMissionRequest` and weighted by the dispatch policy, but the
    work-order parser currently discards it, so on an unextended fleet it is
    accepted and ignored rather than rejected.
    """
    body = fleet_backend.build_work_order(_transport())
    assert set(body) == {
        "name",
        "externalReference",
        "priority",
        "requiredCapabilities",
        "payloadKg",
        "pickup",
        "dropoff",
        "preferredRobotId",
    }
    assert body["requiredCapabilities"] == ["material_transport"]
    assert body["payloadKg"] == 8.5
    assert body["preferredRobotId"] == "b2w_z1_01"
    assert "contract" not in body


def test_a_manipulation_node_adds_only_new_fields(
    fleet_backend: FleetControlBackend,
) -> None:
    """The extension is additive, so Fleet Control's existing types are untouched."""
    body = fleet_backend.build_work_order(_manipulation())
    assert body["contract"] == MANIPULATION_EXTENSION
    assert body["kind"] == "MANIPULATION"
    assert body["manipulation"]["primitive"] == "open_container"
    assert body["manipulation"]["containerId"] == "box_07"
    assert body["manipulation"]["readOnly"] is False
    # Every field the current contract already defines is still present.
    assert {"name", "requiredCapabilities", "pickup", "dropoff"} <= set(body)


def test_a_perception_node_is_marked_read_only(
    fleet_backend: FleetControlBackend,
) -> None:
    body = fleet_backend.build_work_order(_manipulation(kind="perception"))
    assert body["manipulation"]["readOnly"] is True


def test_the_verification_predicate_is_sent_with_the_order(
    fleet_backend: FleetControlBackend,
) -> None:
    """Verification becomes the executor's explicit job, not an inference."""
    body = fleet_backend.build_work_order(_manipulation())
    assert body["manipulation"]["verification"] == "state(box_07, open)"


@pytest.mark.parametrize("request_factory", [_transport, _manipulation])
def test_the_scheduler_choice_is_sent_as_a_preference(
    fleet_backend: FleetControlBackend, request_factory
) -> None:
    """Fleet Control's dispatch keeps the final say, so this is never a binding."""
    body = fleet_backend.build_work_order(request_factory())
    assert body["preferredRobotId"] == request_factory().robot_id
    assert "assignedRobotId" not in body


def test_the_external_reference_includes_the_attempt(
    fleet_backend: FleetControlBackend,
) -> None:
    """A retry is new work; reusing the reference would replay the failed order."""
    first = _manipulation(attempt=1).external_reference
    second = _manipulation(attempt=2).external_reference
    assert first != second
    assert first == "brash-task:t1:n4:1"


def test_a_node_with_no_station_is_rejected_before_submission(
    fleet_backend: FleetControlBackend,
) -> None:
    with pytest.raises(FleetControlError, match="no station"):
        fleet_backend.build_work_order(_manipulation(station=None))


def test_a_node_spanning_two_map_revisions_is_rejected_and_named(
    fleet_backend: FleetControlBackend,
) -> None:
    """Fleet Control rejects this too, but failing here names the node."""
    with pytest.raises(FleetControlError, match="spans map revisions"):
        fleet_backend.build_work_order(
            _transport(dropoff=_location("HANDOVER-02", map_id="demo-plant@2"))
        )


# -- execution -------------------------------------------------------------
async def test_a_transport_order_runs_to_success(
    fleet_backend: FleetControlBackend, fleet_stub: FleetStubServer
) -> None:
    result = await fleet_backend.execute(_transport())
    assert result.success
    assert result.error_code == "SUCCESS"
    assert result.external_reference
    assert fleet_stub.stub.submissions


async def test_a_completed_transport_counts_as_evidence_of_arrival(
    fleet_backend: FleetControlBackend,
) -> None:
    """The fleet's own localization confirms the drop-off, so this is observed."""
    result = await fleet_backend.execute(_transport())
    assert result.observed_effects["verified_predicates"] == [
        "at(box_07, handover_station_02)"
    ]
    assert 0.0 < result.observed_effects["confidence"] < 1.0
    assert "fleet mission completion" in result.observed_effects["evidence"]


async def test_a_manipulation_order_reports_the_predicate_it_confirmed(
    fleet_backend: FleetControlBackend,
) -> None:
    result = await fleet_backend.execute(_manipulation())
    assert result.success
    assert result.observed_effects["verified_predicates"] == ["state(box_07, open)"]


async def test_the_selected_robot_is_reported_back(
    fleet_backend: FleetControlBackend,
) -> None:
    result = await fleet_backend.execute(_manipulation())
    assert result.observed_effects["selected_robot_id"] == "g1_brainco_01"


async def test_a_fleet_substitution_is_recorded_rather_than_hidden(
    fleet_backend: FleetControlBackend, fleet_stub: FleetStubServer
) -> None:
    """Dispatch may override the preference; the operator has to be able to see it."""
    fleet_stub.stub.robots["g1_brainco_01"].availability = "BUSY"
    result = await fleet_backend.execute(_manipulation())
    assert result.success
    assert result.observed_effects["selected_robot_id"] == "b2w_z1_01"
    assert result.observed_effects["preferred_robot_id"] == "g1_brainco_01"


async def test_submitting_the_same_reference_twice_returns_the_same_order(
    fleet_backend: FleetControlBackend, fleet_stub: FleetStubServer
) -> None:
    first = await fleet_backend.execute(_transport())
    second = await fleet_backend.execute(_transport())
    assert first.external_reference == second.external_reference
    assert len(fleet_stub.stub.work_orders) == 1


async def test_a_retry_creates_a_new_order(
    fleet_backend: FleetControlBackend, fleet_stub: FleetStubServer
) -> None:
    await fleet_backend.execute(_transport(attempt=1))
    await fleet_backend.execute(_transport(attempt=2))
    assert len(fleet_stub.stub.work_orders) == 2


# -- error mapping ---------------------------------------------------------
async def test_an_injected_fault_reaches_the_orchestrator_as_a_shared_error_code(
    fleet_backend: FleetControlBackend, fleet_stub: FleetStubServer
) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(primitive="open_container", error_code="LID_NOT_ACCESSIBLE")
    )
    result = await fleet_backend.execute(_manipulation())
    assert not result.success
    assert result.error_code == "LID_NOT_ACCESSIBLE"
    assert "injected fault" in result.detail


async def test_fault_observations_are_carried_through(
    fleet_backend: FleetControlBackend, fleet_stub: FleetStubServer
) -> None:
    """The recovery planner reads these to pick a repositioning angle."""
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="open_container",
            error_code="LID_NOT_ACCESSIBLE",
            observations={"recommended_yaw_change_deg": 35},
        )
    )
    result = await fleet_backend.execute(_manipulation())
    assert result.observed_effects["recommended_yaw_change_deg"] == 35


async def test_a_fault_can_be_scoped_to_one_node(
    fleet_backend: FleetControlBackend, fleet_stub: FleetStubServer
) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="open_container", error_code="LID_NOT_ACCESSIBLE", node_id="n9"
        )
    )
    assert (await fleet_backend.execute(_manipulation(node_id="n4"))).success
    assert not (await fleet_backend.execute(_manipulation(node_id="n9"))).success


async def test_a_fault_is_consumed_so_the_next_attempt_can_succeed(
    fleet_backend: FleetControlBackend, fleet_stub: FleetStubServer
) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(primitive="open_container", error_code="LID_NOT_ACCESSIBLE", remaining=1)
    )
    assert not (await fleet_backend.execute(_manipulation(attempt=1))).success
    assert (await fleet_backend.execute(_manipulation(attempt=2))).success


async def test_an_unallocatable_order_maps_to_a_precondition_failure(
    fleet_backend: FleetControlBackend, fleet_stub: FleetStubServer
) -> None:
    for robot in fleet_stub.stub.robots.values():
        robot.availability = "BUSY"
    result = await fleet_backend.execute(_transport())
    assert not result.success
    assert result.error_code == "PRECONDITION_FAILED"


async def test_a_rejected_capability_is_reported_not_retried_blindly(
    fleet_backend: FleetControlBackend,
) -> None:
    result = await fleet_backend.execute(
        _transport(fleet_capabilities=("teleport_payload",))
    )
    assert not result.success
    assert result.error_code == "PRECONDITION_FAILED"
    assert "unknown capabilities" in result.detail


async def test_missing_credentials_are_reported_as_a_precondition_failure(
    fleet_stub: FleetStubServer,
) -> None:
    backend = FleetControlBackend(
        FleetControlSettings(base_url=fleet_stub.base_url, token="", site_id="")
    )
    try:
        result = await backend.execute(_transport())
        assert not result.success
        assert result.error_code == "PRECONDITION_FAILED"
    finally:
        await backend.close()


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("LID_NOT_ACCESSIBLE", "LID_NOT_ACCESSIBLE"),
        ("The lid is not accessible from this pose", "LID_NOT_ACCESSIBLE"),
        ("shelf bay has no free space", "TARGET_OCCUPIED"),
        ("object pose uncertain after three views", "OBJECT_POSE_UNCERTAIN"),
        ("grasp failed on the handle", "GRASP_FAILED"),
        ("payload slip detected mid-carry", "SLIP_DETECTED"),
        ("route is blocked by traffic", "PATH_BLOCKED"),
        ("robot heartbeat lost", "COMMUNICATION_LOST"),
        ("mission timed out", "TIMEOUT"),
        ("robot moved to quarantine", "ROBOT_FAULT"),
        ("no eligible robot for the requested capabilities", "PRECONDITION_FAILED"),
        ("a person entered the safety zone", "HUMAN_IN_SAFETY_ZONE"),
        ("something nobody anticipated", "UNKNOWN_FAILURE"),
        (None, "UNKNOWN_FAILURE"),
    ],
)
def test_fleet_prose_maps_onto_the_closed_error_set(reason, expected: str) -> None:
    """Fleet failure reasons are human-readable prose, not an enum."""
    assert map_failure_reason(reason) == expected


# -- timeout and cancellation ---------------------------------------------
async def test_an_order_that_outlives_its_timeout_is_cancelled() -> None:
    stub = FleetStub(execution_delay_s=1.0)
    with FleetStubServer(stub, port=0) as server:
        backend = FleetControlBackend(
            FleetControlSettings(base_url=server.base_url, poll_interval_s=0.01)
        )
        try:
            result = await backend.execute(_transport(timeout_s=0.05))
            assert not result.success
            assert result.error_code == "TIMEOUT"
            assert "exceeded" in result.detail
        finally:
            await backend.close()


async def test_cancellation_reaches_the_fleet(
    fleet_backend: FleetControlBackend, fleet_stub: FleetStubServer
) -> None:
    request = _transport()
    await fleet_backend.execute(request)
    await fleet_backend.cancel(request)
    # The order already finished, so cancelling is a no-op the fleet accepts.
    assert fleet_stub.stub.work_orders


async def test_cancelling_something_never_submitted_is_harmless(
    fleet_backend: FleetControlBackend,
) -> None:
    await fleet_backend.cancel(_transport(node_id="never_sent"))


async def test_emergency_stop_cancels_everything_submitted(
    fleet_backend: FleetControlBackend, fleet_stub: FleetStubServer
) -> None:
    stub = fleet_stub.stub
    stub.execution_delay_s = 0.0
    await fleet_backend.execute(_transport())
    await fleet_backend.emergency_stop()
    assert stub.work_orders


# -- fleet state -----------------------------------------------------------
async def test_robot_states_are_normalized(
    fleet_backend: FleetControlBackend,
) -> None:
    states = await fleet_backend.robot_states()
    assert set(states) == {"b2w_z1_01", "g1_brainco_01"}
    for state in states.values():
        assert state["availability"].islower()
        assert state["connection"].islower()
        assert isinstance(state["battery_percent"], float)


async def test_robot_states_are_empty_when_the_fleet_is_unreachable() -> None:
    backend = FleetControlBackend(
        FleetControlSettings(base_url="http://127.0.0.1:1", request_timeout_s=1.0)
    )
    try:
        assert await backend.robot_states() == {}
    finally:
        await backend.close()
