"""Trial recorder: execution-grounded training data.

The interesting supervision signal for an orchestrator is not "did a human like
this plan" but "did this plan physically work". Every trial is therefore written
as one JSONL record pairing the goal, the world state it was planned against, the
graph the model produced, the assignments the scheduler chose, and what actually
happened to each node.

That record is what later supports learning better decomposition, better cost
estimates, and better recovery ranking. It is also the audit trail: the reason a
particular robot was sent to a particular place is reconstructable months later.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

TRIAL_CONTRACT = "brash.execution-trial/v1"


@dataclass
class TrialRecorder:
    """Appends one JSON object per trial."""

    path: Path
    enabled: bool = True

    @classmethod
    def from_env(cls) -> TrialRecorder:
        directory = os.environ.get("ORCH_TRIAL_DIR", "trials")
        enabled = os.environ.get("ORCH_TRIAL_RECORDING", "1") not in ("0", "false", "False")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return cls(path=Path(directory) / f"trials-{stamp}.jsonl", enabled=enabled)

    def record(
        self,
        *,
        task_id: str,
        goal: str,
        constraints: Mapping[str, Any],
        world_before: Mapping[str, Any],
        interpretation: Mapping[str, Any] | None,
        plan: Mapping[str, Any] | None,
        outcome: Mapping[str, Any] | None,
        llm_profile: str = "",
        backend: str = "",
    ) -> dict[str, Any]:
        """Write one trial and return the record that was written."""
        record = {
            "contract": TRIAL_CONTRACT,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "task_id": task_id,
            "goal": goal,
            "constraints": dict(constraints),
            "llm_profile": llm_profile,
            "backend": backend,
            "world_before": dict(world_before),
            "interpretation": dict(interpretation) if interpretation else None,
            "plan": dict(plan) if plan else None,
            "outcome": dict(outcome) if outcome else None,
            "labels": _derive_labels(interpretation, plan, outcome),
        }
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        return record

    def read_all(self) -> Iterator[dict[str, Any]]:
        if not self.path.is_file():
            return iter(())
        return (
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )


def _derive_labels(
    interpretation: Mapping[str, Any] | None,
    plan: Mapping[str, Any] | None,
    outcome: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Precompute the supervision targets, so analysis does not re-derive them.

    These are the reference architecture's key metrics, per trial: whether the
    first generated graph was valid, whether the goal was physically verified,
    and how much human help the task needed.
    """
    labels: dict[str, Any] = {
        "graph_valid_first_attempt": bool(interpretation and interpretation.get("valid_first_try")),
        "repair_attempts": max(len((interpretation or {}).get("attempts") or []) - 1, 0),
        "plan_feasible": bool(plan and plan.get("feasible")),
        "task_success": bool(outcome and outcome.get("success")),
        "goal_verified": bool(outcome and outcome.get("goal_verified")),
        "recovery_actions": len((outcome or {}).get("recovery_actions") or []),
        "human_interventions": int((outcome or {}).get("human_interventions") or 0),
        "duration_s": float((outcome or {}).get("duration_s") or 0.0),
    }

    node_labels: list[dict[str, Any]] = []
    for node in (outcome or {}).get("nodes") or []:
        node_labels.append(
            {
                "node_id": node.get("node_id"),
                "action": node.get("action"),
                "assigned_robot_id": node.get("assigned_robot_id"),
                "succeeded": node.get("state") == "SUCCEEDED",
                "error_code": node.get("error_code"),
                "attempts": node.get("attempts"),
                # Comparing the estimate against this is what turns success
                # priors and duration estimates from guesses into measurements.
                "observed_duration_s": node.get("duration_s"),
            }
        )
    labels["nodes"] = node_labels
    return labels
