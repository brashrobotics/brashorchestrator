"""LLM Task Interpreter.

This is the only place natural language enters the system. It produces a typed
task graph and nothing else: no robot assignment, no motor command, no safety
decision.

Interpretation is a loop, not a single shot. The model proposes, the schema and
the validator reject with machine-readable reasons, and those reasons go back as
the next prompt. Attempts are bounded, because a model that cannot satisfy the
validator in a couple of tries is not going to on the tenth, and an operator
waiting on a physical task deserves a clear failure instead of a stall.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from brash_orchestrator_core.action_library import ActionLibrary
from brash_orchestrator_core.capability_registry import CapabilityRegistry
from brash_orchestrator_core.llm.provider import LlmError, LlmProvider, LlmRequest
from brash_orchestrator_core.llm.settings import LlmSettings
from brash_orchestrator_core.schema_registry import SchemaRegistry
from brash_orchestrator_core.task_graph import TaskGraph, TaskGraphError
from brash_orchestrator_core.task_validator import TaskValidator, ValidationError, ValidationReport
from brash_world_model.model import WorldModel


@dataclass
class InterpretationAttempt:
    index: int
    payload: dict[str, Any]
    errors: list[dict[str, Any]]
    model: str = ""
    decoding: str = ""
    latency_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.index,
            "model": self.model,
            "decoding": self.decoding,
            "latency_s": round(self.latency_s, 3),
            "errors": self.errors,
            "task_graph": self.payload,
        }


@dataclass
class InterpretationResult:
    graph: TaskGraph | None
    report: ValidationReport
    attempts: list[InterpretationAttempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.graph is not None and self.report.ok

    @property
    def valid_first_try(self) -> bool:
        """Feeds the task-graph-validity KPI."""
        return self.ok and len(self.attempts) == 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "valid_first_try": self.valid_first_try,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "validation": self.report.to_dict(),
            "task_graph": self.graph.to_dict() if self.graph else None,
        }


class TaskInterpreter:
    def __init__(
        self,
        *,
        provider: LlmProvider,
        settings: LlmSettings,
        library: ActionLibrary,
        registry: CapabilityRegistry,
        validator: TaskValidator,
        schemas: SchemaRegistry | None = None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.library = library
        self.registry = registry
        self.validator = validator
        self.schemas = schemas or SchemaRegistry.load()
        self._system_prompt = settings.prompt_text("task_interpreter_system")

    async def interpret(
        self,
        *,
        task_id: str,
        goal: str,
        world: WorldModel,
        constraints: Mapping[str, Any] | None = None,
        approvals: Sequence[str] = (),
    ) -> InterpretationResult:
        constraints = dict(constraints or {})
        schema = self.schemas.get("task_graph")
        attempts: list[InterpretationAttempt] = []
        report = ValidationReport()
        feedback: list[dict[str, Any]] = []

        for index in range(1, self.settings.max_repair_attempts + 2):
            request = LlmRequest(
                system=self._system_prompt,
                user=self._build_user_prompt(
                    task_id=task_id,
                    goal=goal,
                    world=world,
                    constraints=constraints,
                    feedback=feedback,
                ),
                schema=schema,
                schema_name="task_graph",
                temperature=self.settings.temperature,
                top_p=self.settings.top_p,
                max_tokens=self.settings.max_tokens,
                context={"task_id": task_id, "goal": goal},
            )
            response = await self.provider.complete_json(request)
            payload = dict(response.payload)
            # The task identifier is the operator's, not the model's.
            payload["task_id"] = task_id

            attempt = InterpretationAttempt(
                index=index,
                payload=payload,
                errors=[],
                model=response.model,
                decoding=response.decoding,
                latency_s=response.latency_s,
            )
            attempts.append(attempt)

            report = ValidationReport()
            schema_errors = self.schemas.validation_errors("task_graph", payload)
            for error in schema_errors:
                report.add(error)

            graph: TaskGraph | None = None
            if report.ok:
                try:
                    graph = TaskGraph.from_dict(payload)
                except TaskGraphError as error:
                    report.add(
                        ValidationError("schema", "MALFORMED_GRAPH", str(error))
                    )

            if graph is not None:
                report = self.validator.validate(
                    graph, world, constraints=constraints, approvals=approvals
                )

            attempt.errors = [error.to_dict() for error in report.errors]
            if report.ok and graph is not None:
                return InterpretationResult(graph=graph, report=report, attempts=attempts)

            feedback = attempt.errors

        return InterpretationResult(graph=None, report=report, attempts=attempts)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------
    def _build_user_prompt(
        self,
        *,
        task_id: str,
        goal: str,
        world: WorldModel,
        constraints: Mapping[str, Any],
        feedback: Sequence[Mapping[str, Any]],
    ) -> str:
        """Assemble the seven-part interpreter context.

        The world summary is a short typed digest, never raw robot telemetry. A
        planner needs to know that a box is closed and where it sits, not the
        current joint velocities of the arm that will open it.
        """
        sections: list[str] = [
            f"## 1. TASK\ntask_id: {task_id}\ngoal: {goal}",
            "## 2. WORLD STATE\n" + _as_json(world.summary()),
            "## 3. AVAILABLE ACTIONS\n" + _as_json(self.library.prompt_view()),
            "## 4. CAPABILITY VOCABULARY\n"
            + _as_json(sorted(set(self.library.capability_vocabulary()) & set(self.registry.capability_names()))),
            "## 5. CONSTRAINTS\n" + _as_json(constraints or {}),
            "## 6. ALLOWED GOAL PREDICATES\n"
            + _as_json(list(self.library.goal_predicate_vocabulary())),
            "## 7. OUTPUT SCHEMA\n" + _as_json(self.schemas.get("task_graph")),
        ]

        examples = self._example_section()
        if examples:
            sections.append(examples)

        if feedback:
            sections.append(
                "## VALIDATION ERRORS\n"
                "Your previous graph was rejected. Fix exactly these problems and "
                "re-emit the complete graph.\n" + _as_json(list(feedback))
            )

        sections.append(
            "Emit only the JSON task graph object. No prose, no markdown, no code fences."
        )
        return "\n\n".join(sections)

    def _example_section(self) -> str:
        """Few-shot examples, skipped for the mock provider which replays them."""
        if self.settings.is_mock:
            return ""
        path = self.settings.prompts.get("examples")
        if not path or self.settings.max_examples <= 0:
            return ""
        try:
            lines = [
                line
                for line in open(path, encoding="utf-8").read().splitlines()
                if line.strip()
            ]
        except OSError:
            return ""

        rendered: list[str] = []
        for line in lines[: self.settings.max_examples]:
            try:
                example = json.loads(line)
            except json.JSONDecodeError:
                continue
            rendered.append(
                f"GOAL: {example.get('goal', '')}\nGRAPH: {json.dumps(example.get('task_graph', {}))}"
            )
        if not rendered:
            return ""
        return "## EXAMPLES\n" + "\n\n".join(rendered)


def _as_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=False, default=str)


async def interpret_goal(
    *,
    task_id: str,
    goal: str,
    world: WorldModel,
    library: ActionLibrary,
    registry: CapabilityRegistry,
    validator: TaskValidator,
    settings: LlmSettings,
    provider: LlmProvider | None = None,
    constraints: Mapping[str, Any] | None = None,
    approvals: Sequence[str] = (),
) -> InterpretationResult:
    """Convenience wrapper that owns the provider lifetime."""
    owned = provider is None
    active = provider or settings.build_provider()
    interpreter = TaskInterpreter(
        provider=active,
        settings=settings,
        library=library,
        registry=registry,
        validator=validator,
    )
    try:
        return await interpreter.interpret(
            task_id=task_id,
            goal=goal,
            world=world,
            constraints=constraints,
            approvals=approvals,
        )
    finally:
        if owned:
            await active.close()


__all__ = [
    "InterpretationAttempt",
    "InterpretationResult",
    "LlmError",
    "TaskInterpreter",
    "interpret_goal",
]
