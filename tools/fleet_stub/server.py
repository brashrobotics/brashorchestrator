"""A Fleet Control stand-in that implements the target work-order contract.

This exists so the orchestrator can be exercised end to end against the contract
it is written for, including the ``brash.manipulation-work-order/v1`` extension
that Fleet Control has not adopted yet. It is not a simulator of robot physics
and it is not a substitute for Fleet Control: it is the smallest server that
answers the same HTTP surface, so integration tests drive the real HTTP client on
the real code path rather than a mock.

Fault injection is the other reason it exists. Recovery is most of the value of
the orchestrator, and recovery cannot be tested against a system that always
succeeds. Faults are declared per primitive and consumed in order, so a test can
say "the first attempt to open the box reports the lid is not accessible, the
second succeeds" and get exactly that.

Run it standalone:

    python -m tools.fleet_stub --port 4373

Endpoints, matching Fleet Control's ``/api/v1`` surface:

======================================================  =============================
``GET  /api/v1/health``                                  advertises the extension
``GET  /api/v1/ready``                                   readiness
``GET  /api/v1/fleet/snapshot``                          robot and work-order state
``POST /api/v1/work-orders``                             create, idempotent
``GET  /api/v1/work-orders``                             list
``GET  /api/v1/work-orders/{id}``                        read one
``POST /api/v1/work-orders/{id}/cancel``                 cancel
``POST /control/v1/faults``                              declare injected faults
``POST /control/v1/reset``                               clear all state
======================================================  =============================
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

MANIPULATION_EXTENSION = "brash.manipulation-work-order/v1"
FLEET_SCHEMA_VERSION = "1.0"

DEFAULT_PORT = 4373

#: Canonical Fleet Control actions, plus the manipulation names the extension
#: would add. A work order asking for anything else is rejected the way Fleet
#: Control would reject an unknown capability.
CANONICAL_ACTIONS = frozenset(
    {
        "pallet_transport",
        "cart_transport",
        "tote_transport",
        "material_transport",
        "inspection",
        "inventory_scan",
        "docking",
        "charging",
    }
)
MANIPULATION_ACTIONS = frozenset(
    {
        "open_container",
        "close_container",
        "pick_tool_from_container",
        "place_object_on_shelf",
        "reposition_container",
        "verify_object_pose",
        "detect_tool_in_container",
        "locate_object",
        "inspect_target_shelf",
        "request_teleoperation",
    }
)


@dataclass
class FaultInjection:
    """One scripted failure for a primitive."""

    primitive: str
    error_code: str
    remaining: int = 1
    observations: dict[str, Any] = field(default_factory=dict)
    node_id: str | None = None
    """When set, only the matching task node is affected."""


def parse_fault_request(body: Mapping[str, Any]) -> list[FaultInjection]:
    """Read a fault declaration from an HTTP body.

    Deliberately forgiving about shape -- one fault or a list, camelCase or
    snake_case -- and deliberately strict about declaring nothing. A control
    surface that accepts a typo and returns 200 produces a green run that proves
    nothing, which is the one outcome a fault injector must never cause.
    """
    raw_faults = body.get("faults")
    if raw_faults is None:
        raw_faults = [body] if body else []
    elif isinstance(raw_faults, Mapping):
        raw_faults = [raw_faults]
    if not isinstance(raw_faults, list) or not raw_faults:
        raise ValueError(
            "declare at least one fault, as an object or as a 'faults' list of "
            "{primitive, errorCode, remaining?, observations?, nodeId?}"
        )

    def field(raw: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if name in raw:
                return raw[name]
        return None

    faults: list[FaultInjection] = []
    for index, raw in enumerate(raw_faults):
        if not isinstance(raw, Mapping):
            raise ValueError(f"faults[{index}] must be an object")
        primitive = field(raw, "primitive")
        error_code = field(raw, "errorCode", "error_code")
        if not primitive:
            raise ValueError(f"faults[{index}] needs a 'primitive'")
        if not error_code:
            raise ValueError(f"faults[{index}] needs an 'errorCode'")
        faults.append(
            FaultInjection(
                primitive=str(primitive),
                error_code=str(error_code),
                remaining=int(field(raw, "remaining") or 1),
                observations=dict(field(raw, "observations") or {}),
                node_id=field(raw, "nodeId", "node_id"),
            )
        )
    return faults


@dataclass
class StubRobot:
    robot_id: str
    availability: str = "AVAILABLE"
    connection: str = "ONLINE"
    battery_percent: float = 90.0
    x: float = 0.0
    y: float = 0.0
    actions: frozenset[str] = field(default_factory=lambda: CANONICAL_ACTIONS | MANIPULATION_ACTIONS)
    max_payload_kg: float = 12.0


@dataclass
class WorkOrder:
    work_order_id: str
    request: dict[str, Any]
    status: str = "PENDING"
    selected_robot_id: str | None = None
    failure_reason: str | None = None
    error_code: str | None = None
    observed_effects: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def primitive(self) -> str:
        manipulation = self.request.get("manipulation") or {}
        return str(manipulation.get("primitive") or "transport")

    @property
    def external_reference(self) -> str | None:
        return self.request.get("externalReference")

    def to_view(self) -> dict[str, Any]:
        view: dict[str, Any] = {
            "workOrderId": self.work_order_id,
            "name": self.request.get("name"),
            "externalReference": self.external_reference,
            "status": self.status,
            "priority": self.request.get("priority", 50),
            "requiredCapabilities": self.request.get("requiredCapabilities", []),
            "payloadKg": self.request.get("payloadKg", 0.0),
            "pickup": self.request.get("pickup"),
            "dropoff": self.request.get("dropoff"),
            "selectedRobotId": self.selected_robot_id,
            "workflowId": f"wf-{self.work_order_id}",
            "allocationAttemptCount": 1,
            "failureReason": self.failure_reason,
            "observedEffects": self.observed_effects,
            "metrics": self.metrics,
        }
        if self.error_code:
            view["errorCode"] = self.error_code
        if "manipulation" in self.request:
            view["kind"] = self.request.get("kind", "MANIPULATION")
            view["manipulation"] = self.request["manipulation"]
        return view


class FleetStub:
    """In-memory Fleet Control behaviour, independent of HTTP."""

    def __init__(
        self,
        *,
        robots: Mapping[str, StubRobot] | None = None,
        execution_delay_s: float = 0.0,
        support_manipulation: bool = True,
    ) -> None:
        self.execution_delay_s = execution_delay_s
        self.support_manipulation = support_manipulation
        self.work_orders: dict[str, WorkOrder] = {}
        self.by_reference: dict[str, str] = {}
        self.faults: list[FaultInjection] = []
        self.submissions: list[dict[str, Any]] = []
        self.robots: dict[str, StubRobot] = dict(
            robots
            or {
                "b2w_z1_01": StubRobot("b2w_z1_01", x=2.0, y=3.0, max_payload_kg=12.0),
                "g1_brainco_01": StubRobot("g1_brainco_01", x=41.0, y=12.0, max_payload_kg=3.0),
            }
        )
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def extensions(self) -> list[str]:
        return [MANIPULATION_EXTENSION] if self.support_manipulation else []

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "schemaVersion": FLEET_SCHEMA_VERSION,
            "siteId": "plant-a",
            "extensions": self.extensions,
        }

    def add_fault(self, fault: FaultInjection) -> None:
        with self._lock:
            self.faults.append(fault)

    def reset(self) -> None:
        with self._lock:
            self.work_orders.clear()
            self.by_reference.clear()
            self.faults.clear()
            self.submissions.clear()

    # ------------------------------------------------------------------
    def create_work_order(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        problem = self._validate(body)
        if problem is not None:
            return 400, {"error": problem}

        reference = body.get("externalReference")
        with self._lock:
            # Fleet Control returns the existing order for a repeated reference,
            # which is what makes retries safe for an ERP integration.
            if reference and reference in self.by_reference:
                existing = self.work_orders[self.by_reference[reference]]
                return 200, existing.to_view()

            work_order = WorkOrder(
                work_order_id=f"wo-{uuid.uuid4().hex[:12]}", request=dict(body)
            )
            self.work_orders[work_order.work_order_id] = work_order
            if reference:
                self.by_reference[str(reference)] = work_order.work_order_id
            self.submissions.append(dict(body))

        if self.execution_delay_s > 0:
            # Resolve on a timer so the order really is observable as PENDING,
            # which is what makes orchestrator-side timeout and cancellation
            # testable rather than theoretical.
            timer = threading.Timer(self.execution_delay_s, self._allocate_and_run, (work_order,))
            timer.daemon = True
            timer.start()
            return 201, work_order.to_view()

        self._allocate_and_run(work_order)
        return 201, work_order.to_view()

    def _validate(self, body: Mapping[str, Any]) -> dict[str, Any] | None:
        for required in ("name", "requiredCapabilities", "pickup", "dropoff"):
            if required not in body:
                return {
                    "code": "CONTRACT_VALIDATION_FAILED",
                    "message": f"{required} is required",
                }
        capabilities = body.get("requiredCapabilities") or []
        if not capabilities:
            return {
                "code": "MISSION_CAPABILITY_REQUIRED",
                "message": "A mission requires at least one capability.",
            }
        pickup, dropoff = body.get("pickup") or {}, body.get("dropoff") or {}
        if pickup.get("mapId") != dropoff.get("mapId"):
            return {
                "code": "MISSION_MAP_MISMATCH",
                "message": "Pickup and drop-off must use the same map version.",
            }
        is_manipulation = "manipulation" in body or body.get("kind") == "MANIPULATION"
        if is_manipulation and not self.support_manipulation:
            return {
                "code": "CONTRACT_VALIDATION_FAILED",
                "message": f"unsupported field 'manipulation'; {MANIPULATION_EXTENSION} not enabled",
            }
        unknown = [
            capability
            for capability in capabilities
            if capability not in CANONICAL_ACTIONS and capability not in MANIPULATION_ACTIONS
        ]
        if unknown:
            return {
                "code": "CAPABILITY_UNKNOWN",
                "message": f"unknown capabilities: {', '.join(sorted(unknown))}",
            }
        return None

    def _allocate_and_run(self, work_order: WorkOrder) -> None:
        """Allocate a robot, then resolve the order to a terminal state.

        Real Fleet Control does this asynchronously through a worker with leases.
        With no configured delay this resolves inline, which keeps tests
        deterministic; with a delay it resolves on a timer so the order is
        genuinely observable as unfinished. The orchestrator polls the same way
        either way because it cannot tell the difference.
        """
        with self._lock:
            if work_order.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                # A cancellation landed while the timer was pending.
                return
            robot_id, reason = self._select_robot(work_order)
            if robot_id is None:
                work_order.status = "FAILED"
                work_order.failure_reason = reason
                work_order.error_code = "PRECONDITION_FAILED"
                return

            work_order.selected_robot_id = robot_id
            work_order.status = "RUNNING"

            fault = self._consume_fault(work_order)
            if fault is not None:
                work_order.status = "FAILED"
                work_order.error_code = fault.error_code
                work_order.failure_reason = (
                    f"injected fault {fault.error_code} on {work_order.primitive}"
                )
                work_order.observed_effects = dict(fault.observations)
                return

            work_order.status = "SUCCEEDED"
            work_order.error_code = "SUCCESS"
            work_order.observed_effects = self._observations_for(work_order)
            work_order.metrics = {"duration_s": max(self.execution_delay_s, 0.01)}
            self._move_robot(work_order, robot_id)

    def _select_robot(self, work_order: WorkOrder) -> tuple[str | None, str]:
        preferred = work_order.request.get("preferredRobotId")
        capabilities = set(work_order.request.get("requiredCapabilities") or [])
        payload = float(work_order.request.get("payloadKg") or 0.0)

        def eligible(robot: StubRobot) -> bool:
            return (
                robot.availability == "AVAILABLE"
                and robot.connection == "ONLINE"
                and capabilities <= robot.actions
                and payload <= robot.max_payload_kg
            )

        # The orchestrator's choice is a preference; it wins when still eligible.
        if preferred and preferred in self.robots and eligible(self.robots[preferred]):
            return str(preferred), ""
        for robot_id in sorted(self.robots):
            if eligible(self.robots[robot_id]):
                return robot_id, ""
        return None, "no eligible robot for the requested capabilities and payload"

    def _consume_fault(self, work_order: WorkOrder) -> FaultInjection | None:
        node_id = _node_id_from_reference(work_order.external_reference)
        for fault in self.faults:
            if fault.remaining <= 0 or fault.primitive != work_order.primitive:
                continue
            if fault.node_id is not None and fault.node_id != node_id:
                continue
            fault.remaining -= 1
            return fault
        return None

    def _observations_for(self, work_order: WorkOrder) -> dict[str, Any]:
        """Report what a perception-capable executor would have seen.

        The orchestrator refuses to treat a bare success as evidence, so an
        executor that reports no observation leaves the effect unverified and the
        task is not marked complete. A manipulation work order names the
        predicate it wants confirmed in ``manipulation.verification``; echoing it
        here is the stub standing in for the perception step that a real
        manipulation-capable robot would perform.

        ``verification_confirmed: false`` in a fault's observations reproduces
        the awkward real case where an action completes mechanically but nothing
        can confirm the result.
        """
        manipulation = work_order.request.get("manipulation") or {}
        observations: dict[str, Any] = {
            "confidence": 0.95,
            "primitive": work_order.primitive,
            "station_id": (work_order.request.get("dropoff") or {}).get("stationId"),
        }
        verification = manipulation.get("verification")
        if verification:
            resolved = str(verification).replace("assigned_robot", str(work_order.selected_robot_id))
            observations["verified_predicates"] = [resolved]
        return observations

    def _move_robot(self, work_order: WorkOrder, robot_id: str) -> None:
        dropoff = work_order.request.get("dropoff") or {}
        robot = self.robots.get(robot_id)
        if robot is not None and "x" in dropoff and "y" in dropoff:
            robot.x = float(dropoff["x"])
            robot.y = float(dropoff["y"])

    # ------------------------------------------------------------------
    def get_work_order(self, work_order_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            work_order = self.work_orders.get(work_order_id)
        if work_order is None:
            return 404, {"error": {"code": "WORK_ORDER_NOT_FOUND", "message": work_order_id}}
        return 200, work_order.to_view()

    def list_work_orders(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            views = [order.to_view() for order in self.work_orders.values()]
        return 200, {"workOrders": views}

    def cancel_work_order(self, work_order_id: str) -> tuple[int, dict[str, Any]]:
        with self._lock:
            work_order = self.work_orders.get(work_order_id)
            if work_order is None:
                return 404, {"error": {"code": "WORK_ORDER_NOT_FOUND", "message": work_order_id}}
            if work_order.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                work_order.status = "CANCELLED"
                work_order.failure_reason = "cancelled"
            return 200, work_order.to_view()

    def snapshot(self) -> tuple[int, dict[str, Any]]:
        with self._lock:
            robots = [
                {
                    "schemaVersion": FLEET_SCHEMA_VERSION,
                    "robotId": robot.robot_id,
                    "availability": robot.availability,
                    "connection": robot.connection,
                    "batteryPercent": robot.battery_percent,
                    "pose": {"x": robot.x, "y": robot.y, "theta": 0.0},
                    "activeMissionId": None,
                }
                for robot in sorted(self.robots.values(), key=lambda item: item.robot_id)
            ]
            orders = [order.to_view() for order in self.work_orders.values()]
        return 200, {"robots": robots, "workOrders": orders, "missions": []}


def _node_id_from_reference(reference: str | None) -> str | None:
    """Recover the node id from ``brash-task:{task}:{node}:{attempt}``."""
    if not reference:
        return None
    parts = str(reference).split(":")
    return parts[2] if len(parts) >= 3 else None


class _Handler(BaseHTTPRequestHandler):
    server_version = "BrashFleetStub/0.1"
    stub: FleetStub

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(format, *args)

    # -- helpers --------------------------------------------------------
    def _respond(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> tuple[dict[str, Any] | None, str]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}, ""
        raw = self.rfile.read(length)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            return None, str(error)
        return (document, "") if isinstance(document, dict) else (None, "body must be an object")

    def _authorized(self) -> bool:
        # Present so the orchestrator's header handling is genuinely exercised,
        # not to provide security. This server must never face a real network.
        return bool(self.headers.get("Authorization")) and bool(self.headers.get("X-Fleet-Site"))

    # -- routes ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        stub = self.server.stub  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path in ("/api/v1/health", "/api/v1/ready"):
            self._respond(200, stub.health())
            return
        if not self._authorized():
            self._respond(401, {"error": {"code": "AUTHENTICATION_REQUIRED", "message": "missing credentials"}})
            return
        if path == "/api/v1/fleet/snapshot":
            self._respond(*stub.snapshot())
            return
        if path == "/api/v1/work-orders":
            self._respond(*stub.list_work_orders())
            return
        if path.startswith("/api/v1/work-orders/"):
            self._respond(*stub.get_work_order(path.rsplit("/", 1)[-1]))
            return
        self._respond(404, {"error": {"code": "ROUTE_NOT_FOUND", "message": path}})

    def do_POST(self) -> None:  # noqa: N802
        stub = self.server.stub  # type: ignore[attr-defined]
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        body, problem = self._read_body()
        if body is None:
            self._respond(400, {"error": {"code": "INVALID_JSON", "message": problem}})
            return

        if path == "/control/v1/reset":
            stub.reset()
            self._respond(200, {"reset": True})
            return
        if path == "/control/v1/faults":
            try:
                declared = parse_fault_request(body)
            except ValueError as error:
                self._respond(
                    400, {"error": {"code": "CONTRACT_VALIDATION_FAILED", "message": str(error)}}
                )
                return
            for fault in declared:
                stub.add_fault(fault)
            self._respond(200, {"declared": len(declared), "faults": len(stub.faults)})
            return

        if not self._authorized():
            self._respond(401, {"error": {"code": "AUTHENTICATION_REQUIRED", "message": "missing credentials"}})
            return
        if path == "/api/v1/work-orders":
            self._respond(*stub.create_work_order(body))
            return
        if path.startswith("/api/v1/work-orders/") and path.endswith("/cancel"):
            work_order_id = path[len("/api/v1/work-orders/") : -len("/cancel")]
            self._respond(*stub.cancel_work_order(work_order_id))
            return
        self._respond(404, {"error": {"code": "ROUTE_NOT_FOUND", "message": path}})


class FleetStubServer:
    """Threaded HTTP wrapper, usable as a context manager in tests."""

    def __init__(
        self,
        stub: FleetStub | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        verbose: bool = False,
    ) -> None:
        self.stub = stub or FleetStub()
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.stub = self.stub  # type: ignore[attr-defined]
        self._server.verbose = verbose  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[0], self.port
        return f"http://{host}:{port}"

    def start(self) -> FleetStubServer:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fleet-stub", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> FleetStubServer:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()


def serve(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fleet_stub",
        description="Fleet Control stand-in implementing the target work-order contract.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="seconds each work order takes, for exercising timeouts",
    )
    parser.add_argument(
        "--no-manipulation",
        action="store_true",
        help=f"withhold {MANIPULATION_EXTENSION} to reproduce today's Fleet Control",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    stub = FleetStub(
        execution_delay_s=args.delay, support_manipulation=not args.no_manipulation
    )
    server = FleetStubServer(stub, host=args.host, port=args.port, verbose=args.verbose)
    server.start()
    print(f"fleet stub listening on {server.base_url}")
    print(f"  manipulation extension: {'enabled' if stub.support_manipulation else 'disabled'}")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.stop()
    return 0
