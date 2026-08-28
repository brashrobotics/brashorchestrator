# Integration notes: the manipulation gap

**Status: proposed. `brashfleetcontrol` has not been modified.**

This document records exactly what Fleet Control would need to adopt for the
orchestrator to run manipulation work on a real fleet, so that the decision can
be made later with the details already written down rather than rediscovered.

Nothing here is speculative about the orchestrator side: it already emits this
contract, and `tools/fleet_stub/` already implements it, which is how the demo
and the whole simulation suite run today.

## The gap

Fleet Control's work orders are transport-centric. A `CreateWorkOrderRequest` is
a pickup, a drop-off, a payload, and a capability list —
[`fleet-contracts.ts:276`](../../brashfleetcontrol/packages/contracts/src/fleet-contracts.ts).
That covers "carry box_07 from the maintenance area to the handover station"
perfectly, and there is no way to express "open box_07", "pick the drill out of
it", or "put the drill on shelf B12".

Those are exactly the steps that make the flagship scenario more than a delivery
task. So the orchestrator emits them against a proposed additive extension,
`brash.manipulation-work-order/v1`.

## Why additive works

Two properties of the existing implementation make this cheap:

- `requiredCapabilities` is an unconstrained `string[]`, validated only as
  non-empty in
  [`mission.ts`](../../brashfleetcontrol/packages/domain/src/mission/mission.ts).
  Manipulation capability names need no type change.
- `parseCreateWorkOrderRequest` is a hand-written parser that reads the fields it
  knows and ignores the rest, so unknown fields do not throw
  ([`validation.ts:164`](../../brashfleetcontrol/packages/contracts/src/validation.ts)).

That second property is a trap as much as a convenience, and it is the reason for
the startup probe described below: on an unextended fleet, a manipulation work
order is **accepted and silently executed as a plain transport**. A rejection
would be far safer than that. Note also that the published OpenAPI schema *does*
set `additionalProperties: false` for `CreateWorkOrderRequest`
([`fleet-api.v1.yaml:1183`](../../brashfleetcontrol/packages/contracts/openapi/fleet-api.v1.yaml)),
so the runtime parser and the documented contract currently disagree about extra
fields. Any adoption should settle that disagreement in one direction.

## Required changes

### 1. Advertise the extension on `/api/v1/health`

The single most important change, because it is what lets a client detect the
other five. Today the health payload is `{ok, service, siteId, robots, missions}`
([`fleet-api.ts:43`](../../brashfleetcontrol/apps/fleet-server/src/api/fleet-api.ts)).
Add an array:

```jsonc
{
  "ok": true,
  "service": "brash-fleet-core",
  "siteId": "plant-a",
  "extensions": ["brash.manipulation-work-order/v1"]
}
```

The orchestrator probes this once at startup. If the extension is absent it
reports `BACKEND_UNSUPPORTED` for every manipulation and perception node and logs
one explicit warning, rather than submitting work that would quietly run as a
transport. Transport nodes continue to work normally against an unextended fleet.

### 2. Accept the `manipulation` block on a work order

```jsonc
{
  "name": "open_container for t-drill/n4",
  "externalReference": "brash-task:t-drill:n4:1",
  "priority": 60,
  "requiredCapabilities": ["inspection"],
  "payloadKg": 0,
  "pickup":  { "stationId": "HANDOVER-02", "mapId": "demo-plant@1", "x": 36, "y": 14.5 },
  "dropoff": { "stationId": "HANDOVER-02", "mapId": "demo-plant@1", "x": 36, "y": 14.5 },

  // --- added by brash.manipulation-work-order/v1 ---
  "contract": "brash.manipulation-work-order/v1",
  "kind": "MANIPULATION",
  "preferredRobotId": "g1_brainco_01",
  "manipulation": {
    "primitive": "open_container",
    "readOnly": false,
    "timeoutSeconds": 45,
    "verification": "state(box_07, open)",
    "containerId": "box_07",
    "objectId": "drill_01",
    "graspRegion": "handle",
    "parameters": { "yaw_change_deg": 90 }
  }
}
```

Field notes:

- `kind` defaults to `"TRANSPORT"`, keeping every existing caller valid.
- `readOnly` marks a perception primitive that observes and changes nothing. It
  should be schedulable concurrently with other work and should never require a
  safety interlock meant for motion.
- `verification` is the ground predicate the executing system is asked to
  confirm. Sending it makes verification the executor's explicit job rather than
  something the orchestrator infers from a bare success. An executor that cannot
  confirm it should report success *without* it, and the orchestrator will record
  the effect as claimed rather than verified.
- `pickup` and `dropoff` are both set to the manipulation station so the body
  stays valid against the existing required fields.

### 3. Honour `preferredRobotId` on work orders

`preferredRobotId` is already defined on `CreateMissionRequest`
([`fleet-contracts.ts:137`](../../brashfleetcontrol/packages/contracts/src/fleet-contracts.ts))
and already weighted by the dispatch policy, which gives a matching robot a
−100,000 score bonus
([`weighted-dispatch-policy.ts:93`](../../brashfleetcontrol/packages/application/src/policies/weighted-dispatch-policy.ts)).
It is not on `CreateWorkOrderRequest`: `parseCreateWorkOrderRequest` validates it
through the mission parser and then drops it from the returned object, so today
it is accepted and ignored.

The change is to carry it through to the mission the work order creates. Note
that `fleet-core.ts` already does the reverse — it sets
`preferredRobotId: selectedRobotId` on missions derived from a work order
([`fleet-core.ts:2189`](../../brashfleetcontrol/packages/application/src/fleet-core.ts))
— so the plumbing exists; only the inbound hop is missing.

This stays a **preference**. The orchestrator's scheduler computes which body is
best suited to the action, and Fleet Control's dispatch keeps the final say,
which is the layering its own orchestrator module documents. When dispatch
overrides the preference, the orchestrator records both and reports the
substitution rather than hiding it.

### 4. Report `observedEffects` and a structured `errorCode`

The work-order view needs two additions:

```jsonc
{
  "workOrderId": "wo-6b984618745d",
  "status": "FAILED",
  "failureReason": "The lid is not accessible from this pose",
  "errorCode": "LID_NOT_ACCESSIBLE",
  "observedEffects": {
    "verified_predicates": ["state(box_07, open)"],
    "confidence": 0.95,
    "evidence": "wrist camera, three views",
    "recommended_yaw_change_deg": 35
  }
}
```

`observedEffects` is what closes the loop. Without it the orchestrator can only
record that a robot claimed success, and the whole verification invariant
degrades to trust.

`errorCode` matters nearly as much. The orchestrator maps free-text
`failureReason` prose onto its closed error set today, and that mapping is
guesswork by construction — `map_failure_reason` in
`backends/fleet_control.py` exists only because the fleet reports prose. A
structured code makes recovery deterministic. The codes are listed in
`schemas/execution_event.schema.json`.

`recommended_yaw_change_deg` is an example of the wider point: a failing robot
usually knows something useful about why it failed, and passing that through
turns a blind retry into an informed one. The recovery planner uses exactly this
value to decide how far to rotate the box.

### 5. Extend the canonical action vocabulary

[`canonical-capabilities.ts`](../../brashfleetcontrol/packages/robot-adapter-sdk/src/capabilities/canonical-capabilities.ts)
lists eight canonical actions, all transport, inspection, or docking. Adding the
manipulation primitives lets adapter conformance testing and capability
validation cover them:

```text
open_container   close_container   pick_tool_from_container   place_object_on_shelf
reposition_container   verify_object_pose   detect_tool_in_container
locate_object   inspect_target_shelf   request_teleoperation
```

### 6. Adapters must implement the primitives

The `RobotAdapter` SDK gains manipulation entry points, and the conformance
suite gains cases for them. This is the largest piece of work and the one that
genuinely belongs to Fleet Control: it is where vendor differences live, and the
reason the orchestrator delegates execution rather than driving robots itself.

## Effort, roughly

| Change | Where | Size |
| --- | --- | --- |
| 1. health extensions | `apps/fleet-server/src/api/fleet-api.ts` | trivial |
| 2. `manipulation` block | `contracts/`, `domain/`, `application/` | moderate |
| 3. `preferredRobotId` passthrough | `contracts/src/validation.ts`, `application/src/fleet-core.ts` | small |
| 4. `observedEffects` + `errorCode` | `contracts/`, `persistence/`, adapter SDK | moderate |
| 5. canonical actions | `robot-adapter-sdk/src/capabilities/` | trivial |
| 6. adapter implementations | `robot-adapter-sdk/`, each vendor adapter | large |

Changes 1, 3, and 5 are worth doing independently of the rest: 1 makes capability
detection possible for any client, 3 fixes a field that is already validated and
then discarded, and 5 costs nothing.

## Until then

`tools/fleet_stub/` implements the full target contract — including the
manipulation extension, `observedEffects`, structured error codes, idempotent
`externalReference` handling, and injectable faults — on port 4373:

```bash
python -m tools.fleet_stub --port 4373
```

Integration and simulation tests drive the *real* HTTP client against it, so the
work-order construction, header handling, polling, idempotency, and error mapping
exercised by the tests are the same code paths a real fleet would see. The stub
can also be started with `support_manipulation=False` to reproduce an unextended
fleet and confirm the refusal behaviour, which
`tests/integration/test_fleet_backend.py` does.

## An alternative, if the extension is rejected

If Fleet Control would rather not carry manipulation semantics at all, the same
result is reachable by having the orchestrator execute manipulation through
`brashembodiedcontroller` directly while continuing to route transport through
Fleet Control. That was not chosen because it splits execution across two paths
with different failure semantics, two cancellation stories, and two places where
"who is driving this robot right now" has to be resolved. The additive extension
keeps one execution path and one authority over the fleet, which is worth more
than the code it costs.
