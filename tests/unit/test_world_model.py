"""World model: predicates, derived facts, verification provenance, deltas."""

from __future__ import annotations

import pytest

from brash_world_model.model import (
    PredicateContext,
    WorldModel,
    WorldModelError,
    WorldStateDelta,
)
from brash_world_model.predicates import (
    Predicate,
    PredicateError,
    bind_predicate,
    is_derived,
    parse_predicate,
)


# -- predicate parsing ---------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("on(drill_01, shelf_B12)", Predicate("on", ("drill_01", "shelf_B12"))),
        ("state(box_07,closed)", Predicate("state", ("box_07", "closed"))),
        ("  pose_known( box_07 )  ", Predicate("pose_known", ("box_07",))),
        ("idle()", Predicate("idle", ())),
    ],
)
def test_parse_predicate_normalizes_whitespace(text: str, expected: Predicate) -> None:
    assert parse_predicate(text) == expected


def test_predicates_compare_equal_regardless_of_source_spacing() -> None:
    """This is the whole reason predicates are a value object, not a string."""
    assert parse_predicate("state(box_07, open)") == parse_predicate("state(box_07,open)")


@pytest.mark.parametrize("text", ["not a predicate", "on(a,)", "on(", "on(a, )b"])
def test_parse_predicate_rejects_malformed_input(text: str) -> None:
    with pytest.raises(PredicateError):
        parse_predicate(text)


def test_bind_predicate_substitutes_parameters_and_keeps_literals() -> None:
    bound = bind_predicate(
        "object_type(container_id, container)", {"container_id": "box_07"}
    )
    assert str(bound) == "object_type(box_07, container)"


def test_bind_predicate_resolves_the_assigned_robot() -> None:
    bound = bind_predicate(
        "held_by(object_id, assigned_robot)",
        {"object_id": "drill_01"},
        assigned_robot="g1_brainco_01",
    )
    assert str(bound) == "held_by(drill_01, g1_brainco_01)"


def test_bind_predicate_refuses_an_unassigned_robot() -> None:
    with pytest.raises(PredicateError, match="assigned_robot"):
        bind_predicate("held_by(object_id, assigned_robot)", {"object_id": "drill_01"})


# -- scenario loading ----------------------------------------------------
def test_the_demo_scenario_seeds_the_expected_facts(world: WorldModel) -> None:
    assert world.holds("state(box_07, closed)")
    assert world.holds("inside(drill_01, box_07)")
    assert "b2w_z1_01" in world.robots
    assert "g1_brainco_01" in world.robots


def test_seeded_facts_are_verified_because_the_operator_declared_them(
    world: WorldModel,
) -> None:
    fact = world.fact("state(box_07, closed)")
    assert fact is not None
    assert fact.verified
    assert fact.source == "SCENARIO_LOAD"


def test_an_unknown_flag_stays_unknown_rather_than_defaulting_to_false(
    world: WorldModel,
) -> None:
    """``unknown`` in a scenario means "nobody has looked yet".

    The distinction matters for planning: the box's lid accessibility is not
    known to be false, it is simply not established, and a plan has to establish
    it before opening the box.
    """
    assert not world.holds("lid_accessible(box_07)")
    assert world.fact("lid_accessible(box_07)") is None


# -- derived predicates --------------------------------------------------
def test_derived_predicates_are_answered_by_inspection(world: WorldModel) -> None:
    assert is_derived(parse_predicate("object_exists(box_07)"))
    assert world.holds("object_exists(box_07)")
    assert not world.holds("object_exists(nothing_here)")


def test_object_type_reads_the_entity(world: WorldModel) -> None:
    assert world.holds("object_type(box_07, container)")
    assert not world.holds("object_type(box_07, tool)")


def test_mass_within_limit_depends_on_the_candidate_robot(world: WorldModel) -> None:
    heavy = PredicateContext(payload_limit_kg=0.5)
    generous = PredicateContext(payload_limit_kg=50.0)
    assert world.holds("mass_within_limit(box_07)", generous)
    assert not world.holds("mass_within_limit(box_07)", heavy)


def test_shelf_height_supported_respects_reach(world: WorldModel) -> None:
    short = PredicateContext(max_reach_height_m=0.5)
    tall = PredicateContext(min_reach_height_m=0.1, max_reach_height_m=3.0)
    assert world.holds("shelf_height_supported(shelf_B12)", tall)
    assert not world.holds("shelf_height_supported(shelf_B12)", short)


def test_route_available_rejects_a_blocked_location(world: WorldModel) -> None:
    world.blocked_locations.add("shelf_B12")
    assert not world.holds("route_available(shelf_B12)")


def test_route_between_floors_needs_a_declared_connection(world: WorldModel) -> None:
    world.connected_floors = []
    context = PredicateContext(assigned_robot="b2w_z1_01")
    world.robots["b2w_z1_01"].location = "maintenance_area"
    assert world.locations["maintenance_area"].floor != world.locations["shelf_B12"].floor
    assert not world.holds("route_available(shelf_B12)", context)


def test_a_derived_predicate_cannot_be_asserted(world: WorldModel) -> None:
    """Asserting a derived fact would let it drift from the entity it describes."""
    with pytest.raises(WorldModelError, match="derived"):
        world.apply_delta(WorldStateDelta(asserted=["object_exists(ghost)"]))


def test_an_unrecorded_predicate_is_simply_false(world: WorldModel) -> None:
    """Stored facts are closed-world: absent means not established, not an error."""
    assert not world.holds("wildly_invented_predicate(box_07)")


def test_a_derived_predicate_with_the_wrong_arity_raises(world: WorldModel) -> None:
    with pytest.raises(PredicateError, match="arity"):
        world.holds("object_type(box_07)")


# -- deltas and provenance ----------------------------------------------
def test_only_an_observation_marks_a_fact_verified(world: WorldModel) -> None:
    world.apply_delta(
        WorldStateDelta(asserted=["on(drill_01, shelf_B12)"], source="BACKEND_CLAIM")
    )
    assert world.holds("on(drill_01, shelf_B12)")
    assert not world.is_verified("on(drill_01, shelf_B12)")

    world.apply_delta(
        WorldStateDelta(asserted=["on(drill_01, shelf_B12)"], source="OBSERVATION")
    )
    assert world.is_verified("on(drill_01, shelf_B12)")


def test_a_claim_never_downgrades_an_observation(world: WorldModel) -> None:
    world.apply_delta(WorldStateDelta(asserted=["pose_known(box_07)"], source="OBSERVATION"))
    world.apply_delta(WorldStateDelta(asserted=["pose_known(box_07)"], source="BACKEND_CLAIM"))
    assert world.is_verified("pose_known(box_07)")


def test_asserting_a_new_location_drops_the_stale_one(world: WorldModel) -> None:
    assert world.holds("at(box_07, maintenance_area)")
    world.assert_all(["at(box_07, handover_station_02)"])

    assert world.holds("at(box_07, handover_station_02)")
    # An object is at exactly one place, so the stale fact has to go.
    assert not world.holds("at(box_07, maintenance_area)")
    assert world.objects["box_07"].location == "handover_station_02"


def test_asserting_a_new_state_replaces_the_old_state(world: WorldModel) -> None:
    world.assert_all(["state(box_07, open)"])
    assert world.holds("state(box_07, open)")
    assert not world.holds("state(box_07, closed)")
    assert world.objects["box_07"].state == "open"


def test_negated_assertions_retract_rather_than_store(world: WorldModel) -> None:
    world.assert_all(["held_by(drill_01, g1_brainco_01)"])
    assert "drill_01" in world.robots["g1_brainco_01"].payload_object_ids

    world.assert_all(["not_held(drill_01)"])
    assert not world.holds("held_by(drill_01, g1_brainco_01)")
    assert "drill_01" not in world.robots["g1_brainco_01"].payload_object_ids


def test_not_inside_retracts_containment(world: WorldModel) -> None:
    assert world.holds("inside(drill_01, box_07)")
    world.assert_all(["not_inside(drill_01, box_07)"])
    assert not world.holds("inside(drill_01, box_07)")


def test_retracting_a_fact_that_was_never_true_is_harmless(world: WorldModel) -> None:
    world.apply_delta(WorldStateDelta(retracted=["on(drill_01, shelf_B12)"]))
    assert not world.holds("on(drill_01, shelf_B12)")


def test_attribute_updates_flow_into_derived_predicates(world: WorldModel) -> None:
    assert world.holds("shelf_has_free_space(shelf_B12)")
    world.apply_delta(WorldStateDelta(attributes={"shelf_B12.free_space": False}))
    assert not world.holds("shelf_has_free_space(shelf_B12)")


def test_set_attribute_rejects_an_unknown_entity(world: WorldModel) -> None:
    with pytest.raises(WorldModelError, match="unknown entity"):
        world.set_attribute("nowhere.free_space", False)


def test_set_attribute_requires_a_dotted_path(world: WorldModel) -> None:
    with pytest.raises(WorldModelError, match="entity_id.attribute"):
        world.set_attribute("shelf_B12", False)


# -- queries -------------------------------------------------------------
def test_resolve_location_walks_containment(world: WorldModel) -> None:
    """The drill is inside a box that is in an area, and that is where it is."""
    assert world.resolve_location("drill_01") == world.resolve_location("box_07")
    assert world.resolve_location("drill_01") in world.locations


def test_resolve_location_terminates_on_a_containment_loop(world: WorldModel) -> None:
    world.objects["box_07"].location = "drill_01"
    world.objects["drill_01"].location = "box_07"
    assert world.resolve_location("drill_01") is None


def test_mission_location_uses_the_station_id_and_map_revision(world: WorldModel) -> None:
    mission = world.mission_location("shelf_B12")
    payload = mission.to_fleet_payload()
    assert payload["mapId"] == world.fleet_map_id
    assert payload["stationId"]
    assert {"stationId", "mapId", "x", "y"} <= set(payload)


def test_mission_location_rejects_an_unknown_location(world: WorldModel) -> None:
    with pytest.raises(WorldModelError, match="unknown location"):
        world.mission_location("nowhere")


def test_distance_between_penalizes_a_floor_change(world: WorldModel) -> None:
    same_floor = world.distance_between("maintenance_area", "handover_station_02")
    cross_floor = world.distance_between("maintenance_area", "shelf_B12")
    assert cross_floor > same_floor


def test_copy_isolates_hypothetical_changes(world: WorldModel) -> None:
    clone = world.copy()
    clone.assert_all(["state(box_07, open)"])
    clone.objects["box_07"].mass_kg = 999.0

    assert world.holds("state(box_07, closed)")
    assert world.objects["box_07"].mass_kg != 999.0


def test_snapshot_records_verification_provenance(world: WorldModel) -> None:
    world.apply_delta(
        WorldStateDelta(asserted=["on(drill_01, shelf_B12)"], source="BACKEND_CLAIM")
    )
    snapshot = world.snapshot()
    entry = next(
        fact for fact in snapshot["facts"] if fact["predicate"] == "on(drill_01, shelf_B12)"
    )
    assert entry["verified"] is False
    assert entry["source"] == "BACKEND_CLAIM"


def test_summary_excludes_raw_telemetry(world: WorldModel) -> None:
    """The interpreter gets meaning, not joint velocities."""
    summary = world.summary()
    assert set(summary) == {"locations", "objects", "robots", "known_facts"}
    for robot in summary["robots"]:
        assert set(robot) <= {"id", "location", "availability", "battery_percent"}


def test_set_robot_state_records_an_observed_position(world: WorldModel) -> None:
    world.set_robot_state("b2w_z1_01", location="handover_station_02", battery_percent=55.0)
    assert world.is_verified("at(b2w_z1_01, handover_station_02)")
    assert world.robots["b2w_z1_01"].battery_percent == 55.0
