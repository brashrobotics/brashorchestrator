"""Ground predicate parsing and formatting.

Predicates are the only currency shared between the action library, the
validator, the monitor, and the recovery planner. Keeping them as a small typed
value object rather than raw strings is what stops "state(box_07, open)" and
"state(box_07,open)" from being treated as different facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PREDICATE_PATTERN = re.compile(r"^\s*(?P<name>[a-z_][a-z0-9_]*)\s*\((?P<args>[^()]*)\)\s*$")

#: Predicates whose truth is derived from entity attributes rather than stored
#: as facts. The world model answers these by inspection, so asserting them
#: directly is a bug in an action definition.
DERIVED_PREDICATES = frozenset(
    {
        "location_exists",
        "mass_within_limit",
        "object_exists",
        "object_type",
        "robot_reachable",
        "route_available",
        "sensor_view_available",
        "shelf_has_free_space",
        "shelf_height_supported",
    }
)

#: Assertion of one of these retracts its positive counterpart instead of being
#: stored, so an action can express "no longer inside" without a separate
#: negation syntax in the YAML.
NEGATED_PREDICATES = {
    "not_inside": "inside",
    "not_held": "held_by",
    "not_at": "at",
}


class PredicateError(ValueError):
    """Raised when a predicate string is malformed."""


@dataclass(frozen=True, slots=True)
class Predicate:
    """A ground predicate such as ``on(drill_01, shelf_B12)``."""

    name: str
    args: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.name}({', '.join(self.args)})"

    @property
    def arity(self) -> int:
        return len(self.args)

    def with_args(self, args: tuple[str, ...]) -> Predicate:
        return Predicate(self.name, args)


def parse_predicate(text: str) -> Predicate:
    """Parse ``name(arg, arg)`` into a :class:`Predicate`."""
    match = _PREDICATE_PATTERN.match(text)
    if match is None:
        raise PredicateError(f"malformed predicate: {text!r}")
    raw_args = match.group("args").strip()
    if not raw_args:
        return Predicate(match.group("name"), ())
    args = tuple(part.strip() for part in raw_args.split(","))
    if any(not arg for arg in args):
        raise PredicateError(f"empty argument in predicate: {text!r}")
    return Predicate(match.group("name"), args)


def bind_predicate(
    template: str,
    parameters: dict[str, object],
    *,
    assigned_robot: str | None = None,
) -> Predicate:
    """Substitute action parameters into a predicate template.

    An argument that names a parameter is replaced by that parameter's value;
    anything else is kept as a literal, which is how ``object_type(container_id,
    container)`` binds only its first argument. The reserved token
    ``assigned_robot`` resolves to the robot the scheduler picked.
    """
    predicate = parse_predicate(template)
    bound: list[str] = []
    for arg in predicate.args:
        if arg == "assigned_robot":
            if assigned_robot is None:
                raise PredicateError(
                    f"predicate {template!r} references assigned_robot but no robot is assigned"
                )
            bound.append(assigned_robot)
        elif arg in parameters:
            bound.append(str(parameters[arg]))
        else:
            bound.append(arg)
    return predicate.with_args(tuple(bound))


def is_derived(predicate: Predicate) -> bool:
    return predicate.name in DERIVED_PREDICATES
