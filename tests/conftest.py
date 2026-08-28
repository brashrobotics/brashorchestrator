"""Shared fixtures.

Every fixture here is hermetic: no LLM endpoint, no Fleet Control, no network
beyond loopback. That is deliberate, because a test suite that needs a model
served is a test suite that stops being run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Trial records are a side effect nothing in the suite should produce on disk.
os.environ.setdefault("ORCH_RESOURCE_ROOT", str(REPO_ROOT))
os.environ.setdefault("ORCH_TRIAL_RECORDING", "0")

from brash_orchestrator_core.action_library import ActionLibrary  # noqa: E402
from brash_orchestrator_core.capability_registry import CapabilityRegistry  # noqa: E402
from brash_orchestrator_core.llm.provider import MockProvider  # noqa: E402
from brash_orchestrator_core.llm.settings import LlmSettings  # noqa: E402
from brash_orchestrator_core.recovery_planner import (  # noqa: E402
    RecoveryPlanner,
    RecoveryPolicies,
)
from brash_orchestrator_core.scheduler import Scheduler, SchedulerConfig  # noqa: E402
from brash_orchestrator_core.schema_registry import SchemaRegistry  # noqa: E402
from brash_orchestrator_core.task_graph import TaskGraph  # noqa: E402
from brash_orchestrator_core.task_validator import TaskValidator  # noqa: E402
from brash_safety_manager.policy import SafetyManager, SafetyPolicy  # noqa: E402
from brash_world_model.model import WorldModel  # noqa: E402

#: The three goals the mock provider knows, kept here so a change to
#: ``prompts/task_graph_examples.jsonl`` breaks in one place rather than five.
DRILL_GOAL = (
    "Take the drill inside the closed box in the maintenance area and place it "
    "on shelf B12 on the second floor."
)
MOVE_GOAL = "Move the tool box from the maintenance area to the handover station."
INSPECT_GOAL = "Check whether shelf B12 has space available."


@pytest.fixture(scope="session")
def drill_goal() -> str:
    return DRILL_GOAL


@pytest.fixture(scope="session")
def move_goal() -> str:
    return MOVE_GOAL


@pytest.fixture(scope="session")
def inspect_goal() -> str:
    return INSPECT_GOAL


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def library() -> ActionLibrary:
    return ActionLibrary.load()


@pytest.fixture(scope="session")
def registry() -> CapabilityRegistry:
    return CapabilityRegistry.load()


@pytest.fixture(scope="session")
def policies() -> RecoveryPolicies:
    return RecoveryPolicies.load()


@pytest.fixture(scope="session")
def schemas() -> SchemaRegistry:
    return SchemaRegistry.load()


@pytest.fixture
def world() -> WorldModel:
    return WorldModel.load(REPO_ROOT / "config" / "world_demo.yaml")


@pytest.fixture
def validator(library: ActionLibrary, registry: CapabilityRegistry, policies: RecoveryPolicies):
    return TaskValidator(library, registry, recovery_policy_codes=policies.error_codes)


@pytest.fixture
def scheduler(library: ActionLibrary, registry: CapabilityRegistry) -> Scheduler:
    return Scheduler(library, registry, SchedulerConfig.load())


@pytest.fixture(scope="session")
def examples(repo_root: Path) -> list[dict[str, Any]]:
    path = repo_root / "prompts" / "task_graph_examples.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture
def drill_graph(examples: list[dict[str, Any]]) -> TaskGraph:
    for example in examples:
        if example["example_id"] == "store_drill":
            return TaskGraph.from_dict(example["task_graph"])
    raise AssertionError("the store_drill example is missing")


@pytest.fixture
def mock_provider(repo_root: Path) -> MockProvider:
    return MockProvider.from_examples_file(repo_root / "prompts" / "task_graph_examples.jsonl")


@pytest.fixture
def llm_settings() -> LlmSettings:
    return LlmSettings.load(profile="mock")


@pytest.fixture
def recovery(
    policies: RecoveryPolicies,
    library: ActionLibrary,
    mock_provider: MockProvider,
    llm_settings: LlmSettings,
    schemas: SchemaRegistry,
) -> RecoveryPlanner:
    return RecoveryPlanner(
        policies=policies,
        library=library,
        provider=mock_provider,
        settings=llm_settings,
        schemas=schemas,
    )


@pytest.fixture
def safety() -> SafetyManager:
    return SafetyManager(SafetyPolicy.load(REPO_ROOT / "config" / "safety.yaml"))


@pytest.fixture
def fleet_stub():
    """A live Fleet Control stand-in on an ephemeral port.

    Integration tests talk real HTTP to this rather than mocking the client, so
    header handling, idempotency, polling, and error mapping are all exercised on
    the same code path production uses.
    """
    from tools.fleet_stub.server import FleetStub, FleetStubServer

    stub = FleetStub()
    server = FleetStubServer(stub, port=0).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fleet_backend(fleet_stub):
    from brash_orchestrator_core.backends.fleet_control import (
        FleetControlBackend,
        FleetControlSettings,
    )

    return FleetControlBackend(
        FleetControlSettings(
            base_url=fleet_stub.base_url,
            token="brash-dev-operator",
            site_id="plant-a",
            poll_interval_s=0.01,
        )
    )


@pytest.fixture
def orchestrator(fleet_backend):
    """A fully wired orchestrator on the mock LLM and the stub fleet."""
    from brash_orchestrator_core.orchestrator import Orchestrator

    return Orchestrator.build(
        llm_profile="mock",
        backend=fleet_backend,
        record_trials=False,
    )
