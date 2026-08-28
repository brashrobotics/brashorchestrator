"""Task graph structure: ordering, cycles, parallelism, and recovery edits."""

from __future__ import annotations

import pytest

from brash_orchestrator_core.task_graph import TaskGraph, TaskGraphError, TaskNode


def _graph(*edges: tuple[str, tuple[str, ...]]) -> TaskGraph:
    return TaskGraph(
        task_id="t",
        goal_predicate="on(a, b)",
        nodes=[
            TaskNode(id=node_id, action="noop", depends_on=depends)
            for node_id, depends in edges
        ],
    )


def test_topological_order_respects_dependencies() -> None:
    graph = _graph(("a", ()), ("b", ("a",)), ("c", ("b",)))
    assert graph.topological_order() == ["a", "b", "c"]


def test_topological_order_is_stable_across_calls() -> None:
    """A plan that reorders itself between runs is not reproducible."""
    graph = _graph(("a", ()), ("b", ()), ("c", ("a", "b")))
    assert graph.topological_order() == graph.topological_order() == ["a", "b", "c"]


def test_execution_layers_group_independent_nodes() -> None:
    graph = _graph(("a", ()), ("b", ()), ("c", ("a",)), ("d", ("a", "b")))
    assert graph.execution_layers() == [["a", "b"], ["c", "d"]]


def test_the_drill_scenario_overlaps_transport_and_inspection(drill_graph: TaskGraph) -> None:
    """The reference scenario's whole point is that two robots work at once."""
    layers = drill_graph.execution_layers()
    parallel = [layer for layer in layers if len(layer) > 1]
    assert parallel, f"no wave runs in parallel: {layers}"
    actions = {
        drill_graph.node(node_id).action for layer in parallel for node_id in layer
    }
    assert "inspect_target_shelf" in actions


def test_find_cycle_returns_the_offending_path() -> None:
    graph = _graph(("a", ("c",)), ("b", ("a",)), ("c", ("b",)))
    cycle = graph.find_cycle()
    assert cycle is not None
    assert cycle[0] == cycle[-1], "a cycle should be reported as a closed path"
    assert set(cycle) == {"a", "b", "c"}


def test_find_cycle_returns_none_for_a_dag(drill_graph: TaskGraph) -> None:
    assert drill_graph.find_cycle() is None


def test_topological_order_raises_on_a_cycle() -> None:
    graph = _graph(("a", ("b",)), ("b", ("a",)))
    with pytest.raises(TaskGraphError, match="cycle"):
        graph.topological_order()


def test_a_two_node_cycle_is_detected() -> None:
    graph = _graph(("a", ("b",)), ("b", ("a",)))
    assert graph.find_cycle() is not None


def test_self_dependency_is_reported_as_a_cycle() -> None:
    graph = _graph(("a", ("a",)))
    assert graph.find_cycle() == ["a", "a"]


def test_topological_order_raises_on_dangling_dependencies() -> None:
    graph = _graph(("a", ("ghost",)))
    with pytest.raises(TaskGraphError, match="unknown nodes"):
        graph.topological_order()


def test_duplicate_ids_are_reported() -> None:
    graph = _graph(("a", ()), ("a", ()))
    assert graph.duplicate_ids() == ["a"]


def test_descendants_cover_the_transitive_closure() -> None:
    graph = _graph(("a", ()), ("b", ("a",)), ("c", ("b",)), ("d", ()))
    assert graph.descendants("a") == {"b", "c"}
    assert graph.descendants("d") == set()


def test_ancestors_cover_the_transitive_closure() -> None:
    graph = _graph(("a", ()), ("b", ("a",)), ("c", ("b",)))
    assert graph.ancestors("c") == {"a", "b"}


def test_insert_before_makes_the_target_wait() -> None:
    graph = _graph(("a", ()), ("b", ("a",)))
    graph.insert_before("b", TaskNode(id="x", action="reposition_container"))

    assert "x" in graph.node("b").depends_on
    # The inserted node inherits the target's dependencies, so it cannot start
    # earlier than the target could have.
    assert graph.node("x").depends_on == ("a",)
    assert graph.topological_order() == ["a", "x", "b"]


def test_insert_before_keeps_the_graph_acyclic() -> None:
    graph = _graph(("a", ()), ("b", ("a",)), ("c", ("b",)))
    graph.insert_before("b", TaskNode(id="x", action="noop"))
    assert graph.find_cycle() is None


def test_insert_before_rejects_a_duplicate_id() -> None:
    graph = _graph(("a", ()), ("b", ("a",)))
    with pytest.raises(TaskGraphError, match="already exists"):
        graph.insert_before("b", TaskNode(id="a", action="noop"))


def test_next_node_id_avoids_collisions() -> None:
    graph = _graph(("n1", ()), ("n2", ()), ("n3", ()))
    new_id = graph.next_node_id()
    assert new_id not in graph.node_ids


def test_round_trip_through_dict_preserves_structure(drill_graph: TaskGraph) -> None:
    restored = TaskGraph.from_dict(drill_graph.to_dict())
    assert restored.node_ids == drill_graph.node_ids
    assert restored.goal_predicate == drill_graph.goal_predicate
    assert restored.topological_order() == drill_graph.topological_order()


def test_from_dict_rejects_a_document_without_nodes() -> None:
    with pytest.raises(TaskGraphError):
        TaskGraph.from_dict({"task_id": "t", "goal_predicate": "on(a, b)"})
