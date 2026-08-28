"""Fleet Control execution backend.

Work reaches robots as Fleet Control work orders. Fleet Control owns vendor
adapters, traffic, charging, and the final robot allocation; the orchestrator
contributes the task decomposition, the capability reasoning, and the physical
verification that a work order alone cannot provide.

Transport nodes map onto the existing ``CreateWorkOrderRequest`` unchanged.
Manipulation and perception nodes use an additive extension,
``brash.manipulation-work-order/v1``, which Fleet Control does not implement yet.
The backend probes for it at startup rather than assuming: if the extension is
absent, manipulation is refused with ``BACKEND_UNSUPPORTED`` instead of being
submitted as something that would quietly execute as a transport. See
``docs/INTEGRATION_NOTES.md`` for the exact Fleet Control changes required.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib import error as urllib_error
from urllib import request as urllib_request

from brash_orchestrator_core.backends.base import (
    BackendHealth,
    ExecutionBackend,
    PrimitiveRequest,
    PrimitiveResult,
)

MANIPULATION_EXTENSION = "brash.manipulation-work-order/v1"

#: Work-order statuses that end the poll loop.
_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

#: Fleet Control's own structured error codes. These are checked before any prose
#: matching, because a code is an enum the fleet controls and a failure reason is
#: free text that happens to mention words.
_FLEET_ERROR_CODES: Mapping[str, str] = {
    "CAPABILITY_UNKNOWN": "PRECONDITION_FAILED",
    "MISSION_CAPABILITY_REQUIRED": "PRECONDITION_FAILED",
    "MISSION_MAP_MISMATCH": "PRECONDITION_FAILED",
    "CONTRACT_VALIDATION_FAILED": "PRECONDITION_FAILED",
    "AUTHENTICATION_REQUIRED": "PRECONDITION_FAILED",
    "ROUTE_NOT_FOUND": "BACKEND_UNSUPPORTED",
    "WORK_ORDER_NOT_FOUND": "UNKNOWN_FAILURE",
    "ROBOT_UNAVAILABLE": "PRECONDITION_FAILED",
    "ALLOCATION_FAILED": "PRECONDITION_FAILED",
}

#: Filler words removed before substring matching, so "the lid is not accessible"
#: and "lid_not_accessible" reduce to the same thing. Fleet failure reasons are
#: written for a human reading an alarm, not for a parser.
_FILLER_WORDS = frozenset({"the", "a", "an", "is", "are", "was", "were", "has", "have", "been"})

#: Fleet Control failure text mapped onto the shared error-code set. Matching is
#: done on normalized substrings because failure reasons are human-readable
#: prose on the fleet side, not an enum.
_FAILURE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("lid_not_accessible", "LID_NOT_ACCESSIBLE"),
    ("lid not accessible", "LID_NOT_ACCESSIBLE"),
    ("lid not reachable", "LID_NOT_ACCESSIBLE"),
    ("target_occupied", "TARGET_OCCUPIED"),
    ("no free space", "TARGET_OCCUPIED"),
    ("occupied", "TARGET_OCCUPIED"),
    ("object_pose_uncertain", "OBJECT_POSE_UNCERTAIN"),
    ("pose uncertain", "OBJECT_POSE_UNCERTAIN"),
    ("grasp_failed", "GRASP_FAILED"),
    ("grasp failed", "GRASP_FAILED"),
    ("slip", "SLIP_DETECTED"),
    ("object_not_found", "OBJECT_NOT_FOUND"),
    ("not found", "OBJECT_NOT_FOUND"),
    ("path_blocked", "PATH_BLOCKED"),
    ("route", "PATH_BLOCKED"),
    ("blocked", "PATH_BLOCKED"),
    ("traffic", "PATH_BLOCKED"),
    ("payload", "PAYLOAD_LIMIT_EXCEEDED"),
    ("human", "HUMAN_IN_SAFETY_ZONE"),
    ("safety zone", "HUMAN_IN_SAFETY_ZONE"),
    ("offline", "COMMUNICATION_LOST"),
    ("heartbeat", "COMMUNICATION_LOST"),
    ("unreachable", "COMMUNICATION_LOST"),
    ("timeout", "TIMEOUT"),
    ("timed out", "TIMEOUT"),
    ("quarantine", "ROBOT_FAULT"),
    ("fault", "ROBOT_FAULT"),
    ("no eligible robot", "PRECONDITION_FAILED"),
    ("capability", "PRECONDITION_FAILED"),
    ("precondition", "PRECONDITION_FAILED"),
)


class FleetControlError(RuntimeError):
    """Raised when Fleet Control is unreachable or rejects a request."""


def map_failure_reason(reason: str | None) -> str:
    """Translate a Fleet Control failure reason into a shared error code."""
    if not reason:
        return "UNKNOWN_FAILURE"
    text = str(reason).strip().lower()
    from brash_orchestrator_core.backends.base import ERROR_CODES

    # An exact code, which the manipulation extension returns directly.
    exact = text.upper().replace(" ", "_")
    if exact in ERROR_CODES:
        return exact
    for token in re.split(r"[^A-Za-z_]+", str(reason)):
        if token.upper() in ERROR_CODES:
            return token.upper()
        if token.upper() in _FLEET_ERROR_CODES:
            return _FLEET_ERROR_CODES[token.upper()]

    normalized = " ".join(
        word for word in re.split(r"[^a-z0-9_]+", text) if word and word not in _FILLER_WORDS
    )
    for pattern, code in _FAILURE_PATTERNS:
        if pattern in normalized or pattern in text:
            return code
    return "UNKNOWN_FAILURE"


@dataclass
class FleetControlSettings:
    base_url: str = "http://127.0.0.1:4273"
    site_id: str = "plant-a"
    token: str = "brash-dev-operator"
    poll_interval_s: float = 0.5
    request_timeout_s: float = 15.0
    priority: int = 60
    verify_tls: bool = True

    @classmethod
    def from_env(cls) -> FleetControlSettings:
        return cls(
            base_url=os.environ.get("ORCH_FLEET_BASE_URL", "http://127.0.0.1:4273").rstrip("/"),
            site_id=os.environ.get("ORCH_FLEET_SITE", "plant-a"),
            token=os.environ.get("ORCH_FLEET_TOKEN", "brash-dev-operator"),
            poll_interval_s=float(os.environ.get("ORCH_FLEET_POLL_INTERVAL_S", "0.5")),
            request_timeout_s=float(os.environ.get("ORCH_FLEET_REQUEST_TIMEOUT_S", "15")),
            priority=int(os.environ.get("ORCH_FLEET_PRIORITY", "60")),
        )


class FleetControlBackend(ExecutionBackend):
    name = "fleet_control"

    def __init__(self, settings: FleetControlSettings | None = None) -> None:
        self.settings = settings or FleetControlSettings.from_env()
        self._session: Any | None = None
        self._health: BackendHealth | None = None
        #: node external reference -> fleet work-order id, for cancellation.
        self._submitted: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _headers(self, correlation_id: str = "") -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.token}",
            "X-Fleet-Site": self.settings.site_id,
        }
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        return headers

    async def _ensure_session(self) -> Any:
        if self._session is None:
            import aiohttp

            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.settings.request_timeout_s)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _call(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        correlation_id: str = "",
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self.settings.base_url}{path}"
        try:
            session = await self._ensure_session()
        except ImportError:
            # aiohttp is a declared dependency, but a stdlib fallback keeps the
            # backend usable in a minimal ROS install where it is missing.
            return await asyncio.to_thread(
                _call_blocking, method, url, self._headers(correlation_id), body,
                self.settings.request_timeout_s,
            )

        try:
            async with session.request(
                method, url, json=dict(body) if body is not None else None,
                headers=self._headers(correlation_id),
            ) as response:
                text = await response.text()
                return response.status, _parse_json(text)
        except asyncio.TimeoutError as error:
            raise FleetControlError(f"{method} {url} timed out") from error
        except Exception as error:  # noqa: BLE001 - aiohttp raises a wide family
            raise FleetControlError(f"{method} {url} failed: {error}") from error

    # ------------------------------------------------------------------
    # Health and capability probing
    # ------------------------------------------------------------------
    async def health(self, *, refresh: bool = False) -> BackendHealth:
        if self._health is not None and not refresh:
            return self._health

        try:
            status, document = await self._call("GET", "/api/v1/health")
        except FleetControlError as error:
            self._health = BackendHealth(reachable=False, detail=str(error))
            return self._health

        if status != 200:
            self._health = BackendHealth(
                reachable=False, detail=f"health returned HTTP {status}"
            )
            return self._health

        extensions = frozenset(
            str(item) for item in (document.get("extensions") or document.get("features") or [])
        )
        kinds = {"transport"}
        detail = f"Fleet Control reachable at {self.settings.base_url}"
        if MANIPULATION_EXTENSION in extensions:
            kinds |= {"manipulation", "perception"}
        else:
            detail += (
                f"; {MANIPULATION_EXTENSION} is not advertised, so manipulation and "
                "perception nodes cannot be executed. Run the bundled fleet stub, or "
                "add the extension to Fleet Control (see docs/INTEGRATION_NOTES.md)."
            )
        self._health = BackendHealth(
            reachable=True,
            detail=detail,
            supported_kinds=frozenset(kinds),
            extensions=extensions,
        )
        return self._health

    async def check_preconditions(self, request: PrimitiveRequest) -> tuple[bool, str]:
        health = await self.health()
        if not health.reachable:
            return False, health.detail
        if not health.supports_kind(request.kind):
            return False, (
                f"Fleet Control does not support {request.kind} work orders "
                f"({MANIPULATION_EXTENSION} missing)"
            )
        return True, ""

    # ------------------------------------------------------------------
    # Work-order construction
    # ------------------------------------------------------------------
    def build_work_order(self, request: PrimitiveRequest) -> dict[str, Any]:
        """Render a primitive as a Fleet Control work-order request body."""
        pickup = request.pickup or request.station
        dropoff = request.dropoff or request.station or request.pickup
        if pickup is None or dropoff is None:
            raise FleetControlError(
                f"{request.name} has no station to submit; a work order needs a map location"
            )
        if pickup.map_id != dropoff.map_id:
            # Fleet Control rejects this too, but failing here names the node.
            raise FleetControlError(
                f"{request.name} spans map revisions {pickup.map_id} and {dropoff.map_id}"
            )

        capabilities = list(request.fleet_capabilities) or ["material_transport"]
        body: dict[str, Any] = {
            "name": f"{request.primitive} for {request.task_id}/{request.node_id}",
            "externalReference": request.external_reference,
            "priority": request.priority,
            "requiredCapabilities": capabilities,
            "payloadKg": round(request.payload_kg, 3),
            "pickup": pickup.to_fleet_payload(),
            "dropoff": dropoff.to_fleet_payload(),
            # A preference, never a binding. Fleet Control's mission contract
            # already defines this field and its dispatch policy weights it, but
            # its work-order parser currently drops it, so on an unextended fleet
            # this is accepted and ignored rather than honoured. See
            # docs/INTEGRATION_NOTES.md.
            "preferredRobotId": request.robot_id,
        }

        if request.kind == "transport":
            return body

        # brash.manipulation-work-order/v1: additive fields only.
        body["contract"] = MANIPULATION_EXTENSION
        body["kind"] = "MANIPULATION"
        manipulation: dict[str, Any] = {
            "primitive": request.primitive,
            "readOnly": request.kind == "perception",
            "timeoutSeconds": round(request.timeout_s, 1),
        }
        if request.verification:
            manipulation["verification"] = request.verification
        if request.object_id:
            manipulation["objectId"] = request.object_id
        if request.container_id:
            manipulation["containerId"] = request.container_id
        if request.grasp_region:
            manipulation["graspRegion"] = request.grasp_region
        extra = {
            key: value
            for key, value in request.parameters.items()
            if key not in {"object_id", "container_id"}
        }
        if extra:
            manipulation["parameters"] = extra
        body["manipulation"] = manipulation
        return body

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    async def execute(self, request: PrimitiveRequest) -> PrimitiveResult:
        permitted, reason = await self.check_preconditions(request)
        if not permitted:
            return PrimitiveResult.failed("BACKEND_UNSUPPORTED", detail=reason)

        try:
            body = self.build_work_order(request)
        except FleetControlError as error:
            return PrimitiveResult.failed("PRECONDITION_FAILED", detail=str(error))

        try:
            status, document = await self._call(
                "POST", "/api/v1/work-orders", body=body,
                correlation_id=request.external_reference,
            )
        except FleetControlError as error:
            return PrimitiveResult.failed("COMMUNICATION_LOST", detail=str(error))

        if status not in (200, 201):
            code = _map_http_error(status, document)
            return PrimitiveResult.failed(
                code, detail=f"work-order submission returned HTTP {status}: {_error_text(document)}"
            )

        work_order_id = str(document.get("workOrderId") or document.get("id") or "")
        if not work_order_id:
            return PrimitiveResult.failed(
                "UNKNOWN_FAILURE", detail="Fleet Control returned no work-order id"
            )
        self._submitted[request.external_reference] = work_order_id

        return await self._await_completion(request, work_order_id)

    async def _await_completion(
        self, request: PrimitiveRequest, work_order_id: str
    ) -> PrimitiveResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.timeout_s
        last_document: dict[str, Any] = {}

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._cancel_work_order(work_order_id, "orchestrator timeout")
                return PrimitiveResult.failed(
                    "TIMEOUT",
                    detail=f"{request.name} exceeded {request.timeout_s:.0f}s",
                    external_reference=work_order_id,
                    metrics={"duration_s": request.timeout_s},
                )

            try:
                status, last_document = await self._call(
                    "GET", f"/api/v1/work-orders/{work_order_id}",
                    correlation_id=request.external_reference,
                )
            except FleetControlError as error:
                return PrimitiveResult.failed(
                    "COMMUNICATION_LOST", detail=str(error), external_reference=work_order_id
                )
            if status == 404:
                return PrimitiveResult.failed(
                    "UNKNOWN_FAILURE",
                    detail=f"work order {work_order_id} disappeared",
                    external_reference=work_order_id,
                )

            state = str(last_document.get("status", "")).upper()
            if state in _TERMINAL_STATUSES:
                return self._to_result(request, work_order_id, last_document, state)

            await asyncio.sleep(min(self.settings.poll_interval_s, max(remaining, 0.01)))

    def _to_result(
        self,
        request: PrimitiveRequest,
        work_order_id: str,
        document: Mapping[str, Any],
        state: str,
    ) -> PrimitiveResult:
        observed = dict(document.get("observedEffects") or document.get("observations") or {})
        metrics = {
            key: float(value)
            for key, value in (document.get("metrics") or {}).items()
            if isinstance(value, (int, float))
        }
        selected_robot = document.get("selectedRobotId")
        if selected_robot:
            observed.setdefault("selected_robot_id", selected_robot)
            # Fleet dispatch may substitute an eligible robot for the one the
            # scheduler preferred. Recording both is what makes the substitution
            # visible instead of silent.
            if selected_robot != request.robot_id:
                observed.setdefault("preferred_robot_id", request.robot_id)

        if state == "SUCCEEDED":
            self._add_transport_observation(request, document, observed)
            return PrimitiveResult.succeeded(
                observed_effects=observed, metrics=metrics, external_reference=work_order_id
            )
        if state == "CANCELLED":
            return PrimitiveResult.failed(
                "CANCELLED",
                detail=str(document.get("failureReason") or "cancelled by the fleet"),
                observed_effects=observed,
                metrics=metrics,
                external_reference=work_order_id,
            )
        reason = document.get("errorCode") or document.get("failureReason")
        return PrimitiveResult.failed(
            map_failure_reason(reason),
            detail=str(document.get("failureReason") or reason or "work order failed"),
            observed_effects=observed,
            metrics=metrics,
            external_reference=work_order_id,
        )

    def _add_transport_observation(
        self,
        request: PrimitiveRequest,
        document: Mapping[str, Any],
        observed: dict[str, Any],
    ) -> None:
        """Treat a completed transport as evidence the object arrived.

        Unlike a manipulation claim, this is grounded in a different system's
        measurement: Fleet Control only completes a transport mission after its
        own localization puts the robot at the drop-off station and the release
        step finishes. So the arrival predicate is genuinely observed, and it is
        recorded with a confidence below 1.0 to reflect that the fleet
        localization, not the orchestrator's perception, is the source.

        Manipulation and perception get no such treatment. Their evidence has to
        come from the executor reporting a confirmed predicate.
        """
        if request.kind != "transport" or not request.verification:
            return
        if "verified_predicates" in observed:
            return
        reported_station = (document.get("dropoff") or {}).get("stationId")
        expected_station = request.dropoff.station_id if request.dropoff else None
        if not reported_station or reported_station != expected_station:
            return
        observed["verified_predicates"] = [request.verification]
        observed.setdefault("confidence", 0.9)
        observed.setdefault("evidence", "fleet mission completion at the drop-off station")

    # ------------------------------------------------------------------
    # Cancellation and state
    # ------------------------------------------------------------------
    async def cancel(self, request: PrimitiveRequest) -> None:
        work_order_id = self._submitted.get(request.external_reference)
        if work_order_id:
            await self._cancel_work_order(work_order_id, "cancelled by the orchestrator")

    async def _cancel_work_order(self, work_order_id: str, reason: str) -> None:
        try:
            await self._call(
                "POST",
                f"/api/v1/work-orders/{work_order_id}/cancel",
                body={"reason": reason, "operatorId": "brash-orchestrator"},
            )
        except FleetControlError:
            # A cancellation that cannot be delivered must not mask the original
            # failure; the fleet's own recovery and alarms take over.
            pass

    async def emergency_stop(self) -> None:
        for work_order_id in list(self._submitted.values()):
            await self._cancel_work_order(work_order_id, "emergency stop requested")

    async def robot_states(self) -> dict[str, dict[str, Any]]:
        try:
            status, document = await self._call("GET", "/api/v1/fleet/snapshot")
        except FleetControlError:
            return {}
        if status != 200:
            return {}
        states: dict[str, dict[str, Any]] = {}
        for entry in document.get("robots") or []:
            if not isinstance(entry, Mapping):
                continue
            robot_id = entry.get("robotId") or entry.get("robot_id")
            if not robot_id:
                continue
            pose = entry.get("pose") or {}
            states[str(robot_id)] = {
                "availability": str(entry.get("availability", "")).lower(),
                "connection": str(entry.get("connection", "")).lower(),
                "battery_percent": float(entry.get("batteryPercent", 100.0)),
                "active_mission_id": entry.get("activeMissionId"),
                "x": pose.get("x"),
                "y": pose.get("y"),
            }
        return states


def _parse_json(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:500]}
    return document if isinstance(document, dict) else {"raw": document}


def _error_text(document: Mapping[str, Any]) -> str:
    error = document.get("error")
    if isinstance(error, Mapping):
        return f"{error.get('code', '')} {error.get('message', '')}".strip()
    return str(document)[:300]


def _map_http_error(status: int, document: Mapping[str, Any]) -> str:
    error = document.get("error")
    code = str(error.get("code", "")) if isinstance(error, Mapping) else ""
    message = str(error.get("message", "")) if isinstance(error, Mapping) else ""
    if status in (401, 403):
        return "PRECONDITION_FAILED"
    if status == 404:
        return "BACKEND_UNSUPPORTED"
    if status >= 500:
        return "COMMUNICATION_LOST"
    # The structured code decides; the prose message is only a fallback, because
    # a rejection message naming a capability or a payload field would otherwise
    # be misread as a capability or payload failure.
    if code:
        mapped = map_failure_reason(code)
        if mapped != "UNKNOWN_FAILURE":
            return mapped
    mapped = map_failure_reason(message)
    return mapped if mapped != "UNKNOWN_FAILURE" else "PRECONDITION_FAILED"


def _call_blocking(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Mapping[str, Any] | None,
    timeout_s: float,
) -> tuple[int, dict[str, Any]]:
    """Stdlib fallback used only when aiohttp is unavailable."""
    data = json.dumps(dict(body)).encode("utf-8") if body is not None else None
    http_request = urllib_request.Request(url, data=data, method=method)
    for key, value in headers.items():
        http_request.add_header(key, value)
    try:
        with urllib_request.urlopen(http_request, timeout=timeout_s) as response:
            return response.status, _parse_json(response.read().decode("utf-8"))
    except urllib_error.HTTPError as error:
        return error.code, _parse_json(error.read().decode("utf-8", errors="replace"))
    except urllib_error.URLError as error:
        raise FleetControlError(f"{method} {url} failed: {error.reason}") from error
