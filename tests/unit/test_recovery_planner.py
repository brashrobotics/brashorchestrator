"""Recovery planning.

The central claim being tested is structural: the LLM ranks a closed set and can
never introduce an action. Everything else is about the deterministic policy
producing options that are actually possible right now.
"""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from brash_orchestrator_core.backends.base import PrimitiveResult
from brash_orchestrator_core.events import ExecutionEvent
from brash_orchestrator_core.recovery_planner import (
    RecoveryConfigError,
    RecoveryPlanner,
    RecoveryPolicies,
    STRATEGIES,
)
from brash_orchestrator_core.task_graph import TaskGraph, TaskNode
from brash_world_model.model import WorldModel


@pytest.fixture
def graph(drill_graph: TaskGraph) -> TaskGraph:
    return copy.deepcopy(drill_graph)


def _failure(node_id: str, error_code: str, robot_id: str = "g1_brainco_01") -> ExecutionEvent:
    return ExecutionEvent(
        task_id="t1", node_id=node_id, event="NODE_FAILED", error_code=error_code,
        robot_id=robot_id,
    )


def _applied(node_id: str, candidate_id: str) -> ExecutionEvent:
    return ExecutionEvent(
        task_id="t1",
        node_id=node_id,
        event="RECOVERY_APPLIED",
        observations={"candidate_id": candidate_id},
    )


# -- policy configuration -------------------------------------------------
def test_every_shipped_policy_uses_a_known_strategy(policies: RecoveryPolicies) -> None:
    for candidates in policies.policies.values():
        for candidate in candidates:
            assert candidate.strategy in STRATEGIES


def test_every_inserted_action_exists_in_the_library(
    policies: RecoveryPolicies, library
) -> None:
    """A policy naming an action the library lacks is a silent dead end."""
    for error_code, candidates in policies.policies.items():
        for candidate in candidates:
            if candidate.strategy == "insert_before":
                assert candidate.action in library, f"{error_code}/{candidate.candidate_id}"


def test_every_policy_candidate_explains_itself(policies: RecoveryPolicies) -> None:
    for candidates in policies.policies.values():
        for candidate in candidates:
            assert candidate.rationale


def test_the_readme_failure_codes_all_have_a_policy(policies: RecoveryPolicies) -> None:
    for code in (
        "LID_NOT_ACCESSIBLE",
        "OBJECT_POSE_UNCERTAIN",
        "TARGET_OCCUPIED",
        "GRASP_FAILED",
        "SLIP_DETECTED",
        "PATH_BLOCKED",
        "ROBOT_FAULT",
        "TIMEOUT",
    ):
        assert policies.candidates_for(code), code


def test_an_insert_policy_without_an_action_is_rejected(tmp_path) -> None:
    path = tmp_path / "recovery.yaml"
    path.write_text(
        "policies:\n  X:\n    candidates:\n"
        "      - candidate_id: c\n        strategy: insert_before\n",
        encoding="utf-8",
    )
    with pytest.raises(RecoveryConfigError, match="without naming an action"):
        RecoveryPolicies.load(path)


def test_an_alternate_target_policy_without_a_parameter_is_rejected(tmp_path) -> None:
    path = tmp_path / "recovery.yaml"
    path.write_text(
        "policies:\n  X:\n    candidates:\n"
        "      - candidate_id: c\n        strategy: alternate_target\n",
        encoding="utf-8",
    )
    with pytest.raises(RecoveryConfigError, match="without naming a parameter"):
        RecoveryPolicies.load(path)


def test_an_unknown_strategy_is_rejected(tmp_path) -> None:
    path = tmp_path / "recovery.yaml"
    path.write_text(
        "policies:\n  X:\n    candidates:\n"
        "      - candidate_id: c\n        strategy: improvise\n",
        encoding="utf-8",
    )
    with pytest.raises(RecoveryConfigError, match="unknown strategy"):
        RecoveryPolicies.load(path)


# -- README section 15, scenario 1: the lid is not reachable ---------------
def test_a_lid_failure_offers_repositioning_first(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    node = graph.node("n4")
    options = recovery.build_options(
        node=node,
        result=PrimitiveResult.failed("LID_NOT_ACCESSIBLE"),
        graph=graph,
        world=world,
    )
    assert [option.candidate_id for option in options][:2] == [
        "reposition_then_retry",
        "retry_open",
    ]

    inserted = options[0].node
    assert inserted is not None
    assert inserted.action == "reposition_container"
    assert inserted.parameters["container_id"] == "box_07"
    # No observation was volunteered, so the policy default applies.
    assert inserted.parameters["yaw_change_deg"] == 90


def test_an_observed_yaw_recommendation_overrides_the_default(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    """The executing system knows better than the policy default."""
    result = PrimitiveResult.failed(
        "LID_NOT_ACCESSIBLE", observed_effects={"recommended_yaw_change_deg": 35}
    )
    options = recovery.build_options(
        node=graph.node("n4"), result=result, graph=graph, world=world
    )
    assert options[0].node.parameters["yaw_change_deg"] == 35


def test_an_inserted_node_gets_an_unused_id(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    options = recovery.build_options(
        node=graph.node("n4"),
        result=PrimitiveResult.failed("LID_NOT_ACCESSIBLE"),
        graph=graph,
        world=world,
    )
    inserted_ids = [o.node.id for o in options if o.node is not None]
    assert inserted_ids
    assert not set(inserted_ids) & set(graph.node_ids)
    assert len(set(inserted_ids)) == len(inserted_ids)


# -- README section 15, scenario 2: the shelf is occupied ------------------
def test_an_occupied_shelf_offers_a_permitted_alternate(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    world.locations["shelf_B13"] = replace(
        world.locations["shelf_B12"], location_id="shelf_B13", free_space=True
    )
    options = recovery.build_options(
        node=graph.node("n6"),
        result=PrimitiveResult.failed("TARGET_OCCUPIED"),
        graph=graph,
        world=world,
    )
    alternate = next(o for o in options if o.candidate_id == "alternate_shelf")
    assert alternate.parameter == "shelf_id"
    assert alternate.value == "shelf_B13"


def test_an_alternate_shelf_that_is_also_full_is_not_offered(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    """Offering a target that fails a moment later is worse than not offering it."""
    world.locations["shelf_B13"] = replace(
        world.locations["shelf_B12"], location_id="shelf_B13", free_space=False
    )
    options = recovery.build_options(
        node=graph.node("n6"),
        result=PrimitiveResult.failed("TARGET_OCCUPIED"),
        graph=graph,
        world=world,
    )
    assert "alternate_shelf" not in [option.candidate_id for option in options]
    assert [option.strategy for option in options] == ["escalate"]


def test_an_unknown_alternate_shelf_is_not_offered(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    assert "shelf_B13" not in world.locations
    options = recovery.build_options(
        node=graph.node("n6"),
        result=PrimitiveResult.failed("TARGET_OCCUPIED"),
        graph=graph,
        world=world,
    )
    assert "alternate_shelf" not in [option.candidate_id for option in options]


# -- README section 15, scenario 3: the robot faults holding the payload ---
def test_reassignment_is_withheld_while_the_robot_holds_the_payload(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    """Sending the work elsewhere would strand the object mid-transfer."""
    world.assert_all(["held_by(drill_01, g1_brainco_01)"])
    history = [_failure("n6", "ROBOT_FAULT")]
    options = recovery.build_options(
        node=graph.node("n6"),
        result=PrimitiveResult.failed("ROBOT_FAULT"),
        graph=graph,
        world=world,
        history=history,
    )
    assert [option.candidate_id for option in options] == ["escalate_fault"]


def test_reassignment_returns_once_the_payload_is_released(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    history = [_failure("n6", "ROBOT_FAULT")]
    options = recovery.build_options(
        node=graph.node("n6"),
        result=PrimitiveResult.failed("ROBOT_FAULT"),
        graph=graph,
        world=world,
        history=history,
    )
    assert "reassign_faulted" in [option.candidate_id for option in options]


def test_reassignment_excludes_the_robot_that_just_failed(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    history = [_failure("n6", "ROBOT_FAULT", robot_id="g1_brainco_01")]
    options = recovery.build_options(
        node=graph.node("n6"),
        result=PrimitiveResult.failed("ROBOT_FAULT"),
        graph=graph,
        world=world,
        history=history,
    )
    reassign = next(o for o in options if o.strategy == "reassign")
    assert "g1_brainco_01" in reassign.excluded_robot_ids


# -- README section 15, scenario 4: teleoperation fallback -----------------
def test_teleoperation_is_withheld_unless_the_task_permits_it(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    options = recovery.build_options(
        node=graph.node("n5"),
        result=PrimitiveResult.failed("OBJECT_POSE_UNCERTAIN"),
        graph=graph,
        world=world,
    )
    assert "teleoperate_pick" not in [option.candidate_id for option in options]


def test_teleoperation_appears_when_the_task_permits_it(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    options = recovery.build_options(
        node=graph.node("n5"),
        result=PrimitiveResult.failed("OBJECT_POSE_UNCERTAIN"),
        graph=graph,
        world=world,
        constraints={"allow_teleoperation": True},
    )
    assert "teleoperate_pick" in [option.candidate_id for option in options]


# -- attempt history -------------------------------------------------------
def test_an_option_that_already_failed_is_not_offered_again(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    """Repeating a spent option burns a physical attempt for nothing."""
    history = [_failure("n4", "LID_NOT_ACCESSIBLE"), _applied("n4", "reposition_then_retry")]
    options = recovery.build_options(
        node=graph.node("n4"),
        result=PrimitiveResult.failed("LID_NOT_ACCESSIBLE"),
        graph=graph,
        world=world,
        history=history,
    )
    assert "reposition_then_retry" not in [option.candidate_id for option in options]
    assert options[0].candidate_id == "retry_open"


def test_an_unmapped_error_code_yields_no_options(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    options = recovery.build_options(
        node=graph.node("n4"),
        result=PrimitiveResult.failed("UNKNOWN_FAILURE"),
        graph=graph,
        world=world,
    )
    assert options == []


# -- the LLM is a ranker, not a generator ---------------------------------
async def test_a_single_option_is_taken_without_consulting_the_model(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel, mock_provider
) -> None:
    decision = await recovery.decide(
        node=graph.node("n6"),
        spec=None,
        result=PrimitiveResult.failed("PAYLOAD_LIMIT_EXCEEDED"),
        graph=graph,
        world=world,
        attempt=1,
    )
    assert decision is not None
    assert decision.ranked_by == "policy"
    assert mock_provider.calls == []


async def test_the_model_ranks_only_within_the_offered_set(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    decision = await recovery.decide(
        node=graph.node("n4"),
        spec=None,
        result=PrimitiveResult.failed("LID_NOT_ACCESSIBLE"),
        graph=graph,
        world=world,
        attempt=1,
    )
    assert decision is not None
    assert decision.ranked_by == "llm"
    assert decision.candidate_id in decision.considered


async def test_a_hallucinated_candidate_falls_back_to_policy_order(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel, mock_provider
) -> None:
    """The guardrail doing its job, rather than an error."""
    mock_provider.scripted_payloads = [
        {
            "selected_candidate_id": "cut_the_box_open_with_a_saw",
            "confidence": 0.99,
            "reason": "faster",
        }
    ]
    decision = await recovery.decide(
        node=graph.node("n4"),
        spec=None,
        result=PrimitiveResult.failed("LID_NOT_ACCESSIBLE"),
        graph=graph,
        world=world,
        attempt=1,
    )
    assert decision is not None
    assert decision.candidate_id == "reposition_then_retry"
    assert decision.ranked_by == "policy"
    assert "unknown candidate" in decision.reason


async def test_an_unreachable_model_never_blocks_recovery(
    policies: RecoveryPolicies, library, llm_settings, schemas, graph: TaskGraph, world: WorldModel
) -> None:
    class DeadProvider:
        name = "dead"

        async def complete_json(self, request):
            from brash_orchestrator_core.llm.provider import LlmError

            raise LlmError("connection refused")

        async def close(self) -> None:
            return None

    planner = RecoveryPlanner(
        policies=policies,
        library=library,
        provider=DeadProvider(),
        settings=llm_settings,
        schemas=schemas,
    )
    decision = await planner.decide(
        node=graph.node("n4"),
        spec=None,
        result=PrimitiveResult.failed("LID_NOT_ACCESSIBLE"),
        graph=graph,
        world=world,
        attempt=1,
    )
    assert decision is not None
    assert decision.ranked_by == "policy"


async def test_ranking_can_be_switched_off_entirely(
    policies: RecoveryPolicies, library, mock_provider, llm_settings, schemas,
    graph: TaskGraph, world: WorldModel,
) -> None:
    offline = copy.deepcopy(policies)
    offline.use_llm_ranking = False
    planner = RecoveryPlanner(
        policies=offline,
        library=library,
        provider=mock_provider,
        settings=llm_settings,
        schemas=schemas,
    )
    decision = await planner.decide(
        node=graph.node("n4"),
        spec=None,
        result=PrimitiveResult.failed("LID_NOT_ACCESSIBLE"),
        graph=graph,
        world=world,
        attempt=1,
    )
    assert decision is not None
    assert decision.ranked_by == "policy"
    assert mock_provider.calls == []


async def test_no_permitted_option_means_no_decision(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    decision = await recovery.decide(
        node=graph.node("n4"),
        spec=None,
        result=PrimitiveResult.failed("UNKNOWN_FAILURE"),
        graph=graph,
        world=world,
        attempt=1,
    )
    assert decision is None


async def test_the_ranking_prompt_lists_the_candidates_and_the_world_state(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel, mock_provider
) -> None:
    await recovery.decide(
        node=graph.node("n4"),
        spec=None,
        result=PrimitiveResult.failed("LID_NOT_ACCESSIBLE"),
        graph=graph,
        world=world,
        attempt=1,
    )
    prompt = mock_provider.calls[0].user
    for heading in ("## FAILED NODE", "## OBSERVATIONS", "## CANDIDATES", "## WORLD STATE"):
        assert heading in prompt
    assert "reposition_then_retry" in prompt
    assert "Choose exactly one candidate_id" in prompt


async def test_the_decision_is_serializable_for_the_event_log(
    recovery: RecoveryPlanner, graph: TaskGraph, world: WorldModel
) -> None:
    decision = await recovery.decide(
        node=graph.node("n4"),
        spec=None,
        result=PrimitiveResult.failed("LID_NOT_ACCESSIBLE"),
        graph=graph,
        world=world,
        attempt=1,
    )
    payload = decision.to_dict()
    assert payload["candidate_id"]
    assert payload["strategy"] in STRATEGIES
    assert payload["inserted_node"]["action"] == "reposition_container"
