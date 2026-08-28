"""Grounding world-model locations against a Twin Studio map bundle.

The bundle is produced by a separate repository on its own release cadence, so
the reader is tolerant about key spelling and strict about map identity. Both
halves of that are asserted here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brash_world_model.model import WorldModel
from brash_world_model.twin_bundle_loader import (
    TwinBundleError,
    ground_world_model,
    load_twin_bundle,
)


def _write(directory: Path, name: str, document: dict) -> Path:
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        "manifest.json",
        {"contract": "brash.twin-map-bundle/v2", "mapId": "demo-plant@3"},
    )
    _write(
        tmp_path,
        "topology.json",
        {
            "nodes": [
                {"nodeId": "MAINT-01", "x": 9.25, "y": 4.75, "floor": 0, "zoneId": "workshop"},
                {"nodeId": "SHELF-B12", "position": {"x": 55.0, "y": 19.5}, "level": "level-1"},
                {"nodeId": "HANDOVER-02", "coordinates": [37.5, 15.0], "floor": 1},
            ]
        },
    )
    return tmp_path


def test_a_bundle_directory_is_read(bundle_dir: Path) -> None:
    bundle = load_twin_bundle(bundle_dir)
    assert bundle.map_id == "demo-plant@3"
    assert set(bundle.stations) == {"MAINT-01", "SHELF-B12", "HANDOVER-02"}


def test_a_manifest_file_path_is_accepted(bundle_dir: Path) -> None:
    bundle = load_twin_bundle(bundle_dir / "manifest.json")
    assert bundle.map_id == "demo-plant@3"


@pytest.mark.parametrize("station_id", ["MAINT-01", "SHELF-B12", "HANDOVER-02"])
def test_coordinates_are_read_from_any_of_the_accepted_shapes(
    bundle_dir: Path, station_id: str
) -> None:
    station = load_twin_bundle(bundle_dir).stations[station_id]
    assert station.x > 0 and station.y > 0


def test_a_named_floor_is_reduced_to_its_number(bundle_dir: Path) -> None:
    assert load_twin_bundle(bundle_dir).stations["SHELF-B12"].floor == 1


def test_lookup_is_insensitive_to_punctuation_and_case(bundle_dir: Path) -> None:
    bundle = load_twin_bundle(bundle_dir)
    assert bundle.lookup("maint01") is bundle.lookup("MAINT-01")


def test_a_bundle_without_a_map_identity_is_rejected(tmp_path: Path) -> None:
    """Without a map revision a work order cannot name a map, so this is fatal."""
    _write(tmp_path, "manifest.json", {"contract": "brash.twin-map-bundle/v2"})
    with pytest.raises(TwinBundleError, match="no map identity"):
        load_twin_bundle(tmp_path)


def test_a_foreign_contract_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "manifest.json", {"contract": "acme.map/v1", "mapId": "x@1"})
    with pytest.raises(TwinBundleError, match="expected brash.twin-map-bundle"):
        load_twin_bundle(tmp_path)


def test_a_missing_bundle_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(TwinBundleError, match="not found"):
        load_twin_bundle(tmp_path / "nope")


def test_a_directory_without_a_manifest_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TwinBundleError, match="no manifest.json"):
        load_twin_bundle(tmp_path)


def test_invalid_json_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(TwinBundleError, match="not valid JSON"):
        load_twin_bundle(tmp_path)


def test_a_node_without_coordinates_is_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "manifest.json", {"mapId": "x@1"})
    _write(tmp_path, "topology.json", {"nodes": [{"nodeId": "A"}, {"nodeId": "B", "x": 1, "y": 2}]})
    assert set(load_twin_bundle(tmp_path).stations) == {"B"}


# -- grounding ------------------------------------------------------------
def test_grounding_replaces_scenario_coordinates_with_surveyed_ones(
    world: WorldModel, bundle_dir: Path
) -> None:
    before = (world.locations["maintenance_area"].x, world.locations["maintenance_area"].y)
    ground_world_model(world, load_twin_bundle(bundle_dir))
    after = (world.locations["maintenance_area"].x, world.locations["maintenance_area"].y)

    assert after == (9.25, 4.75)
    assert after != before
    assert world.locations["maintenance_area"].grounded


def test_grounding_adopts_the_bundle_map_revision(
    world: WorldModel, bundle_dir: Path
) -> None:
    """Every work order then carries a map revision the fleet recognises."""
    assert world.fleet_map_id == "demo-plant@1"
    ground_world_model(world, load_twin_bundle(bundle_dir))

    assert world.fleet_map_id == "demo-plant@3"
    assert world.mission_location("shelf_B12").map_id == "demo-plant@3"


def test_locations_the_bundle_does_not_know_are_reported_not_dropped(
    world: WorldModel, bundle_dir: Path
) -> None:
    ungrounded = ground_world_model(world, load_twin_bundle(bundle_dir))

    assert "charging_area_b2w" in ungrounded
    # Still planable, just flagged as not backed by a surveyed map.
    assert "charging_area_b2w" in world.locations
    assert not world.locations["charging_area_b2w"].grounded


def test_grounding_carries_the_zone_through_to_the_work_order(
    world: WorldModel, bundle_dir: Path
) -> None:
    """Zone ids are what the safety manager evaluates zone rules against."""
    ground_world_model(world, load_twin_bundle(bundle_dir))
    assert world.mission_location("maintenance_area").zone_id == "workshop"


def test_grounding_updates_the_floor_used_for_route_checks(
    world: WorldModel, bundle_dir: Path
) -> None:
    ground_world_model(world, load_twin_bundle(bundle_dir))
    assert world.locations["shelf_B12"].floor == 1
    assert world.locations["maintenance_area"].floor == 0


def test_one_bundle_grounds_both_pickup_and_dropoff_on_the_same_revision(
    world: WorldModel, bundle_dir: Path
) -> None:
    """Fleet Control rejects a mission that straddles two map revisions."""
    ground_world_model(world, load_twin_bundle(bundle_dir))
    pickup = world.mission_location("maintenance_area")
    dropoff = world.mission_location("handover_station_02")
    assert pickup.map_id == dropoff.map_id
