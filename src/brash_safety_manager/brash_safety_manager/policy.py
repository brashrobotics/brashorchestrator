"""Safety policy enforcement, independent of the LLM.

Every rule here is evaluated from typed configuration and world state. The
language model is never consulted, is never shown these limits as negotiable,
and cannot relax them: a plan that violates a rule is refused at submission
time, which is after the model has had its say and before a robot moves.

What this is not: a safety-certified system. It cannot be, because it is
software in the planning path with no hardware guarantees. The certified
low-level controller, the safety scanners, and the physical emergency stop
remain the actual safety system. This layer exists so that an obviously unsafe
command is not even offered to them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

import yaml


class _Request(Protocol):
    """The parts of a primitive request the safety manager reads."""

    task_id: str
    node_id: str
    action_id: str
    primitive: str
    kind: str
    robot_id: str
    payload_kg: float


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    permitted: bool
    error_code: str
    detail: str
    rule: str = ""

    def as_tuple(self) -> tuple[bool, str, str]:
        return self.permitted, self.error_code, self.detail

    def to_dict(self) -> dict[str, Any]:
        return {
            "permitted": self.permitted,
            "error_code": self.error_code,
            "detail": self.detail,
            "rule": self.rule,
        }


ALLOWED = SafetyVerdict(True, "SUCCESS", "", rule="")


@dataclass
class SafetyPolicy:
    enabled: bool = True
    global_max_payload_kg: float = 15.0
    human_clearance_m: Mapping[str, float] = field(default_factory=dict)
    forbidden_zones: frozenset[str] = frozenset()
    approval_zones: frozenset[str] = frozenset()
    restricted_zones: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    approval_required_primitives: frozenset[str] = frozenset()
    min_battery_percent: float = 20.0
    max_concurrent_primitives: int = 4

    @classmethod
    def load(cls, path: str | Path | None = None) -> SafetyPolicy:
        if path is None:
            from brash_orchestrator_core.paths import config_path

            path = config_path("safety.yaml")
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(document, Mapping):
            raise ValueError(f"{path} is not a mapping")
        zones = document.get("zones") or {}
        return cls(
            enabled=bool(document.get("enabled", True)),
            global_max_payload_kg=float(document.get("global_max_payload_kg", 15.0)),
            human_clearance_m=dict(document.get("human_clearance_m") or {}),
            forbidden_zones=frozenset(zones.get("forbidden") or ()),
            approval_zones=frozenset(zones.get("approval_required") or ()),
            restricted_zones={
                str(zone): tuple(primitives or ())
                for zone, primitives in (zones.get("restricted") or {}).items()
            },
            approval_required_primitives=frozenset(
                document.get("approval_required_primitives") or ()
            ),
            min_battery_percent=float(document.get("min_battery_percent", 20.0)),
            max_concurrent_primitives=int(document.get("max_concurrent_primitives", 4)),
        )


class SafetyManager:
    """Evaluates one primitive request against the policy."""

    def __init__(
        self,
        policy: SafetyPolicy | None = None,
        *,
        approvals: frozenset[str] | set[str] | None = None,
    ) -> None:
        self.policy = policy or SafetyPolicy()
        #: Approval tokens granted by an operator: primitive names, node ids, or
        #: zone ids. Granted out of band, never by a generated plan.
        self.approvals: set[str] = set(approvals or ())
        self.blocked: list[dict[str, Any]] = []
        self._in_flight = 0

    def grant(self, token: str) -> None:
        self.approvals.add(token)

    def revoke(self, token: str) -> None:
        self.approvals.discard(token)

    # ------------------------------------------------------------------
    def evaluate(self, request: _Request, world: Any) -> tuple[bool, str, str]:
        verdict = self.check(request, world)
        if not verdict.permitted:
            self.blocked.append(
                {
                    "task_id": request.task_id,
                    "node_id": request.node_id,
                    "primitive": request.primitive,
                    "robot_id": request.robot_id,
                    **verdict.to_dict(),
                }
            )
        return verdict.as_tuple()

    def check(self, request: _Request, world: Any) -> SafetyVerdict:
        if not self.policy.enabled:
            return ALLOWED

        for rule in (
            self._check_concurrency,
            self._check_payload,
            self._check_battery,
            self._check_zone,
            self._check_human_proximity,
            self._check_approval,
        ):
            verdict = rule(request, world)
            if not verdict.permitted:
                return verdict
        return ALLOWED

    # ------------------------------------------------------------------
    def _check_concurrency(self, request: _Request, world: Any) -> SafetyVerdict:
        if self._in_flight >= self.policy.max_concurrent_primitives:
            return SafetyVerdict(
                False,
                "SAFETY_POLICY_VIOLATION",
                f"{self._in_flight} primitives already in flight, limit is "
                f"{self.policy.max_concurrent_primitives}",
                rule="max_concurrent_primitives",
            )
        return ALLOWED

    def _check_payload(self, request: _Request, world: Any) -> SafetyVerdict:
        if request.payload_kg > self.policy.global_max_payload_kg:
            return SafetyVerdict(
                False,
                "PAYLOAD_LIMIT_EXCEEDED",
                f"payload {request.payload_kg:.1f}kg exceeds the site limit "
                f"{self.policy.global_max_payload_kg:.1f}kg",
                rule="global_max_payload_kg",
            )
        return ALLOWED

    def _check_battery(self, request: _Request, world: Any) -> SafetyVerdict:
        robot = getattr(world, "robots", {}).get(request.robot_id)
        if robot is None:
            return ALLOWED
        if robot.battery_percent < self.policy.min_battery_percent:
            return SafetyVerdict(
                False,
                "SAFETY_POLICY_VIOLATION",
                f"{request.robot_id} battery {robot.battery_percent:.0f}% is below the "
                f"safety floor {self.policy.min_battery_percent:.0f}%",
                rule="min_battery_percent",
            )
        return ALLOWED

    def _check_zone(self, request: _Request, world: Any) -> SafetyVerdict:
        for zone in self._zones_for(request, world):
            if zone in self.policy.forbidden_zones:
                return SafetyVerdict(
                    False,
                    "SAFETY_POLICY_VIOLATION",
                    f"zone {zone!r} is forbidden to orchestrated work",
                    rule="zones.forbidden",
                )
            allowed = self.policy.restricted_zones.get(zone)
            if allowed is not None and request.primitive not in allowed:
                return SafetyVerdict(
                    False,
                    "SAFETY_POLICY_VIOLATION",
                    f"zone {zone!r} allows only {', '.join(allowed)}, not "
                    f"{request.primitive!r}",
                    rule="zones.restricted",
                )
            if zone in self.policy.approval_zones and not self._approved(request, zone):
                return SafetyVerdict(
                    False,
                    "SAFETY_POLICY_VIOLATION",
                    f"zone {zone!r} requires an explicit operator approval",
                    rule="zones.approval_required",
                )
        return ALLOWED

    def _check_human_proximity(self, request: _Request, world: Any) -> SafetyVerdict:
        clearance = self.policy.human_clearance_m.get(request.kind)
        if clearance is None:
            return ALLOWED
        observed = self._human_distance(request, world)
        if observed is not None and observed < clearance:
            return SafetyVerdict(
                False,
                "HUMAN_IN_SAFETY_ZONE",
                f"a person is {observed:.1f}m away; {request.kind} requires "
                f"{clearance:.1f}m clearance",
                rule="human_clearance_m",
            )
        return ALLOWED

    def _check_approval(self, request: _Request, world: Any) -> SafetyVerdict:
        if request.primitive in self.policy.approval_required_primitives and not self._approved(
            request, request.primitive
        ):
            return SafetyVerdict(
                False,
                "SAFETY_POLICY_VIOLATION",
                f"{request.primitive!r} requires a named operator approval",
                rule="approval_required_primitives",
            )
        return ALLOWED

    # ------------------------------------------------------------------
    def _approved(self, request: _Request, token: str) -> bool:
        return bool(
            {token, request.primitive, request.node_id, f"{request.task_id}:{request.node_id}"}
            & self.approvals
        )

    def _zones_for(self, request: _Request, world: Any) -> list[str]:
        zones: list[str] = []
        for location in (
            getattr(request, "pickup", None),
            getattr(request, "dropoff", None),
            getattr(request, "station", None),
        ):
            zone_id = getattr(location, "zone_id", None)
            if zone_id:
                zones.append(str(zone_id))
        return zones

    def _human_distance(self, request: _Request, world: Any) -> float | None:
        """Nearest reported human distance for the acting robot.

        The world model carries this as an attribute rather than a predicate
        because it is a continuously changing measurement, not a fact to reason
        about symbolically.
        """
        robot = getattr(world, "robots", {}).get(request.robot_id)
        raw = getattr(robot, "human_distance_m", None) if robot is not None else None
        if raw is None:
            objects = getattr(world, "objects", {})
            person = objects.get(f"human_near_{request.robot_id}")
            raw = person.attributes.get("distance_m") if person is not None else None
        return float(raw) if isinstance(raw, (int, float)) else None

    # ------------------------------------------------------------------
    def acquire(self) -> None:
        self._in_flight += 1

    def release(self) -> None:
        self._in_flight = max(self._in_flight - 1, 0)
