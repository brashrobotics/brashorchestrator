"""HTTP and WebSocket surface for the operator console.

Thin by design. Every route either reads state off the :class:`TaskHub` or hands
it a request; no planning, scheduling, or safety logic lives here, because a
second implementation of any of those would be a second answer to a question that
must have one.

    GET  /                           the console
    GET  /api/health                 LLM profile, backend health, current task
    GET  /api/library                actions, robots, error codes, example goals
    GET  /api/world                  world model snapshot
    GET  /api/tasks                  recent tasks, newest first
    POST /api/tasks                  submit a goal
    GET  /api/tasks/{task_id}        one task with its events and full result
    POST /api/tasks/{task_id}/cancel cancel the running task
    POST /api/approvals              grant or revoke a safety approval token
    POST /api/faults                 declare a fault on the backend control surface
    POST /api/reset                  reload the scenario and clear faults
    GET  /api/stream                 WebSocket: snapshot then live events
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import WSMsgType, web

from brash_orchestrator_core.backends.fleet_control import FleetControlSettings
from brash_orchestrator_core.orchestrator import Orchestrator
from brash_orchestrator_core.web.hub import HubError, Subscriber, TaskHub

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Public so the console can be mounted inside a larger aiohttp application and
#: so tests can reach the hub through the app they built.
HUB = web.AppKey("hub", TaskHub)
OWNS_ORCHESTRATOR = web.AppKey("owns_orchestrator", bool)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# ----------------------------------------------------------------------
# Error handling
# ----------------------------------------------------------------------
@web.middleware
async def json_errors(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Turn refusals into the same problem shape the fleet contract uses."""
    try:
        return await handler(request)
    except HubError as error:
        if _is_upgrade(request):
            raise
        return _problem(error.code, str(error), error.status)
    except web.HTTPException:
        raise
    except Exception as error:  # noqa: BLE001 - a console reports, it does not 500 blindly
        if _is_upgrade(request):
            # The connection may already be an upgraded WebSocket, and writing an
            # HTTP body into one produces a frame the client can only read as
            # protocol corruption. Let it close instead.
            raise
        return _problem("INTERNAL_ERROR", f"{type(error).__name__}: {error}", 500)


def _is_upgrade(request: web.Request) -> bool:
    return request.headers.get("Upgrade", "").lower() == "websocket"


def _problem(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


async def _body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        document = await request.json()
    except (json.JSONDecodeError, ValueError) as error:
        raise HubError("INVALID_JSON", f"request body is not JSON: {error}") from error
    if not isinstance(document, dict):
        raise HubError("INVALID_JSON", "request body must be a JSON object")
    return document


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


async def get_health(request: web.Request) -> web.StreamResponse:
    return web.json_response(await request.app[HUB].health())


async def get_library(request: web.Request) -> web.StreamResponse:
    return web.json_response(request.app[HUB].library())


async def get_world(request: web.Request) -> web.StreamResponse:
    return web.json_response(request.app[HUB].world())


async def list_tasks(request: web.Request) -> web.StreamResponse:
    hub = request.app[HUB]
    return web.json_response({"tasks": hub.history(), "running_task_id": hub.running_task_id})


async def submit_task(request: web.Request) -> web.StreamResponse:
    body = await _body(request)
    hub = request.app[HUB]
    record = hub.submit(
        goal=body.get("goal", ""),
        mode=str(body.get("mode") or "execute"),
        constraints=_constraints(body.get("constraints") or {}),
        approvals=[str(token) for token in (body.get("approvals") or [])],
        task_id=body.get("task_id"),
    )
    return web.json_response(record.brief(), status=202)


async def get_task(request: web.Request) -> web.StreamResponse:
    record = request.app[HUB].task(request.match_info["task_id"])
    return web.json_response(record.to_dict())


async def cancel_task(request: web.Request) -> web.StreamResponse:
    record = await request.app[HUB].cancel(request.match_info["task_id"])
    return web.json_response(record.brief())


async def set_approval(request: web.Request) -> web.StreamResponse:
    body = await _body(request)
    approvals = request.app[HUB].set_approval(
        str(body.get("token", "")), bool(body.get("granted", True))
    )
    return web.json_response({"approvals": approvals})


async def inject_fault(request: web.Request) -> web.StreamResponse:
    result = await request.app[HUB].inject_fault(await _body(request))
    return web.json_response(result)


async def reset(request: web.Request) -> web.StreamResponse:
    return web.json_response(await request.app[HUB].reset())


def _constraints(raw: Any) -> dict[str, Any]:
    """Normalise the constraint block the browser sends.

    Only the constraints the pipeline understands are forwarded. Silently
    dropping an unrecognised key would be worse than useless here, so an unknown
    constraint is an error the operator sees.
    """
    if not isinstance(raw, dict):
        raise HubError("INVALID_CONSTRAINTS", "constraints must be an object")
    known = {
        "deadline_s": float,
        "max_risk": str,
        "allow_teleoperation": bool,
        "allowed_robot_ids": list,
        "dry_run": bool,
    }
    constraints: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in known:
            raise HubError(
                "UNKNOWN_CONSTRAINT",
                f"unknown constraint {key!r}; expected one of {', '.join(sorted(known))}",
            )
        if value in (None, "", [], False):
            continue
        try:
            constraints[key] = known[key](value)
        except (TypeError, ValueError) as error:
            raise HubError("INVALID_CONSTRAINTS", f"constraint {key!r}: {error}") from error
    if "max_risk" in constraints and constraints["max_risk"] not in ("low", "medium", "high"):
        raise HubError("INVALID_CONSTRAINTS", "max_risk must be low, medium, or high")
    if "allowed_robot_ids" in constraints:
        constraints["allowed_robot_ids"] = [str(item) for item in constraints["allowed_robot_ids"]]
    return constraints


# ----------------------------------------------------------------------
# Live stream
# ----------------------------------------------------------------------
async def stream(request: web.Request) -> web.StreamResponse:
    """Send a full snapshot, then every subsequent message as it happens.

    Snapshot first, then live, in that order and on the same connection: a client
    that polled for state separately could observe an event about a task it has
    not heard of yet.
    """
    hub = request.app[HUB]
    # Subscribe first, then build the snapshot. Anything that happens while the
    # snapshot is being assembled is queued rather than lost, so the client sees
    # the state and then every change to it with no gap in between.
    subscriber = hub.subscribe()
    try:
        snapshot = {"type": "snapshot", **await hub.snapshot()}
        # Upgrade only once nothing else can fail: after this point an error can
        # no longer be reported as HTTP, and writing one anyway would reach the
        # browser as protocol corruption.
        socket = web.WebSocketResponse(heartbeat=20.0, max_msg_size=0, compress=False)
        await socket.prepare(request)
        await socket.send_json(snapshot)

        pump = asyncio.create_task(_pump(socket, subscriber), name="brash-console-pump")
        try:
            async for message in socket:
                if message.type == WSMsgType.ERROR:
                    break
                if message.type == WSMsgType.TEXT and message.data.strip() == "ping":
                    await socket.send_json({"type": "pong"})
        finally:
            pump.cancel()
            await asyncio.wait({pump}, timeout=2.0)
            await socket.close()
        return socket
    finally:
        hub.unsubscribe(subscriber)


async def _pump(socket: web.WebSocketResponse, subscriber: Subscriber) -> None:
    while not socket.closed:
        message = await subscriber.queue.get()
        if subscriber.dropped:
            message = {**message, "dropped": subscriber.dropped}
            subscriber.dropped = 0
        await socket.send_json(message)


# ----------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------
def build_app(hub: TaskHub, *, owns_orchestrator: bool = False) -> web.Application:
    app = web.Application(middlewares=[json_errors])
    app[HUB] = hub
    app[OWNS_ORCHESTRATOR] = owns_orchestrator

    app.router.add_get("/", index)
    app.router.add_get("/api/health", get_health)
    app.router.add_get("/api/library", get_library)
    app.router.add_get("/api/world", get_world)
    app.router.add_get("/api/tasks", list_tasks)
    app.router.add_post("/api/tasks", submit_task)
    app.router.add_get("/api/tasks/{task_id}", get_task)
    app.router.add_post("/api/tasks/{task_id}/cancel", cancel_task)
    app.router.add_post("/api/approvals", set_approval)
    app.router.add_post("/api/faults", inject_fault)
    app.router.add_post("/api/reset", reset)
    app.router.add_get("/api/stream", stream)
    app.router.add_static("/static/", STATIC_DIR, name="static")

    app.on_shutdown.append(_shutdown)
    return app


async def _shutdown(app: web.Application) -> None:
    hub = app[HUB]
    await hub.close()
    if app[OWNS_ORCHESTRATOR]:
        await hub.orchestrator.close()


def serve(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    scenario_path: str | Path | None = None,
    twin_bundle_path: str | Path | None = None,
    llm_profile: str | None = None,
    fleet_settings: FleetControlSettings | None = None,
    record_trials: bool = True,
    approvals: tuple[str, ...] = (),
    orchestrator: Orchestrator | None = None,
) -> None:
    """Build an orchestrator if one was not supplied, then run the console."""
    owns = orchestrator is None
    active = orchestrator or Orchestrator.build(
        scenario_path=scenario_path,
        twin_bundle_path=twin_bundle_path,
        llm_profile=llm_profile,
        fleet_settings=fleet_settings,
        record_trials=record_trials,
        approvals=approvals,
    )
    hub = TaskHub(
        active, scenario_path=scenario_path, twin_bundle_path=twin_bundle_path
    )
    app = build_app(hub, owns_orchestrator=owns)

    print(f"BrashOrchestrator console on http://{host}:{port}")
    print(f"  LLM profile: {active.llm_settings.profile_name}")
    print(f"  execution backend: {active.backend.name}")
    if _is_exposed(host):
        # This surface can dispatch work orders to physical robots and it has no
        # authentication. Saying so is not optional.
        print(
            f"  WARNING: bound to {host}, which is reachable from the network. "
            "The console has no authentication and can command robots."
        )
    web.run_app(app, host=host, port=port, print=None)


def _is_exposed(host: str) -> bool:
    if host in ("", "0.0.0.0", "::"):  # noqa: S104 - detecting this, not doing it
        return True
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host not in ("localhost",)
