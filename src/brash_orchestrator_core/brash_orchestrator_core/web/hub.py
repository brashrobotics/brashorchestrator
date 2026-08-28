"""Server-side session state for the operator console.

The console is a view onto exactly one orchestrator, and that orchestrator owns
one mutable world model and talks to one fleet. That makes it a single-writer
system, and this module says so out loud: one task runs at a time and a second
submission is refused with a clear conflict rather than quietly interleaved into
the same world state. Two tasks asserting facts about the same drill would
produce a world model that describes neither of them.

Everything an observer sees arrives through the execution event stream, which is
the same stream the monitor, the recovery planner, and the trial recorder read.
The browser is therefore not a privileged client with its own view of the truth;
it is one more subscriber to the single narrative of what happened.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import aiohttp

from brash_orchestrator_core.backends.base import ERROR_CODES
from brash_orchestrator_core.events import EventLog, ExecutionEvent, utc_now
from brash_orchestrator_core.orchestrator import Orchestrator, TaskResult, derive_task_id
from brash_orchestrator_core.paths import config_path
from brash_world_model.model import WorldModel
from brash_world_model.twin_bundle_loader import ground_world_model, load_twin_bundle

#: Modes the console can submit a goal in.
TASK_MODES = ("plan", "dry_run", "execute")

#: Events buffered per task. Long enough for any realistic task, bounded so a
#: runaway recovery loop cannot exhaust memory in a long-lived server.
MAX_TASK_EVENTS = 4000

#: Messages buffered per connected browser.
SUBSCRIBER_BACKLOG = 512


class HubError(RuntimeError):
    """A request the hub refuses, carrying how it should surface over HTTP."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(eq=False)
class Subscriber:
    """One connected browser.

    Compared by identity, because two browsers with identical empty queues are
    still two browsers.
    """

    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=SUBSCRIBER_BACKLOG)
    )
    dropped: int = 0

    def offer(self, message: Mapping[str, Any]) -> None:
        """Enqueue a message, discarding the oldest if the client fell behind.

        A browser that stops reading must never slow down or block a physical
        task, so back pressure is resolved by dropping history and telling the
        client it happened rather than by waiting for it.
        """
        try:
            self.queue.put_nowait(dict(message))
            return
        except asyncio.QueueFull:
            self.dropped += 1
        try:
            self.queue.get_nowait()
            self.queue.put_nowait(dict(message))
        except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover - racy client
            pass


@dataclass
class TaskRecord:
    """One submitted goal and everything it produced."""

    task_id: str
    goal: str
    mode: str
    constraints: dict[str, Any] = field(default_factory=dict)
    approvals: list[str] = field(default_factory=list)
    state: str = "RUNNING"
    failure_code: str = ""
    summary: str = ""
    submitted_at: str = field(default_factory=utc_now)
    finished_at: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        return self.state == "RUNNING"

    def brief(self) -> dict[str, Any]:
        """The compact form used by the task list and by live notifications."""
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "mode": self.mode,
            "state": self.state,
            "failure_code": self.failure_code,
            "summary": self.summary,
            "submitted_at": self.submitted_at,
            "finished_at": self.finished_at,
            "event_count": len(self.events),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.brief(),
            "constraints": dict(self.constraints),
            "approvals": list(self.approvals),
            "events": list(self.events),
            "result": self.result,
        }


class TaskHub:
    """Owns the console's view of one orchestrator."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        *,
        scenario_path: str | Path | None = None,
        twin_bundle_path: str | Path | None = None,
        history_limit: int = 25,
    ) -> None:
        self.orchestrator = orchestrator
        self.scenario_path = Path(scenario_path) if scenario_path else config_path("world_demo.yaml")
        self.twin_bundle_path = Path(twin_bundle_path) if twin_bundle_path else None
        self.history_limit = history_limit
        self.subscribers: set[Subscriber] = set()
        self._tasks: dict[str, TaskRecord] = {}
        self._order: list[str] = []
        self._running: TaskRecord | None = None
        self._runner: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._control_surface: bool | None = None

    # ------------------------------------------------------------------
    # Live stream
    # ------------------------------------------------------------------
    def subscribe(self) -> Subscriber:
        subscriber = Subscriber()
        self.subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self.subscribers.discard(subscriber)

    def broadcast(self, kind: str, **payload: Any) -> None:
        message = {"type": kind, "sent_at": utc_now(), **payload}
        for subscriber in tuple(self.subscribers):
            subscriber.offer(message)

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    @property
    def running_task_id(self) -> str | None:
        return self._running.task_id if self._running else None

    def task(self, task_id: str) -> TaskRecord:
        record = self._tasks.get(task_id)
        if record is None:
            raise HubError("TASK_NOT_FOUND", f"no task {task_id!r}", status=404)
        return record

    def history(self) -> list[dict[str, Any]]:
        return [self._tasks[task_id].brief() for task_id in reversed(self._order)]

    def submit(
        self,
        *,
        goal: str,
        mode: str = "execute",
        constraints: Mapping[str, Any] | None = None,
        approvals: Sequence[str] = (),
        task_id: str | None = None,
    ) -> TaskRecord:
        """Accept a goal and start it. Returns as soon as the task is running."""
        goal = str(goal or "").strip()
        if not goal:
            raise HubError("GOAL_REQUIRED", "a goal is required")
        if mode not in TASK_MODES:
            raise HubError(
                "UNKNOWN_MODE", f"mode must be one of {', '.join(TASK_MODES)}, not {mode!r}"
            )
        if self._running is not None:
            raise HubError(
                "TASK_ALREADY_RUNNING",
                f"task {self._running.task_id} is still running. The orchestrator "
                "owns one world model and one fleet, so tasks execute one at a time.",
                status=409,
            )

        resolved = dict(constraints or {})
        if mode == "dry_run":
            resolved["dry_run"] = True
        record = TaskRecord(
            task_id=derive_task_id(goal, task_id),
            goal=goal,
            mode=mode,
            constraints=resolved,
            approvals=list(approvals),
        )
        self._tasks[record.task_id] = record
        self._order.append(record.task_id)
        self._trim_history()
        self._running = record
        self._runner = asyncio.create_task(self._run(record), name=f"brash-task-{record.task_id}")
        self.broadcast("task_started", task=record.brief())
        return record

    async def cancel(self, task_id: str) -> TaskRecord:
        record = self.task(task_id)
        if not record.running or self._runner is None:
            raise HubError(
                "TASK_NOT_RUNNING", f"task {task_id} is already {record.state}", status=409
            )
        runner = self._runner
        runner.cancel()
        # Let the runner's own bookkeeping settle before answering, so the
        # response already reflects the cancelled state. asyncio.wait is used
        # rather than awaiting the task, because awaiting a cancelled task would
        # raise CancelledError here and look like the HTTP request was aborted.
        await asyncio.wait({runner}, timeout=10.0)
        return record

    async def _run(self, record: TaskRecord) -> None:
        log = EventLog(task_id=record.task_id)
        log.subscribe(lambda event: self._on_event(record, event))
        try:
            if record.mode == "plan":
                result = await self.orchestrator.plan_task(
                    task_id=record.task_id,
                    goal=record.goal,
                    constraints=record.constraints,
                    approvals=record.approvals,
                    events=log,
                )
                feasible = bool(result.plan and result.plan.feasible)
                record.state = "SUCCEEDED" if feasible else "FAILED"
                record.summary = _plan_summary(result, feasible)
            else:
                result = await self.orchestrator.run_task(
                    task_id=record.task_id,
                    goal=record.goal,
                    constraints=record.constraints,
                    approvals=record.approvals,
                    events=log,
                )
                record.state = "SUCCEEDED" if result.success else "FAILED"
                record.summary = result.summary
            record.failure_code = "" if record.state == "SUCCEEDED" else result.failure_code
            record.result = result.to_dict()
        except asyncio.CancelledError:
            record.state = "CANCELLED"
            record.failure_code = "CANCELLED"
            record.summary = "the operator cancelled the task"
            raise
        except Exception as error:  # noqa: BLE001 - a console reports, it does not crash
            record.state = "FAILED"
            record.failure_code = "UNKNOWN_FAILURE"
            record.summary = f"the orchestrator raised {type(error).__name__}: {error}"
        finally:
            record.finished_at = utc_now()
            self._running = None
            self._runner = None
            self.broadcast("task_finished", task=record.brief())
            self.broadcast("world", world=self.orchestrator.world.snapshot())

    def _on_event(self, record: TaskRecord, event: ExecutionEvent) -> None:
        payload = event.to_dict()
        if len(record.events) < MAX_TASK_EVENTS:
            record.events.append(payload)
        self.broadcast("event", task_id=record.task_id, event=payload)

    def _trim_history(self) -> None:
        while len(self._order) > self.history_limit:
            evicted = self._order.pop(0)
            if evicted != self.running_task_id:
                self._tasks.pop(evicted, None)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        orchestrator = self.orchestrator
        backend_health = await orchestrator.backend.health()
        profile = orchestrator.llm_settings.profile
        return {
            "llm": {
                "profile": orchestrator.llm_settings.profile_name,
                "provider": orchestrator.provider.name,
                "model": profile.get("model", ""),
                "base_url": profile.get("base_url", ""),
                "decoding": profile.get("decoding", ""),
                "mock": orchestrator.llm_settings.is_mock,
                "max_repair_attempts": orchestrator.llm_settings.max_repair_attempts,
            },
            "backend": {"name": orchestrator.backend.name, **backend_health.to_dict()},
            "scenario": str(self.scenario_path),
            "twin_bundle": str(self.twin_bundle_path) if self.twin_bundle_path else "",
            "dry_run_default": orchestrator.dry_run,
            "trials": str(orchestrator.recorder.path) if orchestrator.recorder else "",
            "control_surface": await self.control_surface_available(),
            "running_task_id": self.running_task_id,
            "approvals": sorted(orchestrator.safety.approvals),
            "safety_enabled": orchestrator.safety.policy.enabled,
        }

    def library(self) -> dict[str, Any]:
        """Everything the front end needs to build its forms and legends."""
        orchestrator = self.orchestrator
        library = orchestrator.library
        registry = orchestrator.registry
        actions = [
            {
                "name": spec.name,
                "signature": spec.signature(),
                "parameters": dict(spec.parameters),
                "required_capabilities": list(spec.required_capabilities),
                "preconditions": list(spec.preconditions),
                "effects": list(spec.effects),
                "verification": spec.verification or "",
                "timeout_s": spec.timeout_s,
                "hazardous": spec.hazardous,
                "kind": spec.execution.kind,
                "primitive": spec.execution.primitive,
            }
            for spec in (library[name] for name in library.names)
        ]
        robots = [
            {
                "robot_id": profile.robot_id,
                "type": profile.type,
                "vendor": profile.vendor,
                "fleet_robot_id": profile.fleet_robot_id,
                "capabilities": sorted(
                    name for name, capability in profile.capabilities.items() if capability.enabled
                ),
            }
            for profile in (registry[robot_id] for robot_id in registry.robot_ids)
        ]
        primitives = sorted({action["primitive"] for action in actions} | {"transport"})
        return {
            "actions": actions,
            "robots": robots,
            "primitives": primitives,
            "error_codes": [code for code in ERROR_CODES if code != "SUCCESS"],
            "recovery_codes": list(orchestrator.recovery.policies.error_codes),
            "goal_predicates": list(library.goal_predicate_vocabulary()),
            "example_goals": self._example_goals(),
            "modes": list(TASK_MODES),
        }

    def world(self) -> dict[str, Any]:
        return self.orchestrator.world.snapshot()

    async def snapshot(self) -> dict[str, Any]:
        """The full picture a browser needs the moment it connects."""
        current = self._running.to_dict() if self._running else None
        return {
            "health": await self.health(),
            "library": self.library(),
            "world": self.world(),
            "tasks": self.history(),
            "current_task": current,
        }

    def _example_goals(self) -> list[str]:
        """Goals from the few-shot example file.

        These matter more than they look: the mock provider replays exactly these
        examples, so under the mock profile they are the goals that can succeed.
        Offering them in the UI is the difference between a console that
        demonstrates the pipeline and one that only demonstrates interpretation
        failures.
        """
        path = self.orchestrator.llm_settings.prompts.get("examples")
        if not path:
            return []
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        goals: list[str] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                goal = json.loads(line).get("goal")
            except json.JSONDecodeError:
                continue
            if isinstance(goal, str) and goal not in goals:
                goals.append(goal)
        return goals

    # ------------------------------------------------------------------
    # Operator controls
    # ------------------------------------------------------------------
    def set_approval(self, token: str, granted: bool) -> list[str]:
        token = str(token or "").strip()
        if not token:
            raise HubError("APPROVAL_REQUIRED", "an approval token is required")
        if granted:
            self.orchestrator.safety.grant(token)
        else:
            self.orchestrator.safety.revoke(token)
        approvals = sorted(self.orchestrator.safety.approvals)
        self.broadcast("approvals", approvals=approvals)
        return approvals

    async def reset(self) -> dict[str, Any]:
        """Reload the scenario and clear injected faults.

        Reloading is the only honest way to rewind: the world model records
        observed facts, and an operator who wants a clean trial wants the
        scenario's declared ground truth back, not a partial undo.
        """
        if self._running is not None:
            raise HubError(
                "TASK_ALREADY_RUNNING",
                f"task {self._running.task_id} is still running",
                status=409,
            )
        world = WorldModel.load(self.scenario_path)
        ungrounded: list[str] = []
        if self.twin_bundle_path:
            ungrounded = list(ground_world_model(world, load_twin_bundle(self.twin_bundle_path)))
        self.orchestrator.world = world
        self.orchestrator.safety.blocked.clear()
        self._tasks.clear()
        self._order.clear()

        stub_reset = None
        if await self.control_surface_available():
            stub_reset = await self._post_control("/control/v1/reset", {})
        self.broadcast("world", world=self.world())
        self.broadcast("tasks", tasks=self.history())
        return {
            "scenario": str(self.scenario_path),
            "ungrounded_locations": ungrounded,
            "fleet_reset": stub_reset is not None,
        }

    async def inject_fault(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Forward a fault declaration to the execution backend's control surface."""
        if not await self.control_surface_available():
            raise HubError(
                "CONTROL_SURFACE_UNAVAILABLE",
                "the execution backend exposes no fault-injection control surface. "
                "Run tools.fleet_stub to get one.",
                status=409,
            )
        response = await self._post_control("/control/v1/faults", body)
        if response is None:
            raise HubError(
                "CONTROL_SURFACE_UNAVAILABLE", "the control surface stopped responding", status=502
            )
        return response

    async def control_surface_available(self) -> bool:
        """Probe whether the backend accepts fault declarations.

        The probe declares nothing and asks the control surface to reject it. The
        stub answers 400 because it refuses empty declarations by design; a real
        Fleet Control has no such route and answers 404. Testing the capability
        we actually need beats sniffing a server banner.
        """
        if self._control_surface is not None:
            return self._control_surface
        base = self._fleet_base_url()
        if not base:
            self._control_surface = False
            return False
        try:
            session = await self._client()
            async with session.post(
                f"{base}/control/v1/faults",
                json={},
                timeout=aiohttp.ClientTimeout(total=2.0),
            ) as response:
                self._control_surface = response.status == 400
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self._control_surface = False
        return bool(self._control_surface)

    async def _post_control(self, path: str, body: Mapping[str, Any]) -> dict[str, Any] | None:
        base = self._fleet_base_url()
        if not base:
            return None
        try:
            session = await self._client()
            async with session.post(
                f"{base}{path}", json=dict(body), timeout=aiohttp.ClientTimeout(total=5.0)
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    error = (payload or {}).get("error") or {}
                    raise HubError(
                        str(error.get("code") or "CONTROL_SURFACE_REJECTED"),
                        str(error.get("message") or f"control surface returned {response.status}"),
                    )
                return dict(payload or {})
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise HubError(
                "CONTROL_SURFACE_UNREACHABLE", f"could not reach {base}{path}: {error}", status=502
            ) from error

    def _fleet_base_url(self) -> str:
        settings = getattr(self.orchestrator.backend, "settings", None)
        return str(getattr(settings, "base_url", "") or "")

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ------------------------------------------------------------------
    async def close(self) -> None:
        runner = self._runner
        if runner is not None:
            runner.cancel()
            await asyncio.wait({runner}, timeout=5.0)
        if self._session is not None and not self._session.closed:
            await self._session.close()


def _plan_summary(result: TaskResult, feasible: bool) -> str:
    """Describe a planning-only result, which has no execution outcome."""
    if not feasible:
        return result.rejection_detail or "the goal could not be planned"
    graph = result.interpretation.graph if result.interpretation else None
    plan = result.plan
    nodes = len(graph) if graph else 0
    waves = len(plan.layers) if plan else 0
    robots = sorted(
        {
            assignment.assigned_robot_id
            for assignment in (plan.assignments.values() if plan else ())
            if assignment.assigned_robot_id
        }
    )
    return (
        f"{nodes} nodes in {waves} wave(s) across {len(robots)} robot(s): "
        f"{', '.join(robots)}. Nothing was executed."
    )


def event_kinds() -> Iterable[str]:
    """Event vocabulary, for the front end's filter legend."""
    return (
        "TASK_ACCEPTED",
        "TASK_REJECTED",
        "GRAPH_GENERATED",
        "GRAPH_VALIDATED",
        "GRAPH_REJECTED",
        "PLAN_ASSIGNED",
        "NODE_STARTED",
        "NODE_SUCCEEDED",
        "NODE_FAILED",
        "NODE_SKIPPED",
        "NODE_CANCELLED",
        "EFFECT_VERIFIED",
        "EFFECT_MISMATCH",
        "RECOVERY_PROPOSED",
        "RECOVERY_APPLIED",
        "RECOVERY_EXHAUSTED",
        "SAFETY_BLOCKED",
        "APPROVAL_REQUIRED",
        "TASK_SUCCEEDED",
        "TASK_FAILED",
        "TASK_CANCELLED",
    )
