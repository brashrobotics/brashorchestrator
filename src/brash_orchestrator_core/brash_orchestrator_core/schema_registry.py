"""JSON Schema loading and validation.

Schema violations are converted into the same :class:`ValidationError` shape the
task validator produces, so the interpreter's repair loop has one error format
to work with regardless of whether the problem was structural or semantic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import json

from brash_orchestrator_core.paths import repository_root
from brash_orchestrator_core.task_validator import ValidationError

#: Logical name to schema file.
SCHEMA_FILES = {
    "task_request": "task_request.schema.json",
    "task_graph": "task_graph.schema.json",
    "execution_event": "execution_event.schema.json",
    "recovery_proposal": "recovery_proposal.schema.json",
    "execution_result": "execution_result.schema.json",
}


class SchemaError(RuntimeError):
    """Raised when a schema is missing or unusable."""


@dataclass
class SchemaRegistry:
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: str | Path | None = None) -> SchemaRegistry:
        root = Path(directory) if directory is not None else repository_root() / "schemas"
        registry = cls()
        for name, filename in SCHEMA_FILES.items():
            path = root / filename
            if not path.is_file():
                raise SchemaError(f"schema {name!r} not found at {path}")
            registry.schemas[name] = json.loads(path.read_text(encoding="utf-8"))
        return registry

    def get(self, name: str) -> dict[str, Any]:
        try:
            return self.schemas[name]
        except KeyError as error:
            raise SchemaError(f"unknown schema {name!r}") from error

    def contract_id(self, name: str) -> str:
        return str(self.get(name).get("$id", name))

    def validation_errors(self, name: str, document: Mapping[str, Any]) -> list[ValidationError]:
        """Validate a document, returning repairable errors rather than raising."""
        validator = _build_validator(name, json.dumps(self.get(name), sort_keys=True))
        errors: list[ValidationError] = []
        for problem in validator.iter_errors(dict(document)):
            path = list(problem.absolute_path)
            node_id, field_name = _locate(path, document)
            errors.append(
                ValidationError(
                    check="schema",
                    code="SCHEMA_VIOLATION",
                    message=_describe(problem, path),
                    node_id=node_id,
                    field=field_name,
                )
            )
        return errors

    def is_valid(self, name: str, document: Mapping[str, Any]) -> bool:
        return not self.validation_errors(name, document)

    def assert_valid(self, name: str, document: Mapping[str, Any]) -> None:
        errors = self.validation_errors(name, document)
        if errors:
            joined = "; ".join(str(error) for error in errors)
            raise SchemaError(f"{name} document does not satisfy {self.contract_id(name)}: {joined}")


@lru_cache(maxsize=len(SCHEMA_FILES) * 2)
def _build_validator(name: str, serialized_schema: str) -> Any:
    """Compile once per schema. Draft 2020-12 compilation is not cheap."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError as error:  # pragma: no cover - dependency guard
        raise SchemaError("jsonschema is required for contract validation") from error
    schema = json.loads(serialized_schema)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _locate(path: list[Any], document: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Map a JSON pointer path onto a node id and field name where possible."""
    if len(path) >= 2 and path[0] == "nodes" and isinstance(path[1], int):
        nodes = document.get("nodes")
        node_id = None
        if isinstance(nodes, list) and path[1] < len(nodes):
            candidate = nodes[path[1]]
            if isinstance(candidate, Mapping):
                node_id = candidate.get("id")
        field_name = ".".join(str(part) for part in path[2:]) or None
        return (str(node_id) if node_id else f"nodes[{path[1]}]", field_name)
    return (None, ".".join(str(part) for part in path) or None)


def _describe(problem: Any, path: list[Any]) -> str:
    pointer = "/".join(str(part) for part in path) or "<root>"
    return f"{pointer}: {problem.message}"
