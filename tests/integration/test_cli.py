"""The command line, which is how Phase 0 is actually operated.

The CLI is the only interface that works without a ROS install, so it is worth
testing as an interface rather than as a wrapper: exit codes have to mean
something to a shell, `--json` has to be parseable, and a failed task must not
exit zero.
"""

from __future__ import annotations

import json

import pytest

from brash_orchestrator_core.cli import main
from tools.fleet_stub.server import FaultInjection


@pytest.fixture
def cli(fleet_stub, monkeypatch):
    """Run the CLI against the stub fleet and the mock model."""
    monkeypatch.setenv("ORCH_TRIAL_RECORDING", "0")

    def run(*argv: str) -> int:
        return main(
            [
                "--llm-profile",
                "mock",
                "--fleet-url",
                fleet_stub.base_url,
                "--no-trials",
                *argv,
            ]
        )

    return run


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# -- plan -------------------------------------------------------------------
def test_planning_a_known_goal_exits_zero(cli, drill_goal: str) -> None:
    assert cli("plan", drill_goal) == 0


def test_the_plan_names_every_node_and_its_robot(cli, drill_goal: str, capsys) -> None:
    cli("plan", drill_goal)
    out = capsys.readouterr().out

    for action in ("transport_container", "open_container", "place_object_on_shelf"):
        assert action in out
    assert "b2w_z1_01" in out
    assert "g1_brainco_01" in out


def test_plan_json_is_machine_readable(cli, drill_goal: str, capsys) -> None:
    cli("--json", "plan", drill_goal)
    payload = _json(capsys)

    assert payload["plan"]["feasible"]
    assert payload["interpretation"]["task_graph"]["nodes"]


def test_planning_submits_nothing(cli, drill_goal: str, fleet_stub) -> None:
    cli("plan", drill_goal)
    assert not fleet_stub.stub.work_orders


def test_an_ungroundable_goal_exits_nonzero(cli) -> None:
    assert cli("plan", "Reticulate the splines in the east annex.") == 1


def test_restricting_the_fleet_to_one_robot_is_reported_as_infeasible(
    cli, drill_goal: str
) -> None:
    """The quadruped cannot open a box, and the CLI has to say so rather than try."""
    assert cli("plan", drill_goal, "--robot", "b2w_z1_01") == 1


# -- validate ---------------------------------------------------------------
@pytest.fixture
def graph_file(tmp_path, examples):
    def write(mutate=None):
        document = next(
            example["task_graph"]
            for example in examples
            if example["example_id"] == "store_drill"
        )
        document = json.loads(json.dumps(document))
        if mutate:
            mutate(document)
        path = tmp_path / "graph.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    return write


def test_a_good_graph_validates(cli, graph_file) -> None:
    assert cli("validate", graph_file()) == 0


def test_a_cycle_is_rejected(cli, graph_file) -> None:
    def add_cycle(document):
        document["nodes"][0]["depends_on"] = ["n7"]

    assert cli("validate", graph_file(add_cycle)) == 1


def test_an_invented_action_is_rejected(cli, graph_file, capsys) -> None:
    def invent(document):
        document["nodes"][0]["action"] = "teleport_object"

    assert cli("validate", graph_file(invent)) == 1
    assert "teleport_object" in capsys.readouterr().out


def test_a_missing_file_is_reported_not_raised(cli, tmp_path) -> None:
    assert cli("validate", str(tmp_path / "nope.json")) == 1


def test_validation_errors_are_machine_readable(cli, graph_file, capsys) -> None:
    def invent(document):
        document["nodes"][0]["action"] = "teleport_object"

    cli("--json", "validate", graph_file(invent))
    report = _json(capsys)

    assert report["ok"] is False
    assert report["errors"]


# -- run --------------------------------------------------------------------
def test_a_successful_run_exits_zero(cli, drill_goal: str) -> None:
    assert cli("run", drill_goal) == 0


def test_a_run_streams_its_events(cli, drill_goal: str, capsys) -> None:
    cli("run", drill_goal)
    out = capsys.readouterr().out

    assert "TASK_ACCEPTED" in out
    assert "NODE_SUCCEEDED" in out
    assert "TASK_SUCCEEDED" in out


def test_quiet_suppresses_the_stream_but_not_the_summary(
    cli, drill_goal: str, capsys
) -> None:
    cli("run", drill_goal, "--quiet")
    out = capsys.readouterr().out

    assert "NODE_SUCCEEDED" not in out
    assert "SUCCEEDED" in out
    assert "goal verified by observation: True" in out


def test_a_dry_run_exits_zero_and_submits_nothing(
    cli, drill_goal: str, fleet_stub
) -> None:
    assert cli("run", drill_goal, "--dry-run", "--quiet") == 0
    assert not fleet_stub.stub.work_orders


def test_a_failed_run_exits_nonzero(cli, drill_goal: str, fleet_stub) -> None:
    """A shell script has to be able to tell that the robot did not do the job."""
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="open_container", error_code="LID_NOT_ACCESSIBLE", remaining=99
        )
    )
    assert cli("run", drill_goal, "--quiet") == 1


def test_a_recovered_run_reports_what_it_did(cli, drill_goal: str, fleet_stub, capsys) -> None:
    fleet_stub.stub.add_fault(
        FaultInjection(
            primitive="open_container", error_code="LID_NOT_ACCESSIBLE", remaining=1
        )
    )
    assert cli("run", drill_goal, "--quiet") == 0
    out = capsys.readouterr().out

    assert "recovery actions: 1" in out
    assert "reposition_then_retry" in out


def test_run_json_carries_the_whole_record(cli, drill_goal: str, capsys) -> None:
    cli("--json", "run", drill_goal)
    payload = _json(capsys)

    assert payload["success"] is True
    assert payload["outcome"]["goal_verified"] is True
    assert payload["events"]


def test_two_runs_of_the_same_goal_are_distinct_tasks(
    cli, move_goal: str, fleet_stub
) -> None:
    """Reusing a task id would make the fleet replay the first run's result."""
    cli("run", move_goal, "--quiet")
    first = len(fleet_stub.stub.work_orders)
    cli("run", move_goal, "--quiet")

    assert len(fleet_stub.stub.work_orders) > first


def test_an_explicit_task_id_is_honoured(cli, move_goal: str, fleet_stub) -> None:
    cli("run", move_goal, "--quiet", "--task-id", "nightly-001")

    references = [order.external_reference for order in fleet_stub.stub.work_orders.values()]
    assert all(reference.startswith("brash-task:nightly-001:") for reference in references)


# -- inspect ----------------------------------------------------------------
def test_inspect_reports_the_backend_and_the_library(cli, capsys) -> None:
    assert cli("inspect") == 0
    out = capsys.readouterr().out

    assert "reachable: True" in out
    assert "manipulation" in out
    assert "open_container" in out


def test_inspect_distinguishes_observed_facts_from_claims(cli, capsys) -> None:
    cli("inspect")
    out = capsys.readouterr().out

    assert "[observed]" in out


def test_inspect_fails_when_the_fleet_is_down(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_TRIAL_RECORDING", "0")
    exit_code = main(
        [
            "--llm-profile",
            "mock",
            "--fleet-url",
            "http://127.0.0.1:1",
            "--no-trials",
            "inspect",
        ]
    )
    assert exit_code == 1


# -- argument handling ------------------------------------------------------
def test_an_unknown_command_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit):
        main(["frobnicate"])


def test_an_unexpected_error_is_reported_without_a_traceback(cli, capsys) -> None:
    assert main(["--scenario", "no-such-file.yaml", "--no-trials", "inspect"]) == 1
    assert "error:" in capsys.readouterr().err
