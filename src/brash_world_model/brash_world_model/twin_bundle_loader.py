"""Ground world-model locations against a published Twin Studio map bundle.

BrashTwinStudio publishes an immutable ``brash.twin-map-bundle/v2`` containing a
navigation topology and a release identity such as ``demo-plant@1``. Fleet
Control's ``mapId`` uses that same ``{family}@{revision}`` shape, so one bundle
grounds both sides: symbolic location identifiers gain real coordinates, and
every work order carries a map revision the fleet recognises.

Grounding also buys a cheap correctness check. Fleet Control rejects a mission
whose pickup and drop-off sit on different map revisions; catching that here
means the operator sees a validation error instead of a rejected work order
halfway through a plan.

The reader is deliberately tolerant about key spelling because the bundle is
produced by a separate repository on its own release cadence. It is strict about
the things that matter: a bundle must declare a map identity, and a node must
have usable coordinates.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from brash_world_model.model import WorldModel

TWIN_MAP_BUNDLE_CONTRACT = "brash.twin-map-bundle/v2"

_MANIFEST_NAMES = ("manifest.json",)
_TOPOLOGY_NAMES = ("topology.json", "navigation-topology.json")

_MAP_ID_KEYS = ("mapId", "map_id", "releaseId", "release_id", "release", "id")
_NODE_COLLECTION_KEYS = ("nodes", "stations", "waypoints")
_NODE_ID_KEYS = ("nodeId", "node_id", "stationId", "station_id", "id", "name")
_FLOOR_KEYS = ("floor", "level", "floorId", "floor_id")


class TwinBundleError(RuntimeError):
    """Raised when a bundle is missing, malformed, or unversioned."""


@dataclass(frozen=True, slots=True)
class TwinStation:
    """One navigable node from the bundle topology."""

    station_id: str
    x: float
    y: float
    floor: int = 0
    zone_id: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass
class TwinBundle:
    map_id: str
    stations: dict[str, TwinStation] = field(default_factory=dict)
    source_path: str = ""

    def lookup(self, identifier: str) -> TwinStation | None:
        """Find a station by identifier, alias, or normalized name."""
        direct = self.stations.get(identifier)
        if direct is not None:
            return direct
        wanted = _normalize(identifier)
        for station in self.stations.values():
            if _normalize(station.station_id) == wanted:
                return station
            if any(_normalize(alias) == wanted for alias in station.aliases):
                return station
        return None


def _normalize(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _first_present(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise TwinBundleError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise TwinBundleError(f"{path} must contain a JSON object")
    return document


def _resolve_paths(bundle_path: Path) -> tuple[Path, Path | None]:
    if bundle_path.is_file():
        manifest = bundle_path
        directory = bundle_path.parent
    else:
        directory = bundle_path
        manifest = next(
            (directory / name for name in _MANIFEST_NAMES if (directory / name).is_file()),
            Path(),
        )
        if not manifest.is_file():
            raise TwinBundleError(f"no manifest.json under {bundle_path}")
    topology = next(
        (directory / name for name in _TOPOLOGY_NAMES if (directory / name).is_file()), None
    )
    return manifest, topology


def _extract_coordinates(raw: Mapping[str, Any]) -> tuple[float, float] | None:
    if "x" in raw and "y" in raw:
        return float(raw["x"]), float(raw["y"])
    for key in ("position", "pose", "point", "coordinates"):
        value = raw.get(key)
        if isinstance(value, Mapping) and "x" in value and "y" in value:
            return float(value["x"]), float(value["y"])
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return float(value[0]), float(value[1])
    return None


#: A signed integer inside a floor label. The lookbehind is what keeps the
#: hyphen in "level-1" a separator rather than a minus sign, while still reading
#: a genuinely negative "-1" for a basement.
_FLOOR_NUMBER = re.compile(r"(?<![A-Za-z0-9])-?\d+")


def _parse_floor(raw: Mapping[str, Any]) -> int:
    value = _first_present(raw, _FLOOR_KEYS)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        # Twin Studio allows named floors such as "level-1" or "B1".
        found = _FLOOR_NUMBER.search(str(value))
        return int(found.group()) if found else 0


def _collect_nodes(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in _NODE_COLLECTION_KEYS:
        value = document.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            merged: list[Mapping[str, Any]] = []
            for node_id, item in value.items():
                if isinstance(item, Mapping):
                    merged.append({**item, "nodeId": item.get("nodeId", node_id)})
            return merged
    return []


def load_twin_bundle(bundle_path: str | Path) -> TwinBundle:
    """Read a Twin Map Bundle directory or manifest file."""
    path = Path(bundle_path)
    if not path.exists():
        raise TwinBundleError(f"twin bundle not found: {path}")

    manifest_path, topology_path = _resolve_paths(path)
    manifest = _read_json(manifest_path)

    contract = str(manifest.get("contract") or manifest.get("contractVersion") or "")
    if contract and not contract.startswith("brash.twin-map-bundle/"):
        raise TwinBundleError(
            f"{manifest_path} declares contract {contract!r}, expected {TWIN_MAP_BUNDLE_CONTRACT}"
        )

    map_id = _first_present(manifest, _MAP_ID_KEYS)
    if map_id is None:
        raise TwinBundleError(
            f"{manifest_path} declares no map identity; a work order cannot name a map revision"
        )

    topology_document: Mapping[str, Any] = manifest
    if topology_path is not None:
        topology_document = _read_json(topology_path)

    nodes = _collect_nodes(topology_document) or _collect_nodes(manifest)
    stations: dict[str, TwinStation] = {}
    for raw in nodes:
        station_id = _first_present(raw, _NODE_ID_KEYS)
        coordinates = _extract_coordinates(raw)
        if station_id is None or coordinates is None:
            continue
        aliases = tuple(
            str(alias)
            for key in ("aliases", "names", "labels")
            for alias in (raw.get(key) or [])
            if isinstance(alias, (str, int))
        )
        name = raw.get("name")
        if isinstance(name, str) and name != station_id:
            aliases = aliases + (name,)
        stations[str(station_id)] = TwinStation(
            station_id=str(station_id),
            x=coordinates[0],
            y=coordinates[1],
            floor=_parse_floor(raw),
            zone_id=raw.get("zoneId") or raw.get("zone_id"),
            aliases=aliases,
        )

    return TwinBundle(map_id=str(map_id), stations=stations, source_path=str(path))


def ground_world_model(model: WorldModel, bundle: TwinBundle) -> list[str]:
    """Overwrite scenario coordinates with bundle truth.

    Returns the location identifiers the bundle did not know. Those stay usable
    for planning but are reported so an operator can see that part of the
    scenario is not backed by a surveyed map.
    """
    model.fleet_map_id = bundle.map_id
    ungrounded: list[str] = []
    for location_id, location in model.locations.items():
        station = bundle.lookup(location.station_id or location_id)
        if station is None:
            location.grounded = False
            ungrounded.append(location_id)
            continue
        location.station_id = station.station_id
        location.x = station.x
        location.y = station.y
        location.floor = station.floor
        location.zone_id = station.zone_id or location.zone_id
        location.grounded = True
    return sorted(ungrounded)
