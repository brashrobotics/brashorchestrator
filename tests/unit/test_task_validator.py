"""The ten validator checks.

Each test corresponds to a way a generated plan can be wrong, and asserts both
that it is caught and that the resulting error is specific enough for the
interpreter to repair rather than merely retry.
"""

from __future__ import annotations

import copy

import pytest

from brash_orchestrator_core.task_graph import TaskGraph, TaskNode
from brash_orchestrator_core.task_validator import TaskValidator
from brash_world_model.model import WorldModel


def _codes(report) -> set[str]:
    return {error.code for error in report.errors}


@pytest.fixture
def graph(drill_graph: TaskGraph) -> TaskGraph:
    return copy.deepcopy(drill_graph)


def test_the_reference_graph_validates_against_the_demo_world(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    report = validator.validate(graph, world)
    assert report.ok, report.summary()


# -- check 1: the action exists -----------------------------------------
def test_an_invented_action_is_rejected_and_the_alternatives_are_listed(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.nodes[0].action = "teleport_object"
    report = validator.validate(graph, world)

    assert "UNKNOWN_ACTION" in _codes(report)
    error = next(e for e in report.errors if e.code == "UNKNOWN_ACTION")
    assert error.node_id == graph.nodes[0].id
    # The repair prompt needs to know what it may choose instead.
    assert "open_container" in error.message


# -- check 2: parameters match the signature -----------------------------
def test_a_missing_parameter_names_the_parameter(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    node = next(n for n in graph.nodes if n.action == "place_object_on_shelf")
    node.parameters.pop("shelf_id")
    report = validator.validate(graph, world)

    error = next(e for e in report.errors if e.code == "MISSING_PARAMETER")
    assert error.field == "shelf_id"
    assert error.node_id == node.id


def test_an_extra_parameter_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.nodes[0].parameters["gripper_force_n"] = 40
    report = validator.validate(graph, world)
    assert "UNEXPECTED_PARAMETER" in _codes(report)


def test_a_wrong_parameter_type_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.nodes.append(
        TaskNode(
            id="nx",
            action="reposition_container",
            parameters={"container_id": "box_07", "yaw_change_deg": "ninety"},
            required_capabilities=("navigate", "grasp_container", "reposition_container"),
        )
    )
    report = validator.validate(graph, world)
    assert "PARAMETER_TYPE_MISMATCH" in _codes(report)


# -- check 3: some robot can do it ---------------------------------------
def test_a_capability_no_robot_provides_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.nodes[0].required_capabilities = (*graph.nodes[0].required_capabilities, "fly")
    report = validator.validate(graph, world)
    assert "UNKNOWN_CAPABILITY" in _codes(report)


def test_omitting_a_required_capability_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    node = next(n for n in graph.nodes if n.action == "open_container")
    node.required_capabilities = ()
    report = validator.validate(graph, world)
    assert "MISSING_REQUIRED_CAPABILITY" in _codes(report)


# -- check 4: structure ---------------------------------------------------
def test_a_dependency_cycle_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.nodes[0].depends_on = (graph.nodes[-1].id,)
    report = validator.validate(graph, world)
    assert "CYCLIC_GRAPH" in _codes(report)


def test_a_self_dependency_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.nodes[0].depends_on = (graph.nodes[0].id,)
    report = validator.validate(graph, world)
    assert "SELF_DEPENDENCY" in _codes(report)


def test_a_dependency_on_a_nonexistent_node_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.nodes[-1].depends_on = ("n99",)
    report = validator.validate(graph, world)
    assert "UNKNOWN_DEPENDENCY" in _codes(report)


def test_an_empty_graph_is_rejected(
    validator: TaskValidator, world: WorldModel
) -> None:
    empty = TaskGraph(task_id="t", goal_predicate="on(drill_01, shelf_B12)", nodes=[])
    report = validator.validate(empty, world)
    assert "EMPTY_GRAPH" in _codes(report)


def test_structural_errors_short_circuit_semantic_checks(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    """A graph that cannot be ordered cannot be reasoned about node by node."""
    graph.nodes[0].depends_on = (graph.nodes[-1].id,)
    report = validator.validate(graph, world)
    assert _codes(report) == {"CYCLIC_GRAPH"}


# -- check 5: preconditions ----------------------------------------------
def test_opening_the_box_before_locating_it_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    """The reference failure: the plan skips the step that makes the lid reachable."""
    transport = next(n for n in graph.nodes if n.action == "transport_container")
    opener = next(n for n in graph.nodes if n.action == "open_container")
    opener.depends_on = tuple(d for d in opener.depends_on if d != transport.id)
    graph.nodes.remove(transport)
    for node in graph.nodes:
        node.depends_on = tuple(d for d in node.depends_on if d != transport.id)

    report = validator.validate(graph, world)
    assert "PRECONDITION_UNSATISFIABLE" in _codes(report)
    error = next(e for e in report.errors if e.code == "PRECONDITION_UNSATISFIABLE")
    assert "lid_accessible(box_07)" in error.message


def test_preconditions_are_evaluated_against_the_projected_state(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    """Later nodes may rely on facts earlier nodes establish, not on the start state."""
    assert not world.holds("state(box_07, open)")
    report = validator.validate(graph, world)
    assert report.ok, report.summary()


def test_reordering_a_dependency_breaks_the_projection(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    pick = next(n for n in graph.nodes if n.action == "pick_tool_from_container")
    pick.depends_on = ()
    report = validator.validate(graph, world)
    assert "PRECONDITION_UNSATISFIABLE" in _codes(report)


# -- check 6: the goal is measurable and established ----------------------
def test_a_goal_no_action_establishes_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.goal_predicate = "customer_satisfied(drill_01)"
    report = validator.validate(graph, world)
    error = next(e for e in report.errors if e.code == "UNVERIFIABLE_GOAL")
    assert "Measurable goal predicates" in error.message


def test_a_negative_goal_is_rejected_as_unverifiable(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    """No sensor positively confirms an absence."""
    graph.goal_predicate = "not_held(drill_01)"
    report = validator.validate(graph, world)
    assert "UNVERIFIABLE_GOAL" in _codes(report)


def test_a_goal_the_graph_never_reaches_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.goal_predicate = "verified_at(box_07, shelf_B12)"
    report = validator.validate(graph, world)
    assert "GOAL_NOT_ESTABLISHED" in _codes(report)


def test_a_goal_naming_an_unknown_entity_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.goal_predicate = "verified_at(hammer_99, shelf_B12)"
    report = validator.validate(graph, world)
    assert "UNKNOWN_GOAL_ENTITY" in _codes(report)


def test_a_malformed_goal_predicate_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.goal_predicate = "put the drill away"
    report = validator.validate(graph, world)
    assert "MALFORMED_GOAL_PREDICATE" in _codes(report)


# -- check 7: constraints stay inside policy ------------------------------
def test_a_deadline_beyond_policy_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    report = validator.validate(graph, world, constraints={"deadline_s": 100_000})
    assert "CONSTRAINT_EXCEEDS_POLICY" in _codes(report)


def test_an_unknown_risk_level_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    report = validator.validate(graph, world, constraints={"max_risk": "reckless"})
    assert "CONSTRAINT_INVALID" in _codes(report)


def test_constraining_to_an_unregistered_robot_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    report = validator.validate(
        graph, world, constraints={"allowed_robot_ids": ["forklift_09"]}
    )
    assert "UNKNOWN_ROBOT" in _codes(report)


# -- check 8: no invented identifiers -------------------------------------
def test_a_hallucinated_object_is_rejected_and_the_real_ones_listed(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    node = next(n for n in graph.nodes if n.action == "locate_object")
    node.parameters["object_id"] = "toolbox_99"
    report = validator.validate(graph, world)

    error = next(e for e in report.errors if e.code == "UNKNOWN_OBJECT")
    assert "box_07" in error.message


def test_a_hallucinated_location_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    node = next(n for n in graph.nodes if n.action == "place_object_on_shelf")
    node.parameters["shelf_id"] = "shelf_Z99"
    report = validator.validate(graph, world)
    assert "UNKNOWN_LOCATION" in _codes(report)


def test_a_proposed_robot_that_does_not_exist_is_rejected(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.nodes[0].proposed_robot_id = "atlas_01"
    report = validator.validate(graph, world)
    assert "UNKNOWN_ROBOT" in _codes(report)


# -- check 10: hazardous actions need approval ----------------------------
def test_teleoperation_is_refused_without_the_constraint(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.nodes.append(
        TaskNode(
            id="nx",
            action="request_teleoperation",
            parameters={"object_id": "drill_01", "reason": "grasp_failed"},
            required_capabilities=("navigate",),
        )
    )
    report = validator.validate(graph, world)
    assert "TELEOPERATION_NOT_PERMITTED" in _codes(report)


def test_teleoperation_still_needs_an_operator_approval(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    """Allowing teleoperation in the constraints is not the same as approving it."""
    graph.nodes.append(
        TaskNode(
            id="nx",
            action="request_teleoperation",
            parameters={"object_id": "drill_01", "reason": "grasp_failed"},
            required_capabilities=("navigate",),
        )
    )
    report = validator.validate(
        graph, world, constraints={"allow_teleoperation": True}
    )
    assert "APPROVAL_REQUIRED" in _codes(report)

    approved = validator.validate(
        graph,
        world,
        constraints={"allow_teleoperation": True},
        approvals=["request_teleoperation"],
    )
    assert "APPROVAL_REQUIRED" not in _codes(approved)


# -- error shape ----------------------------------------------------------
def test_every_error_is_machine_readable_for_the_repair_loop(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.nodes[0].action = "teleport_object"
    report = validator.validate(graph, world)
    for error in report.errors:
        payload = error.to_dict()
        assert {"check", "code", "message"} <= set(payload)
        assert payload["code"].isupper()


def test_the_report_summarizes_every_problem_not_just_the_first(
    validator: TaskValidator, graph: TaskGraph, world: WorldModel
) -> None:
    graph.nodes[0].parameters["bogus"] = 1
    graph.nodes[1].parameters["bogus"] = 1
    report = validator.validate(graph, world)
    assert len([e for e in report.errors if e.code == "UNEXPECTED_PARAMETER"]) == 2
