"""Typed task graph: the only structure the interpreter is allowed to emit.

Dependencies are physical ordering constraints, nothing more. Everything the
graph does not order is free to run in parallel, which is why the drill scenario
can inspect the destination shelf while the box is still being carried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping


class TaskGraphError(ValueError):
    """Raised when a graph is structurally impossible to execute."""


@dataclass
class TaskNode:
    id: str
    action: str
    parameters: dict[str, Any] = field(default_factory=dict)
    required_capabilities: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    proposed_robot_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "action": self.action,
            "parameters": dict(self.parameters),
            "required_capabilities": list(self.required_capabilities),
            "depends_on": list(self.depends_on),
        }
        if self.proposed_robot_id:
            payload["proposed_robot_id"] = self.proposed_robot_id
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TaskNode:
        return cls(
            id=str(raw["id"]),
            action=str(raw["action"]),
            parameters=dict(raw.get("parameters") or {}),
            required_capabilities=tuple(raw.get("required_capabilities") or ()),
            depends_on=tuple(raw.get("depends_on") or ()),
            proposed_robot_id=raw.get("proposed_robot_id"),
        )


@dataclass
class TaskGraph:
    task_id: str
    goal_predicate: str
    nodes: list[TaskNode] = field(default_factory=list)
    rationale: str = ""

    def __iter__(self) -> Iterator[TaskNode]:
        return iter(self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "goal_predicate": self.goal_predicate,
            "nodes": [node.to_dict() for node in self.nodes],
        }
        if self.rationale:
            payload["rationale"] = self.rationale
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> TaskGraph:
        try:
            nodes = [TaskNode.from_dict(node) for node in raw["nodes"]]
        except (KeyError, TypeError) as error:
            raise TaskGraphError(f"task graph is missing usable nodes: {error}") from error
        return cls(
            task_id=str(raw.get("task_id", "")),
            goal_predicate=str(raw.get("goal_predicate", "")),
            nodes=nodes,
            rationale=str(raw.get("rationale", "")),
        )

    # ------------------------------------------------------------------
    # Structure
    # ------------------------------------------------------------------
    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.nodes)

    def node(self, node_id: str) -> TaskNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise TaskGraphError(f"unknown node {node_id!r}")

    def has_node(self, node_id: str) -> bool:
        return any(node.id == node_id for node in self.nodes)

    def duplicate_ids(self) -> list[str]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for node in self.nodes:
            if node.id in seen and node.id not in duplicates:
                duplicates.append(node.id)
            seen.add(node.id)
        return duplicates

    def dangling_dependencies(self) -> list[tuple[str, str]]:
        known = set(self.node_ids)
        return [
            (node.id, dependency)
            for node in self.nodes
            for dependency in node.depends_on
            if dependency not in known
        ]

    def dependents(self, node_id: str) -> list[str]:
        return [node.id for node in self.nodes if node_id in node.depends_on]

    def ancestors(self, node_id: str) -> set[str]:
        """Every node that must complete before ``node_id`` may start."""
        collected: set[str] = set()
        frontier = list(self.node(node_id).depends_on)
        while frontier:
            current = frontier.pop()
            if current in collected or not self.has_node(current):
                continue
            collected.add(current)
            frontier.extend(self.node(current).depends_on)
        return collected

    def descendants(self, node_id: str) -> set[str]:
        collected: set[str] = set()
        frontier = self.dependents(node_id)
        while frontier:
            current = frontier.pop()
            if current in collected:
                continue
            collected.add(current)
            frontier.extend(self.dependents(current))
        return collected

    def find_cycle(self) -> list[str] | None:
        """Return one cycle as a node-id path, or ``None`` when acyclic."""
        WHITE, GREY, BLACK = 0, 1, 2
        colour = {node.id: WHITE for node in self.nodes}
        path: list[str] = []

        def visit(node_id: str) -> list[str] | None:
            colour[node_id] = GREY
            path.append(node_id)
            for dependency in self.node(node_id).depends_on:
                if dependency not in colour:
                    continue
                if colour[dependency] == GREY:
                    start = path.index(dependency)
                    return path[start:] + [dependency]
                if colour[dependency] == WHITE:
                    found = visit(dependency)
                    if found is not None:
                        return found
            path.pop()
            colour[node_id] = BLACK
            return None

        for node in self.nodes:
            if colour[node.id] == WHITE:
                found = visit(node.id)
                if found is not None:
                    return found
        return None

    def topological_order(self) -> list[str]:
        """Kahn ordering, ties broken by declaration order for reproducibility."""
        if self.duplicate_ids():
            raise TaskGraphError(f"duplicate node ids: {self.duplicate_ids()}")
        dangling = self.dangling_dependencies()
        if dangling:
            raise TaskGraphError(f"dependencies on unknown nodes: {dangling}")

        position = {node.id: index for index, node in enumerate(self.nodes)}
        remaining = {node.id: set(node.depends_on) for node in self.nodes}
        ordered: list[str] = []
        while remaining:
            ready = sorted(
                (node_id for node_id, pending in remaining.items() if not pending),
                key=lambda node_id: position[node_id],
            )
            if not ready:
                cycle = self.find_cycle() or sorted(remaining)
                raise TaskGraphError(f"task graph contains a cycle: {' -> '.join(cycle)}")
            for node_id in ready:
                ordered.append(node_id)
                del remaining[node_id]
            for pending in remaining.values():
                pending.difference_update(ready)
        return ordered

    def execution_layers(self) -> list[list[str]]:
        """Group nodes into waves that may run concurrently."""
        position = {node.id: index for index, node in enumerate(self.nodes)}
        remaining = {node.id: set(node.depends_on) for node in self.nodes}
        layers: list[list[str]] = []
        while remaining:
            ready = sorted(
                (node_id for node_id, pending in remaining.items() if not pending),
                key=lambda node_id: position[node_id],
            )
            if not ready:
                cycle = self.find_cycle() or sorted(remaining)
                raise TaskGraphError(f"task graph contains a cycle: {' -> '.join(cycle)}")
            layers.append(ready)
            for node_id in ready:
                del remaining[node_id]
            for pending in remaining.values():
                pending.difference_update(ready)
        return layers

    # ------------------------------------------------------------------
    # Mutation used by recovery
    # ------------------------------------------------------------------
    def next_node_id(self, prefix: str = "n") -> str:
        used = {node.id for node in self.nodes}
        index = len(used) + 1
        while f"{prefix}{index}" in used:
            index += 1
        return f"{prefix}{index}"

    def insert_before(self, node_id: str, new_node: TaskNode) -> None:
        """Insert a node that ``node_id`` will then depend on.

        The new node inherits the target's dependencies so it cannot start
        earlier than the target could have, and the target depends only on the
        new node plus whatever it already required.
        """
        target = self.node(node_id)
        if self.has_node(new_node.id):
            raise TaskGraphError(f"node {new_node.id!r} already exists")
        new_node.depends_on = tuple(target.depends_on)
        target.depends_on = tuple(dict.fromkeys((*target.depends_on, new_node.id)))
        insert_at = self.nodes.index(target)
        self.nodes.insert(insert_at, new_node)

    def add_nodes(self, nodes: Iterable[TaskNode]) -> None:
        for node in nodes:
            if self.has_node(node.id):
                raise TaskGraphError(f"node {node.id!r} already exists")
            self.nodes.append(node)
