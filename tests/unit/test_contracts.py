"""Contract tests: schemas, action library, and robot manifests.

These guard the configuration itself. A typo in actions.yaml is not a cosmetic
problem when the file decides what a robot is allowed to attempt.
"""

from __future__ import annotations

import json

import pytest

from brash_orchestrator_core.action_library import ActionLibrary, ActionLibraryError
from brash_orchestrator_core.capability_registry import CapabilityRegistry
from brash_orchestrator_core.schema_registry import SCHEMA_FILES, SchemaRegistry
from brash_world_model.predicates import parse_predicate


def test_every_schema_declares_a_contract_id(schemas: SchemaRegistry) -> None:
    for name in SCHEMA_FILES:
        contract = schemas.contract_id(name)
        assert contract.startswith("brash."), f"{name} has no brash.<name>/vN identifier"
        assert "/v" in contract


def test_task_graph_schema_accepts_the_reference_example(
    schemas: SchemaRegistry, examples: list[dict]
) -> None:
    for example in examples:
        errors = schemas.validation_errors("task_graph", example["task_graph"])
        assert not errors, f"{example['example_id']} rejected: {[str(e) for e in errors]}"


@pytest.mark.parametrize(
    "mutation,expected_code",
    [
        ({"nodes": []}, "SCHEMA_VIOLATION"),
        ({"goal_predicate": "not a predicate"}, "SCHEMA_VIOLATION"),
        ({"task_id": ""}, "SCHEMA_VIOLATION"),
    ],
)
def test_task_graph_schema_rejects_malformed_documents(
    schemas: SchemaRegistry, examples: list[dict], mutation: dict, expected_code: str
) -> None:
    document = json.loads(json.dumps(examples[0]["task_graph"]))
    document.update(mutation)
    errors = schemas.validation_errors("task_graph", document)
    assert errors and errors[0].code == expected_code


def test_task_graph_schema_rejects_an_unknown_node_field(
    schemas: SchemaRegistry, examples: list[dict]
) -> None:
    document = json.loads(json.dumps(examples[0]["task_graph"]))
    document["nodes"][0]["robot_command"] = "move_joint(3, 1.57)"
    errors = schemas.validation_errors("task_graph", document)
    # The schema is closed so a model cannot smuggle a motor command through.
    assert errors
    assert errors[0].node_id == "n1"


def test_schema_errors_point_at_the_offending_node(
    schemas: SchemaRegistry, examples: list[dict]
) -> None:
    document = json.loads(json.dumps(examples[0]["task_graph"]))
    document["nodes"][2]["action"] = "Move-It"
    errors = schemas.validation_errors("task_graph", document)
    assert errors
    assert errors[0].node_id == "n3"
    assert errors[0].field == "action"


def test_action_preconditions_and_effects_are_parseable(library: ActionLibrary) -> None:
    for action in library.actions.values():
        for template in (*action.preconditions, *action.effects):
            parse_predicate(template)
        if action.verification:
            parse_predicate(action.verification)


def test_every_action_effect_only_uses_declared_parameters(library: ActionLibrary) -> None:
    for action in library.actions.values():
        for template in (*action.preconditions, *action.effects):
            predicate = parse_predicate(template)
            for argument in predicate.args:
                # An argument is either a parameter, the assigned robot, or a
                # literal such as the "container" in object_type(x, container).
                assert (
                    argument in action.parameters
                    or argument == "assigned_robot"
                    or argument.islower()
                ), f"{action.name} references {argument!r}"


def test_every_action_has_a_verification_predicate(library: ActionLibrary) -> None:
    """An action nothing can confirm cannot contribute to a verified goal."""
    missing = [name for name, action in library.actions.items() if not action.verification]
    assert not missing, f"actions without verification: {missing}"


def test_transport_actions_declare_pickup_and_dropoff(library: ActionLibrary) -> None:
    for action in library.actions.values():
        if action.execution.is_transport:
            assert action.execution.pickup_from
            assert action.execution.dropoff_to


def test_every_required_capability_exists_on_some_robot(
    library: ActionLibrary, registry: CapabilityRegistry
) -> None:
    available = set(registry.capability_names())
    for action in library.actions.values():
        missing = set(action.required_capabilities) - available
        assert not missing, f"{action.name} needs unprovided capabilities {sorted(missing)}"


def test_action_library_rejects_an_unknown_parameter_type() -> None:
    with pytest.raises(ActionLibraryError, match="unsupported type"):
        ActionLibrary.from_mapping(
            {
                "actions": {
                    "bad": {
                        "parameters": {"thing": "freeform"},
                        "timeout_s": 10,
                        "execution": {"kind": "manipulation"},
                    }
                }
            }
        )


def test_action_library_rejects_execution_referencing_an_unknown_parameter() -> None:
    with pytest.raises(ActionLibraryError, match="unknown parameter"):
        ActionLibrary.from_mapping(
            {
                "actions": {
                    "bad": {
                        "parameters": {"object_id": "object_id"},
                        "timeout_s": 10,
                        "execution": {"kind": "manipulation", "station_from": "nowhere"},
                    }
                }
            }
        )


def test_capabilities_are_never_robot_names(
    library: ActionLibrary, registry: CapabilityRegistry
) -> None:
    """Capabilities must stay robot-independent or the scheduler is pointless."""
    robot_ids = set(registry.robot_ids)
    for action in library.actions.values():
        assert not (set(action.required_capabilities) & robot_ids)


def test_robot_payload_limits_are_positive(registry: CapabilityRegistry) -> None:
    for robot_id in registry.robot_ids:
        for capability in registry[robot_id].capabilities.values():
            if capability.max_mass_kg is not None:
                assert capability.max_mass_kg > 0, f"{robot_id}.{capability.name}"
            assert 0.0 <= capability.success_prior <= 1.0


def test_two_robots_have_disjoint_specialities(registry: CapabilityRegistry) -> None:
    """The whole design assumes heterogeneous bodies, so assert it."""
    transporters = {profile.robot_id for profile in registry.robots_with(["transport_container"])}
    manipulators = {profile.robot_id for profile in registry.robots_with(["open_container"])}
    assert transporters and manipulators
    assert not (transporters & manipulators)
