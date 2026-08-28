"""Safety policy enforcement.

The rules here are evaluated from typed configuration and world state only. These
tests assert that, and assert that no plan-supplied value can widen them.
"""

from __future__ import annotations

import pytest

from brash_orchestrator_core.backends.base import PrimitiveRequest
from brash_safety_manager.policy import SafetyManager, SafetyPolicy
from brash_world_model.model import MissionLocation, WorldModel


def _request(**kwargs) -> PrimitiveRequest:
    defaults = dict(
        task_id="t1",
        node_id="n1",
        action_id="open_container",
        primitive="open_container",
        kind="manipulation",
        robot_id="g1_brainco_01",
        parameters={},
        timeout_s=60.0,
    )
    defaults.update(kwargs)
    return PrimitiveRequest(**defaults)


def _at(zone: str) -> MissionLocation:
    return MissionLocation(station_id="S", map_id="demo-plant@1", x=0.0, y=0.0, zone_id=zone)


# -- payload --------------------------------------------------------------
def test_a_payload_over_the_site_ceiling_is_refused(
    safety: SafetyManager, world: WorldModel
) -> None:
    permitted, code, detail = safety.evaluate(_request(payload_kg=99.0), world)
    assert not permitted
    assert code == "PAYLOAD_LIMIT_EXCEEDED"
    assert "site limit" in detail


def test_a_payload_inside_the_ceiling_is_allowed(
    safety: SafetyManager, world: WorldModel
) -> None:
    permitted, code, _ = safety.evaluate(_request(payload_kg=8.5), world)
    assert permitted and code == "SUCCESS"


# -- battery --------------------------------------------------------------
def test_a_robot_below_the_battery_floor_is_refused(
    safety: SafetyManager, world: WorldModel
) -> None:
    world.robots["g1_brainco_01"].battery_percent = 5.0
    permitted, code, detail = safety.evaluate(_request(), world)
    assert not permitted
    assert code == "SAFETY_POLICY_VIOLATION"
    assert "battery" in detail


def test_an_unknown_robot_is_not_blocked_on_battery(
    safety: SafetyManager, world: WorldModel
) -> None:
    """A missing telemetry record is not evidence of a flat battery."""
    permitted, _, _ = safety.evaluate(_request(robot_id="not_in_world"), world)
    assert permitted


# -- zones ----------------------------------------------------------------
def test_a_forbidden_zone_is_refused(world: WorldModel) -> None:
    manager = SafetyManager(SafetyPolicy(forbidden_zones=frozenset({"high_voltage"})))
    permitted, code, detail = manager.evaluate(_request(station=_at("high_voltage")), world)
    assert not permitted
    assert code == "SAFETY_POLICY_VIOLATION"
    assert "forbidden" in detail


def test_a_restricted_zone_allows_only_its_listed_primitives(world: WorldModel) -> None:
    manager = SafetyManager(
        SafetyPolicy(restricted_zones={"laboratory": ("verify_object_pose",)})
    )
    blocked, _, detail = manager.evaluate(
        _request(primitive="open_container", station=_at("laboratory")), world
    )
    assert not blocked
    assert "allows only" in detail

    allowed, _, _ = manager.evaluate(
        _request(primitive="verify_object_pose", station=_at("laboratory")), world
    )
    assert allowed


def test_an_approval_zone_needs_an_explicit_grant(world: WorldModel) -> None:
    manager = SafetyManager(SafetyPolicy(approval_zones=frozenset({"high_bay"})))
    permitted, _, detail = manager.evaluate(_request(station=_at("high_bay")), world)
    assert not permitted
    assert "approval" in detail

    manager.grant("high_bay")
    permitted, _, _ = manager.evaluate(_request(station=_at("high_bay")), world)
    assert permitted


def test_revoking_an_approval_restores_the_block(world: WorldModel) -> None:
    manager = SafetyManager(
        SafetyPolicy(approval_zones=frozenset({"high_bay"})), approvals={"high_bay"}
    )
    assert manager.evaluate(_request(station=_at("high_bay")), world)[0]
    manager.revoke("high_bay")
    assert not manager.evaluate(_request(station=_at("high_bay")), world)[0]


def test_zones_are_checked_on_pickup_and_dropoff_too(world: WorldModel) -> None:
    manager = SafetyManager(SafetyPolicy(forbidden_zones=frozenset({"high_voltage"})))
    permitted, _, _ = manager.evaluate(
        _request(kind="transport", pickup=_at("safe"), dropoff=_at("high_voltage")), world
    )
    assert not permitted


# -- human proximity -----------------------------------------------------
def test_a_person_inside_the_manipulation_clearance_stops_work(
    safety: SafetyManager, world: WorldModel
) -> None:
    world.robots["g1_brainco_01"].human_distance_m = 0.4
    permitted, code, detail = safety.evaluate(_request(kind="manipulation"), world)
    assert not permitted
    assert code == "HUMAN_IN_SAFETY_ZONE"
    assert "2.0m clearance" in detail


def test_clearance_thresholds_differ_by_work_kind(
    safety: SafetyManager, world: WorldModel
) -> None:
    """Looking at something from 1m away is not the same risk as reaching into it."""
    world.robots["g1_brainco_01"].human_distance_m = 1.0
    assert not safety.evaluate(_request(kind="manipulation"), world)[0]
    assert safety.evaluate(_request(kind="perception"), world)[0]


def test_no_human_reading_does_not_block_work(
    safety: SafetyManager, world: WorldModel
) -> None:
    assert safety.evaluate(_request(), world)[0]


# -- named approvals -----------------------------------------------------
def test_teleoperation_always_needs_a_named_approval(
    safety: SafetyManager, world: WorldModel
) -> None:
    request = _request(primitive="request_teleoperation", action_id="request_teleoperation")
    permitted, _, detail = safety.evaluate(request, world)
    assert not permitted
    assert "operator approval" in detail

    safety.grant("request_teleoperation")
    assert safety.evaluate(request, world)[0]


def test_an_approval_can_be_scoped_to_one_node(world: WorldModel) -> None:
    manager = SafetyManager(
        SafetyPolicy(approval_required_primitives=frozenset({"request_teleoperation"})),
        approvals={"t1:n7"},
    )
    scoped = _request(primitive="request_teleoperation", node_id="n7")
    other = _request(primitive="request_teleoperation", node_id="n9")
    assert manager.evaluate(scoped, world)[0]
    assert not manager.evaluate(other, world)[0]


# -- concurrency ---------------------------------------------------------
def test_the_concurrency_limit_bounds_the_blast_radius(world: WorldModel) -> None:
    manager = SafetyManager(SafetyPolicy(max_concurrent_primitives=2))
    manager.acquire()
    manager.acquire()
    permitted, _, detail = manager.evaluate(_request(), world)
    assert not permitted
    assert "in flight" in detail

    manager.release()
    assert manager.evaluate(_request(), world)[0]


def test_release_never_goes_negative(world: WorldModel) -> None:
    manager = SafetyManager(SafetyPolicy(max_concurrent_primitives=1))
    manager.release()
    manager.release()
    assert manager.evaluate(_request(), world)[0]


# -- bookkeeping and structure -------------------------------------------
def test_blocked_requests_are_recorded_for_the_operator(
    safety: SafetyManager, world: WorldModel
) -> None:
    safety.evaluate(_request(payload_kg=99.0), world)
    assert len(safety.blocked) == 1
    entry = safety.blocked[0]
    assert entry["node_id"] == "n1"
    assert entry["rule"] == "global_max_payload_kg"


def test_disabling_the_policy_permits_everything(world: WorldModel) -> None:
    manager = SafetyManager(SafetyPolicy(enabled=False, global_max_payload_kg=1.0))
    assert manager.evaluate(_request(payload_kg=500.0), world)[0]


def test_the_shipped_policy_forbids_teleoperation_without_approval() -> None:
    policy = SafetyPolicy.load()
    assert "request_teleoperation" in policy.approval_required_primitives
    assert policy.enabled


def test_a_plan_cannot_supply_its_own_limits(
    safety: SafetyManager, world: WorldModel
) -> None:
    """The request carries no policy field, so there is nothing for a plan to set."""
    fields = set(PrimitiveRequest.__dataclass_fields__)
    assert not {
        "max_payload_kg",
        "human_clearance_m",
        "safety_override",
        "approvals",
    } & fields


@pytest.mark.parametrize(
    "attribute", ["global_max_payload_kg", "min_battery_percent", "max_concurrent_primitives"]
)
def test_every_shipped_limit_is_positive(attribute: str) -> None:
    assert getattr(SafetyPolicy.load(), attribute) > 0
