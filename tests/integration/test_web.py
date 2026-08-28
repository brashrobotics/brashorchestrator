"""The web console, driven over real HTTP and a real WebSocket.

The application is started for each test and exercised through aiohttp's test
client, so routing, JSON shapes, error mapping, and the live stream are all
tested on the code path a browser uses. The LLM is the mock provider and the
fleet is the stub, which keeps the whole thing hermetic.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from brash_orchestrator_core.web.app import HUB, build_app
from brash_orchestrator_core.web.hub import TaskHub


@pytest.fixture
async def console(orchestrator, fleet_stub):
    """A running console over the mock LLM and the stub fleet."""
    hub = TaskHub(orchestrator)
    client = TestClient(TestServer(build_app(hub)))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()
        await orchestrator.close()


def hub_of(console: TestClient) -> TaskHub:
    return console.server.app[HUB]


async def wait_for_task(console: TestClient, task_id: str, timeout: float = 30.0) -> dict[str, Any]:
    """Poll one task until it reaches a terminal state."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        response = await console.get(f"/api/tasks/{task_id}")
        assert response.status == 200
        payload = await response.json()
        if payload["state"] != "RUNNING":
            return payload
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {task_id} never finished")


async def run_goal(console: TestClient, goal: str, mode: str = "execute", **body: Any) -> dict[str, Any]:
    response = await console.post("/api/tasks", json={"goal": goal, "mode": mode, **body})
    assert response.status == 202, await response.text()
    accepted = await response.json()
    return await wait_for_task(console, accepted["task_id"])


# ----------------------------------------------------------------------
# The page itself
# ----------------------------------------------------------------------
async def test_the_console_page_is_served(console: TestClient) -> None:
    response = await console.get("/")
    assert response.status == 200
    assert "BrashOrchestrator Console" in await response.text()


@pytest.mark.parametrize("asset", ["app.js", "styles.css"])
async def test_the_browser_assets_ship_with_the_package(console: TestClient, asset: str) -> None:
    """A zero-build front end is only zero-build if the files are installed."""
    response = await console.get(f"/static/{asset}")
    assert response.status == 200
    assert await response.text()


# ----------------------------------------------------------------------
# Introspection
# ----------------------------------------------------------------------
async def test_health_reports_the_active_profile_and_a_reachable_backend(
    console: TestClient,
) -> None:
    payload = await (await console.get("/api/health")).json()
    assert payload["llm"]["profile"] == "mock"
    assert payload["llm"]["mock"] is True
    assert payload["backend"]["reachable"] is True
    assert "brash.manipulation-work-order/v1" in payload["backend"]["extensions"]
    assert payload["running_task_id"] is None


async def test_the_library_offers_the_goals_the_model_can_actually_answer(
    console: TestClient, drill_goal: str
) -> None:
    """Under the mock profile these are the only goals that resolve.

    Surfacing them is what makes the console demonstrate the pipeline instead of
    demonstrating interpretation failures.
    """
    payload = await (await console.get("/api/library")).json()
    assert drill_goal in payload["example_goals"]
    assert "open_container" in {action["name"] for action in payload["actions"]}
    assert "LID_NOT_ACCESSIBLE" in payload["error_codes"]
    assert "SUCCESS" not in payload["error_codes"]
    assert {robot["robot_id"] for robot in payload["robots"]}


async def test_the_world_endpoint_marks_how_each_fact_was_learned(console: TestClient) -> None:
    payload = await (await console.get("/api/world")).json()
    assert payload["facts"]
    assert all("verified" in fact for fact in payload["facts"])


# ----------------------------------------------------------------------
# Submission and refusal
# ----------------------------------------------------------------------
async def test_an_empty_goal_is_refused(console: TestClient) -> None:
    response = await console.post("/api/tasks", json={"goal": "   "})
    assert response.status == 400
    assert (await response.json())["error"]["code"] == "GOAL_REQUIRED"


async def test_an_unknown_mode_is_refused(console: TestClient, drill_goal: str) -> None:
    response = await console.post("/api/tasks", json={"goal": drill_goal, "mode": "wing_it"})
    assert response.status == 400
    assert (await response.json())["error"]["code"] == "UNKNOWN_MODE"


async def test_an_unknown_constraint_is_refused_rather_than_ignored(
    console: TestClient, drill_goal: str
) -> None:
    """Silently dropping a constraint would be worse than rejecting it.

    An operator who asks for a deadline and gets none must be told, because the
    alternative is a robot moving under limits nobody agreed to.
    """
    response = await console.post(
        "/api/tasks", json={"goal": drill_goal, "constraints": {"go_faster": True}}
    )
    assert response.status == 400
    payload = await response.json()
    assert payload["error"]["code"] == "UNKNOWN_CONSTRAINT"
    assert "go_faster" in payload["error"]["message"]


async def test_a_malformed_risk_ceiling_is_refused(console: TestClient, drill_goal: str) -> None:
    response = await console.post(
        "/api/tasks", json={"goal": drill_goal, "constraints": {"max_risk": "reckless"}}
    )
    assert response.status == 400
    assert (await response.json())["error"]["code"] == "INVALID_CONSTRAINTS"


async def test_a_body_that_is_not_json_is_refused(console: TestClient) -> None:
    response = await console.post(
        "/api/tasks", data="goal=drill", headers={"Content-Type": "application/json"}
    )
    assert response.status == 400
    assert (await response.json())["error"]["code"] == "INVALID_JSON"


async def test_an_unknown_task_is_a_404(console: TestClient) -> None:
    response = await console.get("/api/tasks/never-existed")
    assert response.status == 404
    assert (await response.json())["error"]["code"] == "TASK_NOT_FOUND"


# ----------------------------------------------------------------------
# Planning and execution
# ----------------------------------------------------------------------
async def test_planning_produces_a_graph_and_submits_nothing(
    console: TestClient, fleet_stub, drill_goal: str
) -> None:
    task = await run_goal(console, drill_goal, mode="plan")
    assert task["state"] == "SUCCEEDED"
    assert task["result"]["plan"]["feasible"] is True
    assert task["result"]["outcome"] is None
    assert fleet_stub.stub.submissions == [], "planning must not reach the fleet"


async def test_planning_narrates_itself_with_the_same_event_vocabulary(
    console: TestClient, drill_goal: str
) -> None:
    task = await run_goal(console, drill_goal, mode="plan")
    kinds = [event["event"] for event in task["events"]]
    assert kinds[0] == "TASK_ACCEPTED"
    assert "GRAPH_VALIDATED" in kinds
    assert "PLAN_ASSIGNED" in kinds


async def test_the_graph_travels_on_the_event_that_announces_it(
    console: TestClient, drill_goal: str
) -> None:
    """A live observer must be able to draw the plan from the event stream alone."""
    task = await run_goal(console, drill_goal, mode="plan")
    validated = next(event for event in task["events"] if event["event"] == "GRAPH_VALIDATED")
    assigned = next(event for event in task["events"] if event["event"] == "PLAN_ASSIGNED")

    graph = validated["observations"]["task_graph"]
    assert [node["id"] for node in graph["nodes"]]
    assignments = assigned["observations"]["assignments"]
    assert set(assignments) == {node["id"] for node in graph["nodes"]}
    assert all(robot for robot in assignments.values())
    assert assigned["observations"]["layers"]


async def test_executing_a_goal_verifies_it_against_the_world(
    console: TestClient, drill_goal: str
) -> None:
    task = await run_goal(console, drill_goal)
    assert task["state"] == "SUCCEEDED", task["summary"]
    assert task["result"]["outcome"]["goal_verified"] is True

    world = await (await console.get("/api/world")).json()
    assert world["objects"]["drill_01"]["location"] == "shelf_B12"


async def test_a_dry_run_walks_the_pipeline_without_touching_the_fleet(
    console: TestClient, fleet_stub, drill_goal: str
) -> None:
    task = await run_goal(console, drill_goal, mode="dry_run")
    assert task["state"] == "SUCCEEDED", task["summary"]
    assert task["result"]["outcome"]["dry_run"] is True
    assert fleet_stub.stub.submissions == []


async def test_a_goal_the_model_cannot_interpret_fails_with_a_reason(
    console: TestClient,
) -> None:
    task = await run_goal(console, "Please make me a sandwich.", mode="plan")
    assert task["state"] == "FAILED"
    assert task["failure_code"] == "VALIDATION_FAILED"
    assert task["summary"]


async def test_a_finished_task_appears_in_the_history(console: TestClient, drill_goal: str) -> None:
    task = await run_goal(console, drill_goal, mode="plan")
    listing = await (await console.get("/api/tasks")).json()
    assert listing["running_task_id"] is None
    assert listing["tasks"][0]["task_id"] == task["task_id"]


# ----------------------------------------------------------------------
# One task at a time
# ----------------------------------------------------------------------
@pytest.fixture
def stalled_planner(orchestrator):
    """Replace planning with a call that blocks until released.

    Concurrency and cancellation are properties of the hub, not of the model, so
    they are tested against something whose timing the test controls.
    """
    released = asyncio.Event()

    async def plan_task(**_: Any):
        await released.wait()
        raise AssertionError("the stalled planner should never be released by the test")

    orchestrator.plan_task = plan_task
    yield released
    released.set()


async def test_a_second_submission_is_refused_while_one_runs(
    console: TestClient, stalled_planner, drill_goal: str
) -> None:
    """One world model and one fleet means one task at a time.

    Two tasks asserting facts about the same drill would leave a world model that
    describes neither of them, so the second submission is a conflict rather than
    an interleaving.
    """
    first = await (await console.post("/api/tasks", json={"goal": drill_goal, "mode": "plan"})).json()

    response = await console.post("/api/tasks", json={"goal": drill_goal, "mode": "plan"})
    assert response.status == 409
    payload = await response.json()
    assert payload["error"]["code"] == "TASK_ALREADY_RUNNING"
    assert first["task_id"] in payload["error"]["message"]

    health = await (await console.get("/api/health")).json()
    assert health["running_task_id"] == first["task_id"]


async def test_cancelling_a_running_task_reports_it_cancelled(
    console: TestClient, stalled_planner, drill_goal: str
) -> None:
    accepted = await (
        await console.post("/api/tasks", json={"goal": drill_goal, "mode": "plan"})
    ).json()

    cancelled = await console.post(f"/api/tasks/{accepted['task_id']}/cancel")
    assert cancelled.status == 200

    task = await (await console.get(f"/api/tasks/{accepted['task_id']}")).json()
    assert task["state"] == "CANCELLED"
    assert task["failure_code"] == "CANCELLED"
    assert hub_of(console).running_task_id is None


async def test_cancelling_a_finished_task_is_a_conflict(
    console: TestClient, drill_goal: str
) -> None:
    task = await run_goal(console, drill_goal, mode="plan")
    response = await console.post(f"/api/tasks/{task['task_id']}/cancel")
    assert response.status == 409
    assert (await response.json())["error"]["code"] == "TASK_NOT_RUNNING"


async def test_resetting_is_refused_while_a_task_runs(
    console: TestClient, stalled_planner, drill_goal: str
) -> None:
    await console.post("/api/tasks", json={"goal": drill_goal, "mode": "plan"})
    response = await console.post("/api/reset")
    assert response.status == 409


# ----------------------------------------------------------------------
# Live stream
# ----------------------------------------------------------------------
async def test_the_stream_opens_with_a_full_snapshot(console: TestClient) -> None:
    """Snapshot first, on the same connection, before any live message.

    A client that fetched state over a separate request could observe an event
    about a task it has not been told exists.
    """
    async with console.ws_connect("/api/stream") as socket:
        message = await asyncio.wait_for(socket.receive_json(), timeout=10)
    assert message["type"] == "snapshot"
    assert message["health"]["llm"]["profile"] == "mock"
    assert message["library"]["actions"]
    assert message["world"]["facts"]
    assert message["current_task"] is None


async def test_a_stream_that_cannot_start_fails_as_http_not_as_a_broken_socket(
    console: TestClient,
) -> None:
    """Everything that can fail must fail before the upgrade.

    Once the connection is a WebSocket there is no way to report an HTTP error on
    it, and writing one anyway reaches the browser as protocol corruption rather
    than as a message it can show.
    """
    hub = hub_of(console)

    async def broken_snapshot() -> dict[str, Any]:
        raise RuntimeError("the world model is unreadable")

    hub.snapshot = broken_snapshot
    with pytest.raises(Exception):
        async with console.ws_connect("/api/stream"):
            pass
    assert hub.subscribers == set(), "a failed connection must not leave a subscriber behind"


async def test_the_stream_carries_a_task_from_start_to_finish(
    console: TestClient, drill_goal: str
) -> None:
    async with console.ws_connect("/api/stream") as socket:
        assert (await asyncio.wait_for(socket.receive_json(), timeout=10))["type"] == "snapshot"

        accepted = await (
            await console.post("/api/tasks", json={"goal": drill_goal, "mode": "execute"})
        ).json()

        kinds: list[str] = []
        events: list[str] = []
        while "task_finished" not in kinds:
            message = await asyncio.wait_for(socket.receive_json(), timeout=30)
            kinds.append(message["type"])
            if message["type"] == "event":
                assert message["task_id"] == accepted["task_id"]
                events.append(message["event"]["event"])

    assert kinds[0] == "task_started"
    assert "event" in kinds
    assert kinds[-1] == "task_finished"
    assert events[0] == "TASK_ACCEPTED"
    assert "NODE_SUCCEEDED" in events
    assert "TASK_SUCCEEDED" in events


async def test_the_stream_announces_the_new_world_after_a_task(
    console: TestClient, drill_goal: str
) -> None:
    async with console.ws_connect("/api/stream") as socket:
        await asyncio.wait_for(socket.receive_json(), timeout=10)
        await console.post("/api/tasks", json={"goal": drill_goal, "mode": "execute"})

        worlds = []
        while not worlds:
            message = await asyncio.wait_for(socket.receive_json(), timeout=30)
            if message["type"] == "world":
                worlds.append(message["world"])

    assert worlds[0]["objects"]["drill_01"]["location"] == "shelf_B12"


async def test_a_browser_that_falls_behind_is_told_so_rather_than_blocking_a_task() -> None:
    """Back pressure is resolved by dropping history, never by waiting.

    A browser left open on a laptop lid must not be able to slow down a physical
    task, so the oldest message is discarded and the client is informed.
    """
    from brash_orchestrator_core.web.hub import SUBSCRIBER_BACKLOG, Subscriber

    subscriber = Subscriber()
    for index in range(SUBSCRIBER_BACKLOG + 5):
        subscriber.offer({"type": "event", "index": index})

    assert subscriber.dropped == 5
    assert subscriber.queue.qsize() == SUBSCRIBER_BACKLOG
    assert subscriber.queue.get_nowait()["index"] == 5, "the oldest messages are the ones lost"


# ----------------------------------------------------------------------
# Operator controls
# ----------------------------------------------------------------------
async def test_an_approval_can_be_granted_and_revoked(console: TestClient) -> None:
    granted = await (
        await console.post("/api/approvals", json={"token": "zone:handover", "granted": True})
    ).json()
    assert "zone:handover" in granted["approvals"]

    revoked = await (
        await console.post("/api/approvals", json={"token": "zone:handover", "granted": False})
    ).json()
    assert "zone:handover" not in revoked["approvals"]


async def test_an_empty_approval_token_is_refused(console: TestClient) -> None:
    response = await console.post("/api/approvals", json={"token": ""})
    assert response.status == 400


# ----------------------------------------------------------------------
# Developer surface
# ----------------------------------------------------------------------
async def test_the_fault_surface_is_advertised_because_the_backend_is_a_stub(
    console: TestClient,
) -> None:
    """The probe asks for the capability, it does not sniff a server banner.

    Declaring no faults is rejected by the stub by design, and a real Fleet
    Control has no such route at all.
    """
    health = await (await console.get("/api/health")).json()
    assert health["control_surface"] is True


async def test_an_injected_fault_forces_recovery_and_the_task_still_finishes(
    console: TestClient, drill_goal: str
) -> None:
    declared = await console.post(
        "/api/faults",
        json={"primitive": "open_container", "errorCode": "LID_NOT_ACCESSIBLE", "remaining": 1},
    )
    assert declared.status == 200
    assert (await declared.json())["declared"] == 1

    task = await run_goal(console, drill_goal)
    kinds = [event["event"] for event in task["events"]]
    assert "NODE_FAILED" in kinds
    assert "RECOVERY_APPLIED" in kinds
    assert task["state"] == "SUCCEEDED", task["summary"]


async def test_a_fault_declaring_nothing_is_refused(console: TestClient) -> None:
    response = await console.post("/api/faults", json={"remaining": 3})
    assert response.status == 400
    assert (await response.json())["error"]["code"] == "CONTRACT_VALIDATION_FAILED"


async def test_resetting_reloads_the_scenario_and_clears_faults(
    console: TestClient, drill_goal: str
) -> None:
    await run_goal(console, drill_goal)
    moved = await (await console.get("/api/world")).json()
    assert moved["objects"]["drill_01"]["location"] == "shelf_B12"

    reset = await (await console.post("/api/reset")).json()
    assert reset["fleet_reset"] is True

    restored = await (await console.get("/api/world")).json()
    assert restored["objects"]["drill_01"]["location"] == "box_07"
    assert (await (await console.get("/api/tasks")).json())["tasks"] == []
