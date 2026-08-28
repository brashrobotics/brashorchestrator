# The language model

The model does two jobs: it turns a sentence into a candidate task graph, and it
ranks recovery options that policy has already decided are permissible. It does
nothing else, and the architecture is arranged so that it *cannot* do anything
else.

Everything runs against a locally served, OpenAI-compatible endpoint. No profile
in `config/llm.yaml` points at a hosted API; nothing about a plant's layout,
inventory, or failures leaves the site network.

## What the model does

- normalizes the operator's goal,
- resolves ambiguous objects and locations against the world-state summary,
- drafts a task graph,
- interprets a failure description semantically,
- ranks the allowed recovery options for that failure.

## What the model cannot do

Not "must not" as a prompt instruction — cannot, structurally:

| Prohibition | What prevents it |
| --- | --- |
| emit robot motor commands | there is no code path from a model response to a controller; execution goes through Fleet Control work orders built from typed requests |
| invent an action | decoding is constrained to `brash.task-graph/v1`, and the validator rejects any `action` not in the Action Library |
| invent a recovery | the planner builds the candidate list from `recovery_policies.yaml` and the model returns only a `selected_candidate_id`; an unrecognized id is discarded and the policy order stands |
| change a safety rule | the safety manager reads only `config/safety.yaml` and world state, and is evaluated after planning, immediately before submission |
| claim a task is complete | completion is decided by `ExecutionManager._finalize` from observed facts; the model is never asked |
| exceed a capability limit | payload, reach, and battery are hard eligibility filters in the scheduler |
| assume a location without perception | the validator checks preconditions against the world model, where an unobserved fact is not verified |

## Constrained decoding

A schema in the prompt is a suggestion; a schema in the sampler is a guarantee.
The client sends the task-graph schema to the endpoint in whichever form that
endpoint accepts:

| Style | Wire form | Server |
| --- | --- | --- |
| `vllm` | `extra_body: {"guided_json": <schema>}` | vLLM OpenAI server |
| `nim` | `extra_body: {"nvext": {"guided_json": <schema>}}` | NVIDIA NIM |
| `ollama` | `{"format": <schema>}` | Ollama |
| `json_object` | `response_format: {"type": "json_object"}` | weakest fallback; shape is not guaranteed |
| `auto` | probe once, cache the strongest style the endpoint accepted | any |

`auto` is the default. It tries the styles in descending strength and remembers
the first that works, so an endpoint swap needs no configuration change. If none
is accepted the client raises rather than falling back to unconstrained
generation, because an unconstrained response is not a task graph and pretending
otherwise moves the failure somewhere harder to diagnose.

Responses are still parsed defensively — a balanced-object scan rather than
`json.loads` on the whole body — since `json_object` guarantees valid JSON but not
that the model did not wrap it in prose.

## Interpreter prompt

Seven sections, in this order, assembled by `task_interpreter.py`:

1. **Task** — task id and the goal sentence.
2. **World state** — a short typed summary from the world model, never raw
   telemetry. Locations, objects with their placement and state, robots with
   availability and battery.
3. **Available actions** — the Action Library as parameters, preconditions, and
   effects. This is the model's entire vocabulary.
4. **Capability vocabulary** — robot-independent capability names, so the model
   describes what is needed rather than who should do it.
5. **Constraints** — deadline, risk ceiling, allowed robots, teleoperation
   permission.
6. **Allowed goal predicates** — the closed set the goal may be expressed in.
7. **Output schema** — `brash.task-graph/v1`, the same schema handed to the
   sampler.

Up to two few-shot examples from `prompts/task_graph_examples.jsonl` are appended.
Robot ids are deliberately absent from the prompt: assignment is the scheduler's
job, and a model that names a robot is a model whose plan silently encodes an
allocation nobody costed.

## The repair loop

The validator's errors are written to be repaired against, not just read:

```text
interpret ──▶ validate ──▶ ok? ──▶ done
                 │
                 └─▶ errors ──▶ re-prompt with the errors appended (bounded)
```

Each attempt appends a `## VALIDATION ERRORS` section naming the offending node,
the field, and what was expected. `max_repair_attempts` (default 2) bounds the
loop, so a goal the model cannot ground costs three calls and then fails
explicitly rather than looping.

Whether a graph was valid on the first attempt is recorded per trial, which is
the signal for whether a model, a prompt, or an action library needs work.

## Recovery ranking

Recovery is not generation. The planner:

1. looks up the policy for the reported error code,
2. filters candidates against world state, history, and constraints — a
   `reassign` is dropped when the failed robot still holds the payload, an
   `alternate_target` is dropped when no permitted target has free space,
3. if one candidate survives, takes it and never calls the model,
4. if several survive, asks the model to pick one, constrained to
   `brash.recovery-proposal/v1`, returning only `selected_candidate_id`,
   `confidence`, and `reason`.

Every decision records `ranked_by` (`policy` or `llm`) and the full `considered`
list, so an operator can always see what the alternatives were. If the endpoint
is unreachable, recovery still works: it falls back to policy order.

## Profiles

`config/llm.yaml`, selected with `--llm-profile` or `ORCH_LLM_PROFILE`:

| Profile | Model | Notes |
| --- | --- | --- |
| `cosmos_reason` | `nvidia/cosmos-reason1-7b` | default; a physical-AI reasoning model, the closest fit for judging embodiment, reachability, and failure semantics |
| `qwen_local` | `Qwen/Qwen3-8B` | general instruct model on vLLM |
| `nim` | `nvidia/llama-3.3-nemotron-super-49b-v1` | larger, NIM-hosted |
| `ollama` | `qwen3:8b` | lowest-friction local setup |
| `mock` | — | replays `prompts/task_graph_examples.jsonl` keyed by goal |

Overrides, applied on top of the selected profile: `ORCH_LLM_BASE_URL`,
`ORCH_LLM_MODEL`, `ORCH_LLM_API_KEY`, `ORCH_LLM_DECODING`, `ORCH_LLM_TIMEOUT_S`.

Temperature is 0.0 everywhere. Planning is not a creative task, and a
reproducible plan is worth more than a varied one.

### The mock profile

The `mock` profile exists so the test suite and offline demos never depend on a
model being up. A suite that needs a GPU served is a suite that stops being run,
and a green test run that depended on a 7B model's mood would not mean much
anyway. It replays recorded examples by matching the goal, and raises when asked
for something it has not seen — so a test cannot accidentally pass because the
mock guessed.

## Serving a model

vLLM, which supports `guided_json` directly:

```bash
python -m vllm.entrypoints.openai.api_server \
  --model nvidia/cosmos-reason1-7b \
  --port 8000 --guided-decoding-backend outlines
```

Then point the orchestrator at it:

```bash
export ORCH_LLM_PROFILE=cosmos_reason
export ORCH_LLM_BASE_URL=http://127.0.0.1:8000/v1
python -m brash_orchestrator_core.cli inspect
```

`inspect` reports the resolved profile, the provider, and the decoding style that
was accepted, which is the fastest way to confirm constrained decoding is
actually engaged rather than silently degraded.

## Watching what the model does

Every attempt is recorded with its model, decoding style, latency, raw JSON, and
the validator errors that rejected it. Three ways to read them, all the same
data:

```bash
# the whole interpretation, including every repair attempt
python -m brash_orchestrator_core.cli --json plan "..." | jq .interpretation.attempts
```

The web console's LLM tab renders the same attempts as an inspector, which is
the easiest way to watch the repair loop: the first graph, the machine-readable
complaints that went back as the next prompt, and the graph that satisfied them.
See [WEB_UI.md](WEB_UI.md). Offline, `trials/*.jsonl` carries the same record for
every run.

## Fine-tuning later

The recommendation for a first version is an existing model with constrained
decoding, not a model trained from scratch. `trial_recorder.py` writes one JSONL
record per trial — goal, world state before, the graph, the plan, every event, the
outcome, and precomputed labels — which is the dataset for later fine-tuning of
task decomposition or robot selection, and the audit trail in the meantime.

The `success_prior` values in `config/robots.yaml` are engineering estimates and
are the first thing that should be replaced by a learned, context-dependent
success model once enough trials exist.
