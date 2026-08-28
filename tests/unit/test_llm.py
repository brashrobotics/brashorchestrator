"""The LLM layer: constrained decoding, response salvage, and repair.

The provider tests exercise request shaping and parsing without a served model.
The interpreter tests use the mock provider with scripted payloads, which is how
the validator-feedback repair loop can be asserted deterministically.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from brash_orchestrator_core.llm.provider import (
    ConstrainedDecodingStyle,
    LlmError,
    LlmRequest,
    MockProvider,
    OpenAiCompatProvider,
    build_provider,
    extract_json_object,
)
from brash_orchestrator_core.llm.settings import LlmSettings
from brash_orchestrator_core.task_interpreter import TaskInterpreter
from brash_world_model.model import WorldModel

SCHEMA = {"type": "object"}


def _request(**kwargs) -> LlmRequest:
    return LlmRequest(
        system="s", user="u", schema=SCHEMA, schema_name="task_graph", **kwargs
    )


# -- constrained decoding -------------------------------------------------
@pytest.mark.parametrize(
    "style,expected_key",
    [
        (ConstrainedDecodingStyle.VLLM, "guided_json"),
        (ConstrainedDecodingStyle.NIM, "nvext"),
        (ConstrainedDecodingStyle.OLLAMA, "format"),
        (ConstrainedDecodingStyle.JSON_OBJECT, "response_format"),
    ],
)
def test_each_runtime_gets_its_own_constraint_field(style, expected_key: str) -> None:
    provider = OpenAiCompatProvider(base_url="http://x/v1", model="m", decoding=style)
    body = provider._build_body(_request(), style)
    assert expected_key in body


def test_the_vllm_dialect_carries_the_schema_itself() -> None:
    """A schema sent as prose is a suggestion; sent as guided_json it is a constraint."""
    provider = OpenAiCompatProvider(base_url="http://x/v1", model="m", decoding="vllm")
    body = provider._build_body(_request(), ConstrainedDecodingStyle.VLLM)
    assert body["guided_json"] == SCHEMA


def test_the_nim_dialect_nests_the_schema_under_nvext() -> None:
    provider = OpenAiCompatProvider(base_url="http://x/v1", model="m", decoding="nim")
    body = provider._build_body(_request(), ConstrainedDecodingStyle.NIM)
    assert body["nvext"]["guided_json"] == SCHEMA


def test_no_constraint_style_adds_no_constraint_field() -> None:
    provider = OpenAiCompatProvider(base_url="http://x/v1", model="m", decoding="none")
    body = provider._build_body(_request(), ConstrainedDecodingStyle.NONE)
    assert not {"guided_json", "nvext", "format", "response_format"} & set(body)


def test_auto_defers_style_resolution_until_the_first_call() -> None:
    provider = OpenAiCompatProvider(base_url="http://x/v1", model="m", decoding="auto")
    assert provider._resolved_decoding is None


def test_an_explicit_style_is_fixed_up_front() -> None:
    provider = OpenAiCompatProvider(base_url="http://x/v1", model="m", decoding="vllm")
    assert provider._resolved_decoding is ConstrainedDecodingStyle.VLLM


def test_the_request_body_never_streams() -> None:
    """The orchestrator needs one complete document, not tokens."""
    provider = OpenAiCompatProvider(base_url="http://x/v1", model="m", decoding="vllm")
    assert provider._build_body(_request(), ConstrainedDecodingStyle.VLLM)["stream"] is False


# -- response salvage -----------------------------------------------------
def test_a_plain_json_response_parses() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_a_reasoning_block_is_stripped() -> None:
    """Reasoning models emit think blocks even under constrained decoding."""
    text = "<think>Let me consider the shelf height...</think>\n{\"a\": 1}"
    assert extract_json_object(text) == {"a": 1}


def test_a_code_fence_is_stripped() -> None:
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_a_json_object_embedded_in_prose_is_recovered() -> None:
    assert extract_json_object('Here is the graph: {"a": 1} Hope that helps.') == {"a": 1}


def test_braces_inside_strings_do_not_break_extraction() -> None:
    text = 'noise {"rationale": "use the {handle} region", "a": 1} tail'
    assert extract_json_object(text)["a"] == 1


def test_a_response_with_no_object_raises_with_a_preview() -> None:
    with pytest.raises(LlmError, match="no JSON object"):
        extract_json_object("I am unable to help with that request.")


# -- provider construction ------------------------------------------------
def test_build_provider_rejects_an_unknown_kind() -> None:
    with pytest.raises(LlmError, match="unsupported provider"):
        build_provider({"provider": "anthropic"})


def test_an_openai_profile_requires_a_model_and_url() -> None:
    with pytest.raises(LlmError, match="base_url and model"):
        build_provider({"provider": "openai_compat", "base_url": "http://x/v1"})


def test_every_profile_points_at_a_local_endpoint(repo_root) -> None:
    """No hosted API path exists, so a plan never leaves the site network."""
    import yaml

    document = yaml.safe_load((repo_root / "config" / "llm.yaml").read_text(encoding="utf-8"))
    for name, profile in document["profiles"].items():
        base_url = profile.get("base_url")
        if base_url is None:
            assert profile["provider"] == "mock", f"{name} has no endpoint and is not the mock"
            continue
        assert base_url.startswith(("http://127.0.0.1", "http://localhost")), name


def test_an_unknown_profile_lists_the_available_ones() -> None:
    with pytest.raises(LlmError, match="Available profiles"):
        LlmSettings.load(profile="gpt5")


def test_the_mock_profile_resolves_its_examples_to_an_absolute_path() -> None:
    settings = LlmSettings.load(profile="mock")
    assert settings.is_mock
    assert Path(settings.profile["examples_path"]).is_file()


# -- the mock provider ----------------------------------------------------
async def test_the_mock_provider_replays_by_goal(
    mock_provider: MockProvider, examples: list[dict]
) -> None:
    goal = examples[0]["goal"]
    response = await mock_provider.complete_json(
        _request(context={"goal": goal, "task_id": "t1"})
    )
    assert response.payload["task_id"] == "t1"
    assert len(response.payload["nodes"]) == len(examples[0]["task_graph"]["nodes"])


async def test_the_mock_provider_matches_on_keywords_not_exact_text(
    mock_provider: MockProvider,
) -> None:
    response = await mock_provider.complete_json(
        _request(context={"goal": "put that drill onto the shelf please", "task_id": "t"})
    )
    assert response.payload["goal_predicate"] == "verified_at(drill_01, shelf_B12)"


async def test_the_mock_provider_refuses_an_unmatched_goal(
    mock_provider: MockProvider,
) -> None:
    with pytest.raises(LlmError, match="no example matching"):
        await mock_provider.complete_json(
            _request(context={"goal": "reticulate the splines", "task_id": "t"})
        )


async def test_recovery_ranking_only_returns_an_offered_candidate(
    mock_provider: MockProvider,
) -> None:
    response = await mock_provider.complete_json(
        LlmRequest(
            system="s",
            user="u",
            schema=SCHEMA,
            schema_name="recovery_proposal",
            context={"candidate_ids": ["a", "b"]},
        )
    )
    assert response.payload["selected_candidate_id"] in {"a", "b"}


# -- the interpreter's repair loop ----------------------------------------
def _interpreter(provider, llm_settings, library, registry, validator, schemas):
    return TaskInterpreter(
        provider=provider,
        settings=llm_settings,
        library=library,
        registry=registry,
        validator=validator,
        schemas=schemas,
    )


async def test_a_valid_graph_needs_no_repair(
    mock_provider, llm_settings, library, registry, validator, schemas, world
) -> None:
    interpreter = _interpreter(
        mock_provider, llm_settings, library, registry, validator, schemas
    )
    result = await interpreter.interpret(
        task_id="t1", goal="Move the tool box to the handover station.", world=world
    )
    assert result.ok
    assert result.valid_first_try
    assert len(result.attempts) == 1


async def test_the_task_id_is_the_operators_not_the_models(
    mock_provider, llm_settings, library, registry, validator, schemas, world
) -> None:
    interpreter = _interpreter(
        mock_provider, llm_settings, library, registry, validator, schemas
    )
    result = await interpreter.interpret(
        task_id="operator-chosen", goal="Move the tool box to the handover station.", world=world
    )
    assert result.graph is not None
    assert result.graph.task_id == "operator-chosen"


async def test_validation_errors_are_fed_back_and_the_second_attempt_succeeds(
    mock_provider, llm_settings, library, registry, validator, schemas, world, examples
) -> None:
    """The repair loop is the difference between a demo and something usable."""
    good = next(e for e in examples if e["example_id"] == "move_box")["task_graph"]
    broken = copy.deepcopy(good)
    broken["nodes"][0]["parameters"]["object_id"] = "toolbox_99"
    mock_provider.scripted_payloads = [broken, copy.deepcopy(good)]

    interpreter = _interpreter(
        mock_provider, llm_settings, library, registry, validator, schemas
    )
    result = await interpreter.interpret(
        task_id="t1", goal="Move the tool box to the handover station.", world=world
    )

    assert result.ok
    assert not result.valid_first_try
    assert len(result.attempts) == 2
    assert any(
        error["code"] == "UNKNOWN_OBJECT" for error in result.attempts[0].errors
    )
    # The second prompt has to actually contain the errors, or the model is just
    # being asked to guess again.
    assert "UNKNOWN_OBJECT" in mock_provider.calls[1].user
    assert "VALIDATION ERRORS" in mock_provider.calls[1].user


async def test_repair_attempts_are_bounded(
    mock_provider, llm_settings, library, registry, validator, schemas, world, examples
) -> None:
    broken = copy.deepcopy(
        next(e for e in examples if e["example_id"] == "move_box")["task_graph"]
    )
    broken["nodes"][0]["parameters"]["object_id"] = "toolbox_99"
    mock_provider.scripted_payloads = [copy.deepcopy(broken) for _ in range(10)]

    interpreter = _interpreter(
        mock_provider, llm_settings, library, registry, validator, schemas
    )
    result = await interpreter.interpret(
        task_id="t1", goal="Move the tool box to the handover station.", world=world
    )

    assert not result.ok
    assert len(result.attempts) == llm_settings.max_repair_attempts + 1
    assert result.report.errors


async def test_a_schema_violation_is_reported_before_semantic_checks(
    mock_provider, llm_settings, library, registry, validator, schemas, world
) -> None:
    mock_provider.scripted_payloads = [
        {"task_id": "t1", "goal_predicate": "on(drill_01, shelf_B12)", "nodes": []}
        for _ in range(llm_settings.max_repair_attempts + 1)
    ]
    interpreter = _interpreter(
        mock_provider, llm_settings, library, registry, validator, schemas
    )
    result = await interpreter.interpret(
        task_id="t1", goal="Move the tool box to the handover station.", world=world
    )

    assert not result.ok
    assert any(error["code"] == "SCHEMA_VIOLATION" for error in result.attempts[0].errors)


async def test_the_prompt_carries_the_seven_context_sections(
    mock_provider, llm_settings, library, registry, validator, schemas, world
) -> None:
    interpreter = _interpreter(
        mock_provider, llm_settings, library, registry, validator, schemas
    )
    await interpreter.interpret(
        task_id="t1", goal="Move the tool box to the handover station.", world=world
    )
    prompt = mock_provider.calls[0].user
    for heading in (
        "## 1. TASK",
        "## 2. WORLD STATE",
        "## 3. AVAILABLE ACTIONS",
        "## 4. CAPABILITY VOCABULARY",
        "## 5. CONSTRAINTS",
        "## 6. ALLOWED GOAL PREDICATES",
        "## 7. OUTPUT SCHEMA",
    ):
        assert heading in prompt


async def test_the_prompt_offers_no_action_outside_the_library(
    mock_provider, llm_settings, library, registry, validator, schemas, world
) -> None:
    interpreter = _interpreter(
        mock_provider, llm_settings, library, registry, validator, schemas
    )
    await interpreter.interpret(
        task_id="t1", goal="Move the tool box to the handover station.", world=world
    )
    actions_section = mock_provider.calls[0].user.split("## 3. AVAILABLE ACTIONS")[1]
    actions_section = actions_section.split("## 4.")[0]
    offered = {entry["action"] for entry in json.loads(actions_section.strip())}
    assert offered == set(library.names)
