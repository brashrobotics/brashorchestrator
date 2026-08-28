"""Execution events: the single narrative of what happened.

Every state change is recorded as one event conforming to
``brash.execution-event/v1``. The monitor, the recovery planner, the trial
recorder, and the operator feed all read the same stream, so there is exactly one
account of a task rather than four that can disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

EVENT_CONTRACT = "brash.execution-event/v1"

EventSink = Callable[["ExecutionEvent"], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    task_id: str
    node_id: str
    event: str
    error_code: str = "SUCCESS"
    robot_id: str | None = None
    action: str | None = None
    attempt: int = 1
    detail: str = ""
    external_reference: str | None = None
    observations: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, float] = field(default_factory=dict)
    occurred_at: str = field(default_factory=utc_now)

    @property
    def is_failure(self) -> bool:
        return self.error_code != "SUCCESS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "node_id": self.node_id,
            "robot_id": self.robot_id,
            "action": self.action,
            "event": self.event,
            "error_code": self.error_code,
            "occurred_at": self.occurred_at,
            "attempt": self.attempt,
            "detail": self.detail,
            "external_reference": self.external_reference,
            "observations": dict(self.observations),
            "metrics": dict(self.metrics),
        }

    def describe(self) -> str:
        parts = [f"{self.event}"]
        if self.node_id and self.node_id != "-":
            parts.append(self.node_id)
        if self.action:
            parts.append(self.action)
        if self.robot_id:
            parts.append(f"on {self.robot_id}")
        if self.is_failure:
            parts.append(f"[{self.error_code}]")
        if self.detail:
            parts.append(f"- {self.detail}")
        return " ".join(parts)


@dataclass
class EventLog:
    """Ordered event history with fan-out to live subscribers."""

    task_id: str = ""
    events: list[ExecutionEvent] = field(default_factory=list)
    sinks: list[EventSink] = field(default_factory=list)

    def subscribe(self, sink: EventSink) -> None:
        self.sinks.append(sink)

    def emit(self, event: ExecutionEvent) -> ExecutionEvent:
        self.events.append(event)
        for sink in self.sinks:
            # A broken subscriber must not derail a physical task.
            try:
                sink(event)
            except Exception:  # noqa: BLE001
                pass
        return event

    def record(
        self,
        event: str,
        *,
        node_id: str = "-",
        error_code: str = "SUCCESS",
        **kwargs: Any,
    ) -> ExecutionEvent:
        return self.emit(
            ExecutionEvent(
                task_id=self.task_id,
                node_id=node_id,
                event=event,
                error_code=error_code,
                **kwargs,
            )
        )

    def failures(self) -> list[ExecutionEvent]:
        return [event for event in self.events if event.is_failure]

    def for_node(self, node_id: str) -> list[ExecutionEvent]:
        return [event for event in self.events if event.node_id == node_id]

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.events]

    def extend(self, events: Iterable[ExecutionEvent]) -> None:
        for event in events:
            self.emit(event)
