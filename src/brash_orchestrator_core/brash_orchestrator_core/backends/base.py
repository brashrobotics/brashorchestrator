"""The execution backend contract.

Every robot command in the system leaves through this interface. Keeping it
narrow is the point: a backend executes one validated primitive and reports the
outcome honestly. It makes no planning decision, never chooses a robot, and
never decides that a task is complete.

This is the reference architecture's ``RobotAdapter`` contract, restated for a
fleet-delegated deployment. Vendor adapters live in Fleet Control, so the
orchestrator needs exactly one implementation of this interface rather than one
per robot, and adding a robot never touches orchestrator code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from brash_world_model.model import MissionLocation

#: Error codes a backend may report. Mirrors the execution event contract.
ERROR_CODES = (
    "SUCCESS",
    "PRECONDITION_FAILED",
    "OBJECT_NOT_FOUND",
    "OBJECT_POSE_UNCERTAIN",
    "GRASP_FAILED",
    "SLIP_DETECTED",
    "PATH_BLOCKED",
    "TARGET_OCCUPIED",
    "LID_NOT_ACCESSIBLE",
    "PAYLOAD_LIMIT_EXCEEDED",
    "HUMAN_IN_SAFETY_ZONE",
    "COMMUNICATION_LOST",
    "TIMEOUT",
    "ROBOT_FAULT",
    "UNKNOWN_FAILURE",
    "CANCELLED",
    "BACKEND_UNSUPPORTED",
)


@dataclass(frozen=True, slots=True)
class PrimitiveRequest:
    """One validated, assigned, ready-to-execute primitive."""

    task_id: str
    node_id: str
    action_id: str
    primitive: str
    kind: str
    robot_id: str
    parameters: Mapping[str, Any]
    timeout_s: float
    fleet_capabilities: tuple[str, ...] = ()
    pickup: MissionLocation | None = None
    dropoff: MissionLocation | None = None
    station: MissionLocation | None = None
    payload_kg: float = 0.0
    object_id: str | None = None
    container_id: str | None = None
    grasp_region: str | None = None
    priority: int = 50
    attempt: int = 1
    verification: str | None = None
    """The ground predicate the executing system is asked to confirm.

    Sending it makes verification the executor's explicit job rather than
    something the orchestrator infers from a bare success. An executor that
    cannot confirm it reports success without it, and the effect stays
    unverified.
    """

    @property
    def external_reference(self) -> str:
        """Stable identity for idempotent submission and correlation.

        Including the attempt number matters: a retry after a failure is a new
        piece of work, and reusing the reference would make the fleet return the
        original failed order instead of running again.
        """
        return f"brash-task:{self.task_id}:{self.node_id}:{self.attempt}"

    @property
    def name(self) -> str:
        return f"{self.task_id}/{self.node_id} {self.primitive}"


@dataclass(frozen=True, slots=True)
class PrimitiveResult:
    """What actually happened, as reported by the executing system."""

    success: bool
    error_code: str
    observed_effects: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, float] = field(default_factory=dict)
    external_reference: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.success and self.error_code != "SUCCESS":
            raise ValueError("a successful result must carry error_code SUCCESS")
        if not self.success and self.error_code == "SUCCESS":
            raise ValueError("a failed result must carry a non-SUCCESS error_code")

    @classmethod
    def succeeded(
        cls,
        *,
        observed_effects: Mapping[str, Any] | None = None,
        metrics: Mapping[str, float] | None = None,
        external_reference: str | None = None,
        detail: str = "",
    ) -> PrimitiveResult:
        return cls(
            success=True,
            error_code="SUCCESS",
            observed_effects=dict(observed_effects or {}),
            metrics=dict(metrics or {}),
            external_reference=external_reference,
            detail=detail,
        )

    @classmethod
    def failed(
        cls,
        error_code: str,
        *,
        detail: str = "",
        observed_effects: Mapping[str, Any] | None = None,
        metrics: Mapping[str, float] | None = None,
        external_reference: str | None = None,
    ) -> PrimitiveResult:
        return cls(
            success=False,
            error_code=error_code if error_code != "SUCCESS" else "UNKNOWN_FAILURE",
            observed_effects=dict(observed_effects or {}),
            metrics=dict(metrics or {}),
            external_reference=external_reference,
            detail=detail,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "error_code": self.error_code,
            "observed_effects": dict(self.observed_effects),
            "metrics": dict(self.metrics),
            "external_reference": self.external_reference,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class BackendHealth:
    """What the backend can do right now."""

    reachable: bool
    detail: str = ""
    supported_kinds: frozenset[str] = frozenset()
    extensions: frozenset[str] = frozenset()

    def supports_kind(self, kind: str) -> bool:
        return kind in self.supported_kinds

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "detail": self.detail,
            "supported_kinds": sorted(self.supported_kinds),
            "extensions": sorted(self.extensions),
        }


class ExecutionBackend(ABC):
    """Executes validated primitives and reports results honestly."""

    name: str = "backend"

    @abstractmethod
    async def health(self) -> BackendHealth:
        """Probe reachability and which execution kinds are supported."""

    @abstractmethod
    async def execute(self, request: PrimitiveRequest) -> PrimitiveResult:
        """Execute one primitive and wait for its terminal outcome."""

    @abstractmethod
    async def cancel(self, request: PrimitiveRequest) -> None:
        """Request a safe cancellation of in-flight work."""

    async def check_preconditions(self, request: PrimitiveRequest) -> tuple[bool, str]:
        """Backend-local feasibility check before execution.

        The orchestrator has already checked the symbolic world state; this is
        the executing system's own veto, which is the one that knows about live
        traffic, quarantine, and payload reality.
        """
        return True, ""

    async def robot_states(self) -> dict[str, dict[str, Any]]:
        """Current robot states keyed by fleet robot id."""
        return {}

    async def emergency_stop(self) -> None:
        """Request the fastest safe stop the executing system offers.

        This is never the primary emergency stop. The physical stop path is
        independent of this software and must remain so.
        """

    async def close(self) -> None:
        """Release transport resources."""
