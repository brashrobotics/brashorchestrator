# Web operator console

A browser front end for the same `Orchestrator` facade the CLI and the ROS 2
node use. It exists so a person can submit a goal, watch the plan light up node
by node, read exactly what the model produced and why the validator accepted or
rejected it, and see the world model change underneath — without reading a log.

```bash
python -m tools.fleet_stub --port 4373 &

python -m brash_orchestrator_core.cli \
  --llm-profile mock --fleet-url http://127.0.0.1:4373 \
  serve --port 8088
```

Then open <http://127.0.0.1:8088>. The global flags (`--scenario`,
`--twin-bundle`, `--llm-profile`, `--fleet-url`, `--fleet-site`,
`--fleet-token`, `--no-trials`) come *before* `serve`; `--host`, `--port`, and
`--approve` come after it. `python -m brash_orchestrator_core.web` is a shorter
entry point for the common case.

## What it is built from

No Node toolchain, no npm, no build step, and no new runtime dependency. The
server is aiohttp, which the Fleet Control backend already requires, and the
client is three plain files in `web/static/` served straight off disk. The
console therefore ships inside the Python package and works from a `colcon`
install with nothing else present, which is the same reason `tools/fleet_stub`
is written against the standard library.

```text
web/
  app.py              routes, WebSocket, error mapping
  hub.py              task registry, single-writer rule, live fan-out
  static/index.html   structure
  static/styles.css   hand-written, dark, responsive to one column below 1180px
  static/app.js       one state object plus render functions, no framework
```

## The four things it shows

**The plan, as it happens.** The middle column draws the task graph as execution
waves, so nodes with no ordering relationship sit side by side and visibly run at
the same time. Cards colour by state and pick up their assigned robot, attempt
count, and error code live. When recovery inserts a node the graph grows a card
for it mid-run.

**What the model actually did.** The LLM tab lists every interpretation attempt
with its model, decoding style, latency, the raw JSON it emitted, and the exact
validator errors that rejected it. When the repair loop runs you can read the
first attempt, the machine-readable complaints that went back as the next prompt,
and the graph that satisfied them. `valid first try` is shown because it is a
tracked KPI, not decoration.

**Why that robot.** The assignments panel expands each node into the full
candidate table: cost, predicted success, estimated duration, risk, and for the
ineligible robots the specific reason they were filtered out. "The fleet sent a
different robot than I expected" is answerable without reopening the run.

**What is true now.** The world tab separates observed facts from claimed ones
with the same distinction the world model enforces internally, and shows robots,
payloads, batteries, and object placements after every task.

## Modes

| Mode | Interprets | Schedules | Submits work orders |
| --- | --- | --- | --- |
| Plan | yes | yes | no |
| Dry run | yes | yes | no, but walks execution and safety |
| Execute | yes | yes | yes |

Planning narrates itself through the same event vocabulary as execution, so the
feed reads identically in all three modes rather than having a separate format
for the case where nothing moves.

## One task at a time

A second submission while a task is running is refused with `409
TASK_ALREADY_RUNNING`. The orchestrator owns one mutable world model and talks to
one fleet, so two concurrent tasks asserting facts about the same drill would
leave a world state that describes neither of them. The refusal is the honest
answer; queueing them silently would not be.

## Developer surface

The Dev tab appears only when the execution backend exposes a fault-injection
control surface, which in practice means the backend is `tools/fleet_stub`
rather than a production fleet. The probe asks for the capability instead of
sniffing a server banner: it posts a fault declaration containing no faults, and
the stub rejects it with `400` by design while a real Fleet Control has no such
route and answers `404`.

From there, injecting `LID_NOT_ACCESSIBLE` once on `open_container` and
re-running the drill goal reproduces the first README section 15 scenario live —
the node fails, the recovery planner proposes `reposition_then_retry`, a
`reposition_container` node is inserted before it, and the retry succeeds. It is
the fastest way to see recovery actually work.

"Reload scenario and clear faults" reloads the scenario from disk rather than
attempting an undo, because the world model records observed facts and the only
honest way to rewind is to go back to the operator's declared ground truth.

## HTTP surface

| Route | Purpose |
| --- | --- |
| `GET /` | the console |
| `GET /api/health` | LLM profile, backend health, control surface, running task |
| `GET /api/library` | actions, robots, error codes, example goals |
| `GET /api/world` | world model snapshot |
| `GET /api/tasks` | recent tasks, newest first |
| `POST /api/tasks` | submit a goal |
| `GET /api/tasks/{id}` | one task with events and full result |
| `POST /api/tasks/{id}/cancel` | cancel the running task |
| `POST /api/approvals` | grant or revoke a safety approval token |
| `POST /api/faults` | declare a fault on the backend control surface |
| `POST /api/reset` | reload the scenario and clear faults |
| `GET /api/stream` | WebSocket: snapshot, then live |

Errors use the same problem shape as the fleet contract:
`{"error": {"code": ..., "message": ...}}`. An unrecognised constraint is a
`400`, not a silent drop — an operator who asks for a deadline and does not get
one must be told, because the alternative is a robot moving under limits nobody
agreed to.

## The live stream

A client receives a full snapshot and then every subsequent message on the same
connection, in that order. Fetching state over a separate request would let a
client observe an event about a task it has not been told exists. The server
subscribes before it builds the snapshot for the same reason: anything that
happens while the snapshot is being assembled is queued rather than lost.

The graph and the assignment map travel on the `GRAPH_VALIDATED` and
`PLAN_ASSIGNED` events themselves, in their `observations` field, so a live
observer draws the plan from the one narrative every other component reads
instead of from a side channel that could disagree with it.

A browser that stops reading is disconnected from the truth, not allowed to slow
it down: its backlog is bounded, the oldest messages are dropped, and it is told
how many it missed. A tab left open on a closed laptop must never be able to
stall a physical task.

## Security

There is no authentication. The console binds to `127.0.0.1` by default and
prints a warning if asked to bind anywhere else, because this surface can
dispatch work orders to physical robots. Putting it on a network requires an
authenticating reverse proxy in front of it, and that is a deployment decision
this package deliberately does not make for you.

## Tests

`tests/integration/test_web.py` drives the real application through aiohttp's
test client: routing, JSON shapes, refusals, the single-writer rule,
cancellation, fault injection, recovery, and the WebSocket from snapshot to
`task_finished`. The LLM is the mock provider and the fleet is the stub, so the
whole file is hermetic and runs in the normal `pytest` pass.
