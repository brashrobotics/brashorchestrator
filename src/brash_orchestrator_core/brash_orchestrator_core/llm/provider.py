"""Local LLM providers with schema-constrained decoding.

Every provider here targets a locally served, OpenAI-compatible endpoint. There
is no hosted-API path, so a plan is never sent off site.

Constrained decoding is the important part. A task graph is executable
structure, not prose, so the model is forced to emit a document that satisfies
the JSON Schema rather than being asked politely and parsed hopefully. Runtimes
expose that differently:

===========  ============================================================
``vllm``     ``extra_body`` ``{"guided_json": schema}``
``nim``      ``extra_body`` ``{"nvext": {"guided_json": schema}}``
``ollama``   ``{"format": schema}``
``json_object`` ``response_format`` only, the weakest fallback
===========  ============================================================

``auto`` probes those in descending strength once per process and caches the
first style the endpoint accepts.
"""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

_THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_CODE_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class ConstrainedDecodingStyle(str, Enum):
    AUTO = "auto"
    VLLM = "vllm"
    NIM = "nim"
    OLLAMA = "ollama"
    JSON_OBJECT = "json_object"
    NONE = "none"


#: Probe order for AUTO, strongest guarantee first.
_AUTO_ORDER = (
    ConstrainedDecodingStyle.VLLM,
    ConstrainedDecodingStyle.NIM,
    ConstrainedDecodingStyle.OLLAMA,
    ConstrainedDecodingStyle.JSON_OBJECT,
    ConstrainedDecodingStyle.NONE,
)


class LlmError(RuntimeError):
    """Raised when the model is unreachable or returns unusable output."""


@dataclass(frozen=True, slots=True)
class LlmRequest:
    system: str
    user: str
    schema: Mapping[str, Any]
    schema_name: str
    temperature: float = 0.0
    max_tokens: int = 2048
    top_p: float = 1.0
    #: Carried through for the mock provider and for trial records.
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LlmResponse:
    payload: dict[str, Any]
    text: str
    model: str
    decoding: str
    latency_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "decoding": self.decoding,
            "latency_s": round(self.latency_s, 3),
            "payload": self.payload,
        }


class LlmProvider(ABC):
    """A source of schema-conforming JSON documents."""

    name: str = "provider"

    @abstractmethod
    async def complete_json(self, request: LlmRequest) -> LlmResponse:
        """Return a JSON document satisfying ``request.schema``."""

    async def close(self) -> None:
        """Release any transport resources."""


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull the JSON document out of a model response.

    Constrained decoding usually makes this trivial, but reasoning models still
    wrap output in think blocks or fences even when guided, and a weak fallback
    style guarantees nothing at all. Scanning for a balanced object is more
    robust than trusting the whole response to parse.
    """
    cleaned = _THINK_BLOCK.sub("", text).strip()
    fenced = _CODE_FENCE.search(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(cleaned)):
            character = cleaned[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = cleaned.find("{", start + 1)

    preview = cleaned[:400]
    raise LlmError(f"model response contained no JSON object; first 400 chars: {preview!r}")


class OpenAiCompatProvider(LlmProvider):
    """Client for a locally served OpenAI-compatible chat completions endpoint."""

    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        decoding: ConstrainedDecodingStyle | str = ConstrainedDecodingStyle.AUTO,
        api_key: str | None = None,
        timeout_s: float = 120.0,
        temperature: float = 0.0,
        top_p: float = 1.0,
        max_tokens: int = 2048,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.requested_decoding = ConstrainedDecodingStyle(decoding)
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self._resolved_decoding: ConstrainedDecodingStyle | None = (
            None if self.requested_decoding is ConstrainedDecodingStyle.AUTO
            else self.requested_decoding
        )
        self._session: Any | None = None

    # -- transport ------------------------------------------------------
    async def _ensure_session(self) -> Any:
        if self._session is None:
            try:
                import aiohttp
            except ImportError as error:  # pragma: no cover - dependency guard
                raise LlmError(
                    "aiohttp is required to reach a local LLM endpoint. "
                    "Install it, or select the 'mock' profile for offline runs."
                ) from error
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._session = aiohttp.ClientSession(
                headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout_s)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- request shaping ------------------------------------------------
    def _build_body(
        self, request: LlmRequest, style: ConstrainedDecodingStyle
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": request.temperature,
            "top_p": request.top_p,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        schema = dict(request.schema)
        if style is ConstrainedDecodingStyle.VLLM:
            body["guided_json"] = schema
            body["guided_decoding_backend"] = "outlines"
        elif style is ConstrainedDecodingStyle.NIM:
            body["nvext"] = {"guided_json": schema}
        elif style is ConstrainedDecodingStyle.OLLAMA:
            body["format"] = schema
        elif style is ConstrainedDecodingStyle.JSON_OBJECT:
            body["response_format"] = {"type": "json_object"}
        return body

    async def _post(self, body: Mapping[str, Any]) -> tuple[int, str]:
        session = await self._ensure_session()
        url = f"{self.base_url}/chat/completions"
        try:
            async with session.post(url, json=dict(body)) as response:
                return response.status, await response.text()
        except asyncio.TimeoutError as error:
            raise LlmError(f"LLM request to {url} timed out after {self.timeout_s}s") from error
        except Exception as error:  # noqa: BLE001 - aiohttp raises a wide family
            raise LlmError(f"LLM request to {url} failed: {error}") from error

    async def complete_json(self, request: LlmRequest) -> LlmResponse:
        loop = asyncio.get_running_loop()
        started = loop.time()
        styles = (
            [self._resolved_decoding]
            if self._resolved_decoding is not None
            else list(_AUTO_ORDER)
        )

        last_error = ""
        for style in styles:
            assert style is not None
            status, text = await self._post(self._build_body(request, style))
            if status == 200:
                self._resolved_decoding = style
                content = _extract_choice_content(text)
                payload = extract_json_object(content)
                return LlmResponse(
                    payload=payload,
                    text=content,
                    model=self.model,
                    decoding=style.value,
                    latency_s=loop.time() - started,
                )
            last_error = f"HTTP {status}: {text[:300]}"
            # 400 and 422 mean the endpoint rejected this constrained-decoding
            # dialect, which is exactly what AUTO is probing for. Anything else
            # is a real failure and retrying a weaker dialect would hide it.
            if status not in (400, 422) or self._resolved_decoding is not None:
                raise LlmError(f"LLM endpoint rejected the request. {last_error}")

        raise LlmError(
            "no supported constrained-decoding style was accepted by "
            f"{self.base_url}. Last response: {last_error}"
        )


def _extract_choice_content(raw: str) -> str:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LlmError(f"LLM endpoint returned non-JSON body: {raw[:300]!r}") from error
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmError(f"LLM response contained no choices: {raw[:300]!r}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # Some servers return content parts rather than a plain string.
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, Mapping)
        )
    if not isinstance(content, str) or not content.strip():
        raise LlmError("LLM response message content was empty")
    return content


class MockProvider(LlmProvider):
    """Deterministic replay used by tests and offline demos.

    A green test run must never depend on a model being up, and a dry-run demo
    on a laptop should still produce the real graph. This provider replays
    curated examples selected by goal similarity, and answers recovery requests
    with the planner's own first candidate so ranking stays deterministic.
    """

    name = "mock"

    def __init__(self, examples: Sequence[Mapping[str, Any]] | None = None) -> None:
        self.examples = list(examples or [])
        self.calls: list[LlmRequest] = []
        #: Set to force the next task-graph response, used to exercise repair.
        self.scripted_payloads: list[dict[str, Any]] = []

    @classmethod
    def from_examples_file(cls, path: str | Path) -> MockProvider:
        examples: list[Mapping[str, Any]] = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                examples.append(json.loads(line))
        return cls(examples)

    async def complete_json(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        if self.scripted_payloads:
            payload = self.scripted_payloads.pop(0)
        elif request.schema_name == "recovery_proposal":
            payload = self._recovery_payload(request)
        else:
            payload = self._task_graph_payload(request)
        return LlmResponse(
            payload=payload,
            text=json.dumps(payload),
            model="mock",
            decoding="mock",
        )

    def _task_graph_payload(self, request: LlmRequest) -> dict[str, Any]:
        goal = str(request.context.get("goal", ""))
        example = self.select_example(goal)
        if example is None:
            raise LlmError(
                f"the mock provider has no example matching {goal!r}. "
                "Add one to prompts/task_graph_examples.jsonl or point "
                "ORCH_LLM_PROFILE at a served model."
            )
        graph = json.loads(json.dumps(example["task_graph"]))
        task_id = request.context.get("task_id")
        if task_id:
            graph["task_id"] = task_id
        return graph

    def select_example(self, goal: str) -> Mapping[str, Any] | None:
        """Pick the closest curated example by keyword overlap."""
        wanted = _tokenize(goal)
        best: tuple[float, Mapping[str, Any]] | None = None
        for example in self.examples:
            if _normalize_goal(str(example.get("goal", ""))) == _normalize_goal(goal):
                return example
            keywords = {str(word).lower() for word in example.get("match_keywords", [])}
            if not keywords:
                continue
            overlap = len(keywords & wanted) / len(keywords)
            if overlap > 0 and (best is None or overlap > best[0]):
                best = (overlap, example)
        return best[1] if best else None

    def _recovery_payload(self, request: LlmRequest) -> dict[str, Any]:
        candidate_ids = [
            str(candidate_id) for candidate_id in request.context.get("candidate_ids", [])
        ]
        if not candidate_ids:
            raise LlmError("recovery ranking was requested without candidates")
        return {
            "selected_candidate_id": candidate_ids[0],
            "confidence": 0.5,
            "reason": "Deterministic mock ranking keeps the configured policy order.",
            "ranked_candidate_ids": candidate_ids,
        }


def _normalize_goal(goal: str) -> str:
    return " ".join(goal.lower().split()).strip(" .")


def _tokenize(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", text.lower()) if token}


def build_provider(settings: Mapping[str, Any]) -> LlmProvider:
    """Construct a provider from a resolved profile mapping."""
    provider_kind = str(settings.get("provider", "openai_compat"))
    if provider_kind == "mock":
        examples_path = settings.get("examples_path")
        if examples_path:
            return MockProvider.from_examples_file(examples_path)
        return MockProvider()
    if provider_kind != "openai_compat":
        raise LlmError(
            f"unsupported provider {provider_kind!r}; expected 'openai_compat' or 'mock'"
        )
    base_url = settings.get("base_url")
    model = settings.get("model")
    if not base_url or not model:
        raise LlmError("an openai_compat profile requires both base_url and model")
    return OpenAiCompatProvider(
        base_url=str(base_url),
        model=str(model),
        decoding=str(settings.get("decoding", "auto")),
        api_key=settings.get("api_key"),
        timeout_s=float(settings.get("timeout_s", 120.0)),
        temperature=float(settings.get("temperature", 0.0)),
        top_p=float(settings.get("top_p", 1.0)),
        max_tokens=int(settings.get("max_tokens", 2048)),
    )
