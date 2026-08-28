"""Symbolic world state.

The world model holds what Fleet Control deliberately does not: objects,
containment, container state, and the predicates that make a goal verifiable.
Fleet Control owns robots, missions, and maps; this model owns meaning.

Two rules shape the design:

* A fact carries whether it was *observed* or merely *claimed*. A backend
  reporting success is not evidence, so effects land as unverified until an
  observation confirms them.
* Derived facts are never stored. ``object_exists`` and ``shelf_has_free_space``
  are answered by inspecting entities, so they cannot drift out of sync with the
  attributes they describe.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml

from brash_world_model.predicates import (
    NEGATED_PREDICATES,
    Predicate,
    PredicateError,
    is_derived,
    parse_predicate,
)

UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MissionLocation:
    """A location expressed the way Fleet Control needs it on a work order."""

    station_id: str
    map_id: str
    x: float
    y: float
    zone_id: str | None = None

    def to_fleet_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stationId": self.station_id,
            "mapId": self.map_id,
            "x": self.x,
            "y": self.y,
        }
        if self.zone_id:
            payload["zoneId"] = self.zone_id
        return payload


@dataclass
class Location:
    location_id: str
    type: str = "work_area"
    floor: int = 0
    station_id: str | None = None
    x: float = 0.0
    y: float = 0.0
    zone_id: str | None = None
    height_m: float | None = None
    free_space: bool = True
    sensor_coverage: bool = False
    grounded: bool = False
    """True once a Twin Studio map bundle supplied the coordinates."""


@dataclass
class WorldObject:
    object_id: str
    type: str
    subtype: str | None = None
    location: str | None = None
    relation: str = "at"
    mass_kg: float = 0.0
    state: str | None = None
    lid_type: str | None = None
    preferred_grasp_region: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class RobotRecord:
    robot_id: str
    location: str | None = None
    availability: str = "available"
    connection: str = "online"
    battery_percent: float = 100.0
    payload_object_ids: list[str] = field(default_factory=list)
    fault_code: str = ""


@dataclass(frozen=True, slots=True)
class Fact:
    predicate: Predicate
    verified: bool
    source: str
    confidence: float
    recorded_at: str


@dataclass(frozen=True, slots=True)
class PredicateContext:
    """Robot-dependent limits used when evaluating derived predicates."""

    assigned_robot: str | None = None
    payload_limit_kg: float | None = None
    min_reach_height_m: float | None = None
    max_reach_height_m: float | None = None


@dataclass
class WorldStateDelta:
    """An observed change. Only observations may mark a fact verified."""

    asserted: list[str] = field(default_factory=list)
    retracted: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    source: str = "OBSERVATION"
    confidence: float = 1.0
    task_id: str = ""
    node_id: str = ""

    @property
    def verified(self) -> bool:
        return self.source == "OBSERVATION"


class WorldModelError(RuntimeError):
    """Raised when a scenario or delta cannot be applied."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _as_bool(value: Any) -> bool | None:
    """Interpret a scenario tri-state where ``unknown`` means "not yet known"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() == UNKNOWN:
        return None
    return None if value is None else bool(value)


class WorldModel:
    """Mutable symbolic state for one site."""

    def __init__(self, *, fleet_map_id: str = "default@1") -> None:
        self.fleet_map_id = fleet_map_id
        self.locations: dict[str, Location] = {}
        self.objects: dict[str, WorldObject] = {}
        self.robots: dict[str, RobotRecord] = {}
        self.connected_floors: list[set[int]] = []
        self.blocked_locations: set[str] = set()
        self._facts: dict[Predicate, Fact] = {}
        self._history: list[WorldStateDelta] = []

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    @classmethod
    def from_scenario(cls, scenario: dict[str, Any]) -> WorldModel:
        model = cls(fleet_map_id=str(scenario.get("fleet_map_id", "default@1")))
        for location_id, raw in (scenario.get("locations") or {}).items():
            raw = raw or {}
            model.locations[location_id] = Location(
                location_id=location_id,
                type=str(raw.get("type", "work_area")),
                floor=int(raw.get("floor", 0)),
                station_id=raw.get("station_id"),
                x=float(raw.get("x", 0.0)),
                y=float(raw.get("y", 0.0)),
                zone_id=raw.get("zone_id"),
                height_m=raw.get("height_m"),
                free_space=bool(raw.get("free_space", True)),
                sensor_coverage=bool(raw.get("sensor_coverage", False)),
            )

        routes = scenario.get("routes") or {}
        model.connected_floors = [set(group) for group in routes.get("connected_floors", [])]
        model.blocked_locations = set(routes.get("blocked_locations", []))

        for object_id, raw in (scenario.get("objects") or {}).items():
            raw = dict(raw or {})
            known = {
                "type",
                "subtype",
                "location",
                "relation",
                "mass_kg",
                "state",
                "lid_type",
                "preferred_grasp_region",
                "contents",
            }
            model.objects[object_id] = WorldObject(
                object_id=object_id,
                type=str(raw.get("type", "object")),
                subtype=raw.get("subtype"),
                location=raw.get("location"),
                relation=str(raw.get("relation", "at")),
                mass_kg=float(raw.get("mass_kg", 0.0)),
                state=raw.get("state"),
                lid_type=raw.get("lid_type"),
                preferred_grasp_region=raw.get("preferred_grasp_region"),
                attributes={key: value for key, value in raw.items() if key not in known},
            )

        for robot_id, raw in (scenario.get("robots") or {}).items():
            raw = raw or {}
            model.robots[robot_id] = RobotRecord(
                robot_id=robot_id,
                location=raw.get("location"),
                availability=str(raw.get("availability", "available")),
                connection=str(raw.get("connection", "online")),
                battery_percent=float(raw.get("battery_percent", 100.0)),
                payload_object_ids=list(raw.get("payload_object_ids", [])),
            )

        model._seed_facts(scenario)
        return model

    @classmethod
    def load(cls, path: str | Path) -> WorldModel:
        text = Path(path).read_text(encoding="utf-8")
        scenario = yaml.safe_load(text) or {}
        if not isinstance(scenario, dict):
            raise WorldModelError(f"scenario at {path} is not a mapping")
        return cls.from_scenario(scenario)

    def _seed_facts(self, scenario: dict[str, Any]) -> None:
        """Turn scenario attributes into initial facts.

        Seeded facts are marked verified because a loaded scenario is the
        operator's declared ground truth for the start of a trial.
        """
        for object_id, world_object in self.objects.items():
            location = world_object.location
            if location in self.objects:
                self._store(Predicate("inside", (object_id, location)), True, "SCENARIO_LOAD", 1.0)
            elif location in self.locations:
                self._store(Predicate("at", (object_id, location)), True, "SCENARIO_LOAD", 1.0)

            if world_object.state:
                self._store(
                    Predicate("state", (object_id, world_object.state)), True, "SCENARIO_LOAD", 1.0
                )

            for flag in ("pose_known", "lid_accessible", "grasp_region_visible", "contents_visible"):
                raw_value = scenario.get("objects", {}).get(object_id, {}).get(flag)
                if _as_bool(raw_value):
                    self._store(Predicate(flag, (object_id,)), True, "SCENARIO_LOAD", 1.0)

            for content_id in scenario.get("objects", {}).get(object_id, {}).get("contents", []):
                self._store(Predicate("inside", (content_id, object_id)), True, "SCENARIO_LOAD", 1.0)

        for robot_id, robot in self.robots.items():
            if robot.location in self.locations:
                self._store(Predicate("at", (robot_id, robot.location)), True, "SCENARIO_LOAD", 1.0)
            for object_id in robot.payload_object_ids:
                self._store(Predicate("held_by", (object_id, robot_id)), True, "SCENARIO_LOAD", 1.0)

    # ------------------------------------------------------------------
    # Fact access
    # ------------------------------------------------------------------
    def _store(self, predicate: Predicate, verified: bool, source: str, confidence: float) -> None:
        existing = self._facts.get(predicate)
        # An observation always wins; a claim never downgrades an observation.
        if existing is not None and existing.verified and not verified:
            return
        self._facts[predicate] = Fact(
            predicate=predicate,
            verified=verified,
            source=source,
            confidence=confidence,
            recorded_at=_now(),
        )

    def facts(self) -> Iterator[Fact]:
        return iter(sorted(self._facts.values(), key=lambda fact: str(fact.predicate)))

    def fact(self, predicate: Predicate | str) -> Fact | None:
        return self._facts.get(self._coerce(predicate))

    def is_verified(self, predicate: Predicate | str) -> bool:
        found = self._facts.get(self._coerce(predicate))
        return found is not None and found.verified

    @staticmethod
    def _coerce(predicate: Predicate | str) -> Predicate:
        return predicate if isinstance(predicate, Predicate) else parse_predicate(predicate)

    def holds(
        self, predicate: Predicate | str, context: PredicateContext | None = None
    ) -> bool:
        """Evaluate a predicate, deriving it from entities where appropriate."""
        resolved = self._coerce(predicate)
        if is_derived(resolved):
            return self._evaluate_derived(resolved, context or PredicateContext())
        if resolved.name in NEGATED_PREDICATES:
            positive = Predicate(NEGATED_PREDICATES[resolved.name], resolved.args)
            return positive not in self._facts
        if resolved.name == "at_robot_near":
            # Navigation is part of every submitted work order, so proximity is
            # satisfiable whenever the object sits somewhere the fleet can route
            # to. Actual arrival is enforced by the backend, not by planning.
            return self._evaluate_derived(
                Predicate("robot_reachable", resolved.args), context or PredicateContext()
            )
        return resolved in self._facts

    def _evaluate_derived(self, predicate: Predicate, context: PredicateContext) -> bool:
        name, args = predicate.name, predicate.args
        try:
            if name == "object_exists":
                return args[0] in self.objects
            if name == "location_exists":
                return args[0] in self.locations
            if name == "object_type":
                world_object = self.objects.get(args[0])
                return world_object is not None and world_object.type == args[1]
            if name == "mass_within_limit":
                world_object = self.objects.get(args[0])
                if world_object is None:
                    return False
                if context.payload_limit_kg is None:
                    return True
                return world_object.mass_kg <= context.payload_limit_kg
            if name == "route_available":
                return self._route_available(args[0], context.assigned_robot)
            if name == "robot_reachable":
                target = self.resolve_location(args[0])
                return target is not None and self._route_available(target, context.assigned_robot)
            if name == "sensor_view_available":
                location = self.locations.get(args[0])
                return location is not None and location.sensor_coverage
            if name == "shelf_has_free_space":
                location = self.locations.get(args[0])
                return location is not None and location.free_space
            if name == "shelf_height_supported":
                location = self.locations.get(args[0])
                if location is None:
                    return False
                if location.height_m is None:
                    return True
                if context.min_reach_height_m is not None and location.height_m < context.min_reach_height_m:
                    return False
                if context.max_reach_height_m is not None and location.height_m > context.max_reach_height_m:
                    return False
                return True
        except IndexError as error:
            raise PredicateError(f"wrong arity for derived predicate {predicate}") from error
        raise PredicateError(f"no evaluator for derived predicate {predicate.name}")

    def _route_available(self, location_id: str, robot_id: str | None) -> bool:
        destination = self.locations.get(location_id)
        if destination is None or location_id in self.blocked_locations:
            return False
        if robot_id is None:
            return True
        robot = self.robots.get(robot_id)
        if robot is None or robot.location is None:
            return True
        origin = self.locations.get(robot.location)
        if origin is None or origin.floor == destination.floor:
            return True
        return any(
            origin.floor in group and destination.floor in group for group in self.connected_floors
        )

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def apply_delta(self, delta: WorldStateDelta) -> None:
        """Apply an observed or claimed change."""
        for text in delta.asserted:
            predicate = parse_predicate(text)
            if is_derived(predicate):
                raise WorldModelError(
                    f"{predicate.name} is derived from entity attributes and cannot be asserted"
                )
            if predicate.name in NEGATED_PREDICATES:
                self._retract_matching(Predicate(NEGATED_PREDICATES[predicate.name], predicate.args))
                continue
            self._apply_exclusivity(predicate)
            self._store(predicate, delta.verified, delta.source, delta.confidence)
            self._mirror_into_entities(predicate)

        for text in delta.retracted:
            self._retract_matching(parse_predicate(text))

        for path, value in delta.attributes.items():
            self.set_attribute(path, value)

        self._history.append(delta)

    def _apply_exclusivity(self, predicate: Predicate) -> None:
        """Drop facts that cannot coexist with the one being asserted."""
        exclusive_by_first_arg = {"at", "state", "held_by", "inside", "on"}
        if predicate.name not in exclusive_by_first_arg or not predicate.args:
            return
        subject = predicate.args[0]
        # An object is at exactly one place, in exactly one state, and either
        # held or not. Asserting a new value removes the stale one.
        groups = {
            "at": ("at", "on", "inside"),
            "on": ("at", "on", "inside"),
            "inside": ("at", "on", "inside"),
            "state": ("state",),
            "held_by": ("held_by",),
        }
        for name in groups[predicate.name]:
            for existing in [p for p in self._facts if p.name == name and p.args[:1] == (subject,)]:
                if existing != predicate:
                    del self._facts[existing]

    def _mirror_into_entities(self, predicate: Predicate) -> None:
        """Keep entity attributes consistent with location and state facts."""
        if predicate.name in {"at", "on", "inside"} and len(predicate.args) == 2:
            subject, target = predicate.args
            if subject in self.objects:
                self.objects[subject].location = target
                self.objects[subject].relation = predicate.name
            elif subject in self.robots:
                self.robots[subject].location = target
        elif predicate.name == "state" and len(predicate.args) == 2:
            if predicate.args[0] in self.objects:
                self.objects[predicate.args[0]].state = predicate.args[1]
        elif predicate.name == "held_by" and len(predicate.args) == 2:
            object_id, robot_id = predicate.args
            robot = self.robots.get(robot_id)
            if robot is not None and object_id not in robot.payload_object_ids:
                robot.payload_object_ids.append(object_id)

    def _retract_matching(self, predicate: Predicate) -> None:
        removed = [p for p in self._facts if p.name == predicate.name and p.args == predicate.args]
        if not removed and predicate.name == "held_by" and len(predicate.args) >= 1:
            # not_held(object) names only the object, so drop any holder.
            removed = [
                p
                for p in self._facts
                if p.name == "held_by" and p.args[:1] == predicate.args[:1]
            ]
        for found in removed:
            del self._facts[found]
            if found.name == "held_by" and len(found.args) == 2:
                robot = self.robots.get(found.args[1])
                if robot is not None and found.args[0] in robot.payload_object_ids:
                    robot.payload_object_ids.remove(found.args[0])

    def set_attribute(self, path: str, value: Any) -> None:
        """Update ``entity_id.attribute`` on a location, object, or robot."""
        entity_id, _, attribute = path.partition(".")
        if not attribute:
            raise WorldModelError(f"attribute path {path!r} must be entity_id.attribute")
        for registry in (self.locations, self.objects, self.robots):
            entity = registry.get(entity_id)
            if entity is None:
                continue
            if hasattr(entity, attribute):
                setattr(entity, attribute, value)
            elif isinstance(entity, WorldObject):
                entity.attributes[attribute] = value
            else:
                raise WorldModelError(f"{entity_id} has no attribute {attribute}")
            return
        raise WorldModelError(f"unknown entity {entity_id!r}")

    def set_robot_state(
        self,
        robot_id: str,
        *,
        availability: str | None = None,
        connection: str | None = None,
        battery_percent: float | None = None,
        location: str | None = None,
        fault_code: str | None = None,
    ) -> None:
        robot = self.robots.setdefault(robot_id, RobotRecord(robot_id=robot_id))
        if availability is not None:
            robot.availability = availability
        if connection is not None:
            robot.connection = connection
        if battery_percent is not None:
            robot.battery_percent = battery_percent
        if fault_code is not None:
            robot.fault_code = fault_code
        if location is not None:
            robot.location = location
            self._store(Predicate("at", (robot_id, location)), True, "OBSERVATION", 1.0)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def resolve_location(self, entity_id: str) -> str | None:
        """Walk containment until a real location is reached."""
        seen: set[str] = set()
        current: str | None = entity_id
        while current is not None and current not in seen:
            seen.add(current)
            if current in self.locations:
                return current
            entity = self.objects.get(current) or self.robots.get(current)
            if entity is None:
                return None
            current = entity.location
        return None

    def mission_location(self, location_id: str) -> MissionLocation:
        """Express a location the way a Fleet Control work order needs it."""
        location = self.locations.get(location_id)
        if location is None:
            raise WorldModelError(f"unknown location {location_id!r}")
        return MissionLocation(
            station_id=location.station_id or location_id,
            map_id=self.fleet_map_id,
            x=location.x,
            y=location.y,
            zone_id=location.zone_id,
        )

    def object_mass(self, object_id: str) -> float:
        world_object = self.objects.get(object_id)
        return world_object.mass_kg if world_object else 0.0

    def distance_between(self, origin_id: str | None, destination_id: str | None) -> float:
        """Straight-line distance in metres, used only to price repositioning."""
        origin = self.locations.get(self.resolve_location(origin_id or "") or "")
        destination = self.locations.get(self.resolve_location(destination_id or "") or "")
        if origin is None or destination is None:
            return 0.0
        floor_penalty = 20.0 * abs(origin.floor - destination.floor)
        return ((origin.x - destination.x) ** 2 + (origin.y - destination.y) ** 2) ** 0.5 + floor_penalty

    def snapshot(self) -> dict[str, Any]:
        """Full serializable state, used in trial records and task results."""
        return {
            "fleet_map_id": self.fleet_map_id,
            "facts": [
                {
                    "predicate": str(fact.predicate),
                    "verified": fact.verified,
                    "source": fact.source,
                    "confidence": fact.confidence,
                }
                for fact in self.facts()
            ],
            "locations": {
                location_id: {
                    "type": location.type,
                    "floor": location.floor,
                    "station_id": location.station_id,
                    "free_space": location.free_space,
                    "height_m": location.height_m,
                    "grounded": location.grounded,
                }
                for location_id, location in sorted(self.locations.items())
            },
            "objects": {
                object_id: {
                    "type": world_object.type,
                    "subtype": world_object.subtype,
                    "location": world_object.location,
                    "relation": world_object.relation,
                    "state": world_object.state,
                    "mass_kg": world_object.mass_kg,
                }
                for object_id, world_object in sorted(self.objects.items())
            },
            "robots": {
                robot_id: {
                    "location": robot.location,
                    "availability": robot.availability,
                    "connection": robot.connection,
                    "battery_percent": robot.battery_percent,
                    "payload_object_ids": list(robot.payload_object_ids),
                    "fault_code": robot.fault_code,
                }
                for robot_id, robot in sorted(self.robots.items())
            },
        }

    def summary(self) -> dict[str, Any]:
        """Short typed digest for the LLM prompt.

        Raw telemetry is deliberately excluded. The interpreter needs to know
        what exists and what is currently true, not how fast a joint is moving.
        """
        return {
            "locations": [
                {
                    "id": location_id,
                    "type": location.type,
                    "floor": location.floor,
                    "height_m": location.height_m,
                    "free_space": location.free_space,
                    "sensor_coverage": location.sensor_coverage,
                }
                for location_id, location in sorted(self.locations.items())
            ],
            "objects": [
                {
                    "id": object_id,
                    "type": world_object.type,
                    "subtype": world_object.subtype,
                    "location": world_object.location,
                    "relation": world_object.relation,
                    "state": world_object.state,
                    "mass_kg": world_object.mass_kg,
                    "pose_known": self.holds(Predicate("pose_known", (object_id,))),
                }
                for object_id, world_object in sorted(self.objects.items())
            ],
            "robots": [
                {
                    "id": robot_id,
                    "location": robot.location,
                    "availability": robot.availability,
                    "battery_percent": robot.battery_percent,
                }
                for robot_id, robot in sorted(self.robots.items())
            ],
            "known_facts": [str(fact.predicate) for fact in self.facts()],
        }

    def copy(self) -> WorldModel:
        """Deep-enough copy for hypothetical evaluation during validation."""
        clone = WorldModel(fleet_map_id=self.fleet_map_id)
        clone.locations = {key: replace(value) for key, value in self.locations.items()}
        clone.objects = {
            key: replace(value, attributes=dict(value.attributes))
            for key, value in self.objects.items()
        }
        clone.robots = {
            key: replace(value, payload_object_ids=list(value.payload_object_ids))
            for key, value in self.robots.items()
        }
        clone.connected_floors = [set(group) for group in self.connected_floors]
        clone.blocked_locations = set(self.blocked_locations)
        clone._facts = dict(self._facts)
        return clone

    def assert_all(self, predicates: Iterable[str], *, source: str = "OBSERVATION") -> None:
        self.apply_delta(WorldStateDelta(asserted=list(predicates), source=source))
