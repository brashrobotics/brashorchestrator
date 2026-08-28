"""The event stream and the trial record.

There is deliberately one account of a task rather than several that can
disagree, so these tests are mostly about ordering, contract conformance, and
the fact that a broken consumer cannot derail a physical task.
"""

from __future__ import annotations

import json

import pytest

from brash_orchestrator_core.events import EVENT_CONTRACT, EventLog, ExecutionEvent
from brash_orchestrator_core.schema_registry import SchemaRegistry
from brash_orchestrator_core.trial_recorder import TRIAL_CONTRACT, TrialRecorder


# -- events ----------------------------------------------------------------
def test_events_keep_the_order_they_were_recorded_in() -> None:
    log = EventLog(task_id="t1")
    for name in ("TASK_ACCEPTED", "GRAPH_GENERATED", "NODE_STARTED", "NODE_SUCCEEDED"):
        log.record(name)
    assert [event.event for event in log.events] == [
        "TASK_ACCEPTED",
        "GRAPH_GENERATED",
        "NODE_STARTED",
        "NODE_SUCCEEDED",
    ]


def test_timestamps_are_monotonic_across_the_log() -> None:
    log = EventLog(task_id="t1")
    for _ in range(20):
        log.record("NODE_STARTED")
    stamps = [event.occurred_at for event in log.events]
    assert stamps == sorted(stamps)


def test_every_event_carries_the_task_id() -> None:
    log = EventLog(task_id="t1")
    log.record("NODE_STARTED", node_id="n1")
    assert all(event.task_id == "t1" for event in log.events)


def test_subscribers_see_events_as_they_happen() -> None:
    log = EventLog(task_id="t1")
    seen: list[str] = []
    log.subscribe(lambda event: seen.append(event.event))
    log.record("NODE_STARTED")
    log.record("NODE_SUCCEEDED")
    assert seen == ["NODE_STARTED", "NODE_SUCCEEDED"]


def test_a_broken_subscriber_cannot_derail_a_physical_task() -> None:
    log = EventLog(task_id="t1")
    survivors: list[str] = []

    def explode(event: ExecutionEvent) -> None:
        raise RuntimeError("the operator dashboard fell over")

    log.subscribe(explode)
    log.subscribe(lambda event: survivors.append(event.event))
    log.record("NODE_STARTED")

    assert survivors == ["NODE_STARTED"]
    assert len(log.events) == 1


def test_failures_are_filterable() -> None:
    log = EventLog(task_id="t1")
    log.record("NODE_SUCCEEDED", node_id="n1")
    log.record("NODE_FAILED", node_id="n2", error_code="GRASP_FAILED")
    assert [event.node_id for event in log.failures()] == ["n2"]


def test_per_node_history_is_what_the_recovery_planner_reads() -> None:
    log = EventLog(task_id="t1")
    log.record("NODE_FAILED", node_id="n4", error_code="LID_NOT_ACCESSIBLE")
    log.record("NODE_FAILED", node_id="n5", error_code="GRASP_FAILED")
    log.record("RECOVERY_APPLIED", node_id="n4")
    assert [event.event for event in log.for_node("n4")] == [
        "NODE_FAILED",
        "RECOVERY_APPLIED",
    ]


def test_a_success_code_is_not_a_failure() -> None:
    assert not ExecutionEvent(task_id="t", node_id="n", event="NODE_SUCCEEDED").is_failure


def test_every_event_kind_the_manager_emits_satisfies_the_contract(
    schemas: SchemaRegistry,
) -> None:
    log = EventLog(task_id="t1")
    for name in (
        "TASK_ACCEPTED",
        "GRAPH_GENERATED",
        "GRAPH_VALIDATED",
        "PLAN_ASSIGNED",
        "NODE_STARTED",
        "NODE_SUCCEEDED",
        "EFFECT_VERIFIED",
        "EFFECT_MISMATCH",
        "NODE_SKIPPED",
        "RECOVERY_PROPOSED",
        "RECOVERY_APPLIED",
        "TASK_SUCCEEDED",
    ):
        log.record(name, node_id="n1", action="open_container", robot_id="g1_brainco_01")
    log.record("NODE_FAILED", node_id="n1", error_code="LID_NOT_ACCESSIBLE")

    for payload in log.to_list():
        errors = schemas.validation_errors("execution_event", payload)
        assert not errors, f"{payload['event']}: {[str(e) for e in errors]}"


def test_the_event_contract_matches_the_schema(schemas: SchemaRegistry) -> None:
    assert schemas.contract_id("execution_event") == EVENT_CONTRACT


def test_describe_reads_as_one_line_of_narrative() -> None:
    event = ExecutionEvent(
        task_id="t1",
        node_id="n4",
        event="NODE_FAILED",
        error_code="LID_NOT_ACCESSIBLE",
        action="open_container",
        robot_id="g1_brainco_01",
        detail="the lid faces the wall",
    )
    text = event.describe()
    assert "n4" in text and "open_container" in text
    assert "[LID_NOT_ACCESSIBLE]" in text
    assert "the lid faces the wall" in text


# -- trial records ---------------------------------------------------------
@pytest.fixture
def recorder(tmp_path) -> TrialRecorder:
    return TrialRecorder(path=tmp_path / "trials.jsonl")


def _record(recorder: TrialRecorder, **overrides) -> dict:
    payload = dict(
        task_id="t1",
        goal="Put the drill on the shelf.",
        constraints={},
        world_before={"facts": []},
        interpretation={"valid_first_try": True, "attempts": [{}]},
        plan={"feasible": True},
        outcome={
            "success": True,
            "goal_verified": True,
            "duration_s": 12.5,
            "recovery_actions": [],
            "human_interventions": 0,
            "nodes": [
                {
                    "node_id": "n1",
                    "action": "locate_object",
                    "assigned_robot_id": "b2w_z1_01",
                    "state": "SUCCEEDED",
                    "error_code": "SUCCESS",
                    "attempts": 1,
                    "duration_s": 3.0,
                }
            ],
        },
    )
    payload.update(overrides)
    return recorder.record(**payload)


def test_a_trial_is_appended_as_one_json_line(recorder: TrialRecorder) -> None:
    _record(recorder)
    _record(recorder, task_id="t2")
    lines = recorder.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["task_id"] for line in lines] == ["t1", "t2"]


def test_the_record_declares_its_contract(recorder: TrialRecorder) -> None:
    assert _record(recorder)["contract"] == TRIAL_CONTRACT


def test_the_supervision_labels_are_precomputed(recorder: TrialRecorder) -> None:
    """Analysis should not have to re-derive the metrics that matter."""
    labels = _record(recorder)["labels"]
    assert labels["graph_valid_first_attempt"] is True
    assert labels["task_success"] is True
    assert labels["goal_verified"] is True
    assert labels["repair_attempts"] == 0
    assert labels["duration_s"] == 12.5


def test_repair_attempts_are_counted_from_the_attempt_list(recorder: TrialRecorder) -> None:
    record = _record(
        recorder, interpretation={"valid_first_try": False, "attempts": [{}, {}, {}]}
    )
    assert record["labels"]["repair_attempts"] == 2


def test_per_node_labels_pair_the_estimate_with_the_measurement(
    recorder: TrialRecorder,
) -> None:
    """This is what turns duration estimates from guesses into measurements."""
    node = _record(recorder)["labels"]["nodes"][0]
    assert node["node_id"] == "n1"
    assert node["succeeded"] is True
    assert node["observed_duration_s"] == 3.0
    assert node["assigned_robot_id"] == "b2w_z1_01"


def test_a_rejected_task_still_produces_a_record(recorder: TrialRecorder) -> None:
    """A task the system refused is exactly the kind worth learning from."""
    record = _record(recorder, interpretation=None, plan=None, outcome=None)
    assert record["labels"]["task_success"] is False
    assert record["labels"]["plan_feasible"] is False
    assert recorder.path.is_file()


def test_recording_can_be_switched_off(tmp_path) -> None:
    recorder = TrialRecorder(path=tmp_path / "trials.jsonl", enabled=False)
    record = _record(recorder)
    assert record["task_id"] == "t1"
    assert not recorder.path.exists()


def test_read_all_round_trips_what_was_written(recorder: TrialRecorder) -> None:
    _record(recorder)
    _record(recorder, task_id="t2")
    assert [item["task_id"] for item in recorder.read_all()] == ["t1", "t2"]


def test_read_all_is_empty_before_anything_is_written(recorder: TrialRecorder) -> None:
    assert list(recorder.read_all()) == []
