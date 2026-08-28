**# BrashOrchestrator**

**\*\*Capability-aware task planning, allocation, and execution platform for heterogeneous robot teams\*\***

\> **\*\*Status:\*\*** This README defines the reference architecture proposed for implementation within BrashOrchestrator and the first flagship demo scenario. The package, topic, service, and action names below are the proposed interfaces for the project; if different names are used in the existing codebase, they should be mapped to the same responsibilities.

**## 1. Purpose**

BrashOrchestrator takes complex physical tasks specified in natural language and:

1\. decomposes them into verifiable subtasks,

2\. determines the physical capabilities required for each subtask,

3\. assigns tasks to suitable robots,

4\. monitors robot execution in closed loop,

5\. replans in case of failure,

6\. verifies with sensor data that the task has actually been completed.

The first reference system uses two different embodiments:

\- **\*\*Unitree B2-W + Z1 manipulator:\*\*** finding, grasping, transporting the box, and placing it at the handover point,

\- **\*\*Unitree G1 + BrainCo hand:\*\*** opening the box, detecting, grasping, extracting the drill, and placing it on the target shelf.

The actual product of BrashOrchestrator is not the robots themselves. The product is a **\*\*vendor-independent orchestration layer\*\*** that unifies the mobility, manipulation, perception, and transport capabilities of different robots under a common task model.

\---

**## 2. Flagship use case**

**### User goal**

\`\`\`text

Take the drill inside the closed box in the maintenance area and place it on shelf B12 on the second floor.

\`\`\`

**### Why are two robots required?**

B2-W can safely transport the box over a route involving long distances, uneven terrain, ramps, or stairs. However, it may not have the dexterous manipulation capability required to open the box, grasp the irregularly shaped drill inside the box by its handle, and place it on a shelf designed for humans.

G1 and the BrainCo hand, on the other hand, can open the box and manipulate the drill; however, transporting the heavy box over long distances and difficult terrain may be operationally inefficient or unsafe.

BrashOrchestrator decomposes the task according to robot morphology as follows:

\`\`\`text

B2-W + Z1

  └─ find the box

  └─ grasp the box

  └─ transport the box to the handover station

  └─ place the box so that the lid is accessible

G1 + BrainCo

  └─ inspect the target shelf

  └─ go to the handover station

  └─ open the box

  └─ find the drill

  └─ grasp the drill by its handle

  └─ extract the drill from the box

  └─ place the drill on shelf B12

  └─ verify the placement

\`\`\`

In the first version, it is recommended to use a fixed **\*\*handover station\*\*** instead of direct robot-to-robot handover. Direct physical handover can be added as a separate capability in a later version.

\---

**## 3. Core design principles**

**### 3.1 The LLM does not generate motor commands**

The LLM's role is not to generate velocity, joint position, or torque. The LLM only interprets the user's goal semantically and proposes a typed task graph.

Actual robot commands pass through the following validated layers:

\`\`\`text

Natural-language goal

        ↓

LLM Task Interpreter

        ↓

Typed Task Graph

        ↓

Schema + Preconditions Validator

        ↓

Capability-aware Scheduler

        ↓

Robot Adapter

        ↓

Robot-specific autonomy/manipulation stack

\`\`\`

**### 3.2 Capability is separate from the robot name**

Instead of directly saying “B2-W should do this task,” the Planner first defines the required capability:

\`\`\`text

transport\_container

open\_container

pick\_tool\_from\_container

place\_object\_on\_shelf

verify\_object\_pose

\`\`\`

The Scheduler calculates which robot can perform these capabilities with what success probability, duration, energy, and risk.

**### 3.3 Every action has preconditions and effects**

Example:

\`\`\`yaml

action: open\_container

preconditions:

  - robot\_at\_container

  - container\_reachable

  - container\_state == closed

  - lid\_accessible == true

effects:

  - container\_state = open

\`\`\`

An action is not executed simply because the robot says “I have this capability.” The world state and safety conditions are also validated.

**### 3.4 Task completion is physically verified**

Neither the LLM nor the robot adapter alone can decide `task completed`. The final goal is verified through perception or an independent sensor result:

\`\`\`text

on(drill\_01, shelf\_B12) == true

\`\`\`

\---

**## 4. Proposed system architecture**

\`\`\`mermaid

flowchart TD

    UI[Operator UI / REST / ROS 2 Action] --> TI[Task Interpreter]

    TI --> TG[Typed Task Graph]

    TG --> VAL[Task Validator]

    CR[Capability Registry] --> SCH[Capability-aware Scheduler]

    WM[World Model] --> VAL

    WM --> SCH

    VAL --> SCH

    SCH --> EX[Execution Manager]

    EX --> B2A[B2-W + Z1 Adapter]

    EX --> G1A[G1 + BrainCo Adapter]

    B2A --> B2S[B2-W Navigation and Manipulation Stack]

    G1A --> G1S[G1 Navigation and Manipulation Stack]

    B2A --> EVT[Execution Events]

    G1A --> EVT

    EVT --> MON[Execution Monitor]

    MON --> WM

    MON --> REC[Recovery Planner]

    REC --> SCH

    SAFE[Independent Safety Manager] --> EX

    SAFE --> B2A

    SAFE --> G1A

\`\`\`

**### Main components**

\| Component | Responsibility |

\|---|---|

\| `task\_interpreter` | Converts the natural-language goal into a normalized goal and task graph draft. |

\| `task\_validator` | Performs action schema, precondition, object type, safety, and dependency checks. |

\| `capability\_registry` | Stores the current capabilities and limits of robots and payloads. |

\| `world\_model` | Stores robot, object, location, container, shelf, and task state. |

\| `scheduler` | Assigns tasks to robots based on success probability, duration, energy, risk, and suitability. |

\| `execution\_manager` | Executes the DAG/Behavior Tree; manages parallelism, timeout, cancellation, and synchronization. |

\| `execution\_monitor` | Tracks feedback and observed effects; compares expected and actual state. |

\| `recovery\_planner` | Produces retry, reassign, reposition, alternate target, and teleoperation fallback decisions. |

\| `robot\_adapters` | Converts vendor-specific robot APIs into the common Brash action interface. |

\| `safety\_manager` | Enforces speed, zone, human-proximity, payload, and emergency-stop rules independently of the LLM. |

\---

**## 5. Proposed repository structure**

\`\`\`text

brash\_orchestrator/

├── README.md

├── brash\_interfaces/

│   ├── action/

│   │   ├── ExecuteTask.action

│   │   └── ExecutePrimitive.action

│   ├── msg/

│   │   ├── RobotState.msg

│   │   ├── Capability.msg

│   │   ├── ExecutionEvent.msg

│   │   └── WorldStateDelta.msg

│   └── srv/

│       ├── RegisterRobot.srv

│       ├── QueryCapabilities.srv

│       └── LoadScenario.srv

├── brash\_orchestrator\_core/

│   ├── task\_interpreter/

│   ├── task\_validator/

│   ├── capability\_registry/

│   ├── scheduler/

│   ├── execution\_manager/

│   ├── execution\_monitor/

│   └── recovery\_planner/

├── brash\_world\_model/

├── brash\_safety\_manager/

├── brash\_robot\_adapters/

│   ├── b2w\_z1\_adapter/

│   └── g1\_brainco\_adapter/

├── brash\_bringup/

│   └── launch/

│       ├── orchestrator.launch.py

│       └── drill\_demo.launch.py

├── config/

│   ├── robots.yaml

│   ├── actions.yaml

│   ├── world\_demo.yaml

│   ├── scheduler.yaml

│   └── recovery\_policies.yaml

├── prompts/

│   ├── task\_interpreter\_system.md

│   └── task\_graph\_examples.jsonl

├── schemas/

│   ├── task\_request.schema.json

│   ├── task\_graph.schema.json

│   └── execution\_result.schema.json

└── tests/

    ├── unit/

    ├── integration/

    ├── simulation/

    └── hardware/

\`\`\`

\---

**## 6. Capability Registry**

Robot capabilities should be stored in a typed and measurable form, not as free text.

**### \`config/robots.yaml\`**

\`\`\`yaml

robots:

  b2w\_z1\_01:

    type: mobile\_manipulator

    vendor: unitree

    adapter: b2w\_z1\_adapter

    frame\_id: b2w\_z1\_01/base\_link

    status\_topic: /brash/robots/b2w\_z1\_01/state

    capabilities:

      navigate:

        enabled: true

        terrain:

          - flat

          - ramp

          - rough\_ground

          - stairs

      detect\_container:

        enabled: true

        supported\_types:

          - tote

          - tool\_box

      grasp\_container:

        enabled: true

        max\_mass\_kg: 12.0

        success\_prior: 0.88

      transport\_container:

        enabled: true

        max\_mass\_kg: 12.0

        success\_prior: 0.94

      place\_at\_handover:

        enabled: true

        pose\_tolerance\_m: 0.05

        yaw\_tolerance\_deg: 8.0

      open\_container:

        enabled: false

      pick\_tool\_from\_container:

        enabled: false

  g1\_brainco\_01:

    type: humanoid

    vendor: unitree\_brainco

    adapter: g1\_brainco\_adapter

    frame\_id: g1\_brainco\_01/pelvis

    status\_topic: /brash/robots/g1\_brainco\_01/state

    capabilities:

      navigate:

        enabled: true

        terrain:

          - flat

          - ramp

      open\_container:

        enabled: true

        supported\_lids:

          - hinged

          - loose\_cover

        success\_prior: 0.82

      detect\_tool\_in\_container:

        enabled: true

        supported\_objects:

          - drill

          - screwdriver

          - wrench

      pick\_tool\_from\_container:

        enabled: true

        max\_mass\_kg: 3.0

        preferred\_grasp\_regions:

          drill:

            - handle

        success\_prior: 0.78

      place\_object\_on\_shelf:

        enabled: true

        min\_height\_m: 0.45

        max\_height\_m: 1.65

        max\_mass\_kg: 3.0

        success\_prior: 0.86

      verify\_object\_pose:

        enabled: true

\`\`\`

\> **\*\*Important:\*\*** The payload, reach, tolerance, and success values above are only structural examples. Before running the hardware, they must be replaced with manufacturer limits, the current mechanical integration, and controlled test results.

`success\_prior` values may initially be engineering estimates. As the system runs, context-dependent success models should be updated from real trials.

Example:

\`\`\`text

P(pick\_drill | handle\_visible, mass<2kg, front\_reach) = 0.93

P(pick\_drill | handle\_occluded, side\_reach) = 0.41

\`\`\`

\---

**## 7. Action Library**

A capability defines what the robot can generally do; an action defines an executable, parameterized, and verifiable primitive. The YAML below shows only the main part of the action catalog required for the demo; the `locate\_object`, `inspect\_target\_shelf`, and recovery actions used in the task graph should also be defined separately using the same contract.

**### \`config/actions.yaml\`**

\`\`\`yaml

actions:

  transport\_container:

    required\_capabilities:

      - navigate

      - grasp\_container

      - transport\_container

    parameters:

      container\_id: object\_id

      destination: location\_id

    preconditions:

      - object\_exists(container\_id)

      - object\_type(container\_id, container)

      - robot\_reachable(container\_id)

      - mass\_within\_limit(container\_id)

      - route\_available(destination)

    effects:

      - at(container\_id, destination)

      - released(container\_id)

    timeout\_s: 420

  open\_container:

    required\_capabilities:

      - open\_container

    parameters:

      container\_id: object\_id

    preconditions:

      - at\_robot\_near(container\_id)

      - state(container\_id, closed)

      - lid\_accessible(container\_id)

    effects:

      - state(container\_id, open)

    timeout\_s: 120

  pick\_tool\_from\_container:

    required\_capabilities:

      - detect\_tool\_in\_container

      - pick\_tool\_from\_container

    parameters:

      object\_id: object\_id

      container\_id: object\_id

    preconditions:

      - inside(object\_id, container\_id)

      - state(container\_id, open)

      - grasp\_region\_visible(object\_id)

    effects:

      - held\_by(object\_id, assigned\_robot)

      - not\_inside(object\_id, container\_id)

    timeout\_s: 120

  place\_object\_on\_shelf:

    required\_capabilities:

      - place\_object\_on\_shelf

    parameters:

      object\_id: object\_id

      shelf\_id: location\_id

    preconditions:

      - held\_by(object\_id, assigned\_robot)

      - shelf\_has\_free\_space(shelf\_id)

      - shelf\_height\_supported(shelf\_id)

    effects:

      - on(object\_id, shelf\_id)

      - not\_held(object\_id)

    timeout\_s: 120

  verify\_object\_pose:

    required\_capabilities:

      - verify\_object\_pose

    parameters:

      object\_id: object\_id

      expected\_location: location\_id

    preconditions:

      - sensor\_view\_available(expected\_location)

    effects:

      - verified\_at(object\_id, expected\_location)

    timeout\_s: 30

\`\`\`

\---

**## 8. Initial world model**

**### \`config/world\_demo.yaml\`**

\`\`\`yaml

locations:

  maintenance\_area:

    floor: 0

  handover\_station\_02:

    floor: 1

    type: robot\_handover\_fixture

  shelf\_B12:

    floor: 1

    type: shelf

    height\_m: 1.20

    free\_space: true

objects:

  box\_07:

    type: container

    subtype: tool\_box

    location: maintenance\_area

    state: closed

    mass\_kg: 8.5

    lid\_accessible: unknown

    contents:

      - drill\_01

  drill\_01:

    type: power\_tool

    subtype: drill

    location: box\_07

    relation: inside

    mass\_kg: 1.9

    preferred\_grasp\_region: handle

    pose\_known: false

robots:

  b2w\_z1\_01:

    location: charging\_area\_b2w

    availability: available

    battery\_percent: 91

  g1\_brainco\_01:

    location: humanoid\_station

    availability: available

    battery\_percent: 84

\`\`\`

The World Model should be updated after every execution event. For example, when B2-W places the box, the adapter result alone should not be trusted; perception should verify that the box is actually at the handover station.

\---

**## 9. Task request**

**### Proposed ROS 2 action interface**

\`brash\_interfaces/action/ExecuteTask.action\`:

\`\`\`text

\# Goal

string task\_id

string natural\_language\_goal

string constraints\_json

\---

\# Result

bool success

string final\_state\_json

string failure\_code

string summary

\---

\# Feedback

string execution\_state

string active\_node\_id

string assigned\_robot\_id

float32 progress

string event\_json

\`\`\`

**### Sending the task**

\`\`\`bash

ros2 action send\_goal \\

  /brash/execute\_task \\

  brash\_interfaces/action/ExecuteTask \\

  "{task\_id: 'store\_drill\_001', natural\_language\_goal: 'Take the drill inside the closed box in the maintenance area and place it on shelf B12 on the second floor.', constraints\_json: '{\\"max\_risk\\":\\"medium\\",\\"deadline\_s\\":900}'}" \\

  --feedback

\`\`\`

Alternatively, the UI or REST gateway can send the same request as follows:

\`\`\`json

{

  "task\_id": "store\_drill\_001",

  "goal": "Take the drill inside the closed box in the maintenance area and place it on shelf B12 on the second floor.",

  "constraints": {

    "deadline\_s": 900,

    "max\_risk": "medium",

    "allow\_teleoperation": true,

    "preferred\_handover\_location": "handover\_station\_02"

  }

}

\`\`\`

\---

**## 10. LLM Task Interpreter output**

The LLM should produce only a typed task graph that conforms to the JSON Schema. A robot assignment may be proposed, but the final decision belongs to the scheduler.

\`\`\`json

{

  "task\_id": "store\_drill\_001",

  "goal\_predicate": "verified\_at(drill\_01, shelf\_B12)",

  "nodes": [

    {

      "id": "n1",

      "action": "locate\_object",

      "parameters": {"object\_id": "box\_07"},

      "required\_capabilities": ["detect\_container"],

      "depends\_on": []

    },

    {

      "id": "n2",

      "action": "transport\_container",

      "parameters": {

        "container\_id": "box\_07",

        "destination": "handover\_station\_02"

      },

      "required\_capabilities": [

        "navigate",

        "grasp\_container",

        "transport\_container",

        "place\_at\_handover"

      ],

      "depends\_on": ["n1"]

    },

    {

      "id": "n3",

      "action": "inspect\_target\_shelf",

      "parameters": {"shelf\_id": "shelf\_B12"},

      "required\_capabilities": ["verify\_object\_pose"],

      "depends\_on": []

    },

    {

      "id": "n4",

      "action": "open\_container",

      "parameters": {"container\_id": "box\_07"},

      "required\_capabilities": ["open\_container"],

      "depends\_on": ["n2"]

    },

    {

      "id": "n5",

      "action": "pick\_tool\_from\_container",

      "parameters": {

        "object\_id": "drill\_01",

        "container\_id": "box\_07"

      },

      "required\_capabilities": [

        "detect\_tool\_in\_container",

        "pick\_tool\_from\_container"

      ],

      "depends\_on": ["n4"]

    },

    {

      "id": "n6",

      "action": "place\_object\_on\_shelf",

      "parameters": {

        "object\_id": "drill\_01",

        "shelf\_id": "shelf\_B12"

      },

      "required\_capabilities": ["place\_object\_on\_shelf"],

      "depends\_on": ["n3", "n5"]

    },

    {

      "id": "n7",

      "action": "verify\_object\_pose",

      "parameters": {

        "object\_id": "drill\_01",

        "expected\_location": "shelf\_B12"

      },

      "required\_capabilities": ["verify\_object\_pose"],

      "depends\_on": ["n6"]

    }

  ]

}

\`\`\`

`n3` can run in parallel while `n2` is running. `n6` cannot start until both extraction from the box is complete and shelf suitability has been verified.

\---

**## 11. Validation**

At minimum, the following checks should be performed before the task graph is executed:

1\. Is every action defined in the Action Library?

2\. Are the parameter types compatible with the JSON Schema?

3\. Is there at least one suitable robot for every required capability?

4\. Is the dependency graph acyclic?

5\. Can the preconditions be satisfied in the current world state?

6\. Is the target predicate measurable and verifiable?

7\. Are the user constraints compatible with the safety policies?

8\. Has the Planner generated a robot, object, or location that does not actually exist?

9\. Are an action timeout and recovery policy defined?

10\. Is the required human approval available for a hazardous action?

If the LLM output does not pass the validator, it is not executed directly. A structured error is returned to the Interpreter, and only a limited number of correction attempts are made.

\---

**## 12. Robot assignment and scheduling**

For each task node, the Scheduler retrieves suitable robot candidates from the Capability Registry.

Proposed cost function:

\`\`\`text

J(robot, action) =

    w\_time   × estimated\_duration

  + w\_energy × estimated\_energy

  + w\_risk   × execution\_risk

  + w\_move   × reposition\_cost

  - w\_succ   × predicted\_success\_probability

\`\`\`

The robot with the lowest valid cost is selected. For some actions, a single robot may not be sufficient; in that case, composite assignment can be used.

Example initial assignment:

\| Task node | Assigned robot | Rationale |

\|---|---|---|

\| `n1 locate\_object` | `b2w\_z1\_01` | Access to the area where the box is located and container perception are available. |

\| `n2 transport\_container` | `b2w\_z1\_01` | Transport over difficult terrain and suitable payload capacity. |

\| `n3 inspect\_target\_shelf` | `g1\_brainco\_01` | Close to the target shelf and has perception capability. |

\| `n4 open\_container` | `g1\_brainco\_01` | Dexterous manipulation is required. |

\| `n5 pick\_tool\_from\_container` | `g1\_brainco\_01` | Grasping the drill by its handle is required. |

\| `n6 place\_object\_on\_shelf` | `g1\_brainco\_01` | Human-scale shelf manipulation is required. |

\| `n7 verify\_object\_pose` | `g1\_brainco\_01` or fixed camera | Independent visual verification is preferred. |

The Scheduler's output should be a deterministic execution plan, not LLM text.

\---

**## 13. Robot adapter contract**

Every robot should expose the same interface independently of the vendor API.

\`\`\`python

from abc import ABC, abstractmethod

from dataclasses import dataclass

from typing import Any



@dataclass

class PrimitiveRequest:

    action\_id: str

    parameters: dict[str, Any]

    timeout\_s: float



@dataclass

class PrimitiveResult:

    success: bool

    error\_code: str

    observed\_effects: dict[str, Any]

    metrics: dict[str, float]



class RobotAdapter(ABC):

    @abstractmethod

    async def get\_state(self) -> dict[str, Any]:

        """Robot pose, battery, fault, payload and availability."""

    @abstractmethod

    async def get\_capabilities(self) -> dict[str, Any]:

        """Current capabilities, limits and confidence values."""

    @abstractmethod

    async def check\_preconditions(

        self, request: PrimitiveRequest

    ) -> tuple[bool, str]:

        """Checks robot-local feasibility before execution."""

    @abstractmethod

    async def execute(self, request: PrimitiveRequest) -> PrimitiveResult:

        """Executes a validated primitive through the robot stack."""

    @abstractmethod

    async def cancel(self, action\_id: str) -> None:

        """Requests a safe cancellation."""

    @abstractmethod

    async def emergency\_stop(self) -> None:

        """Requests the vendor-provided safe stop path immediately."""

\`\`\`

**### B2-W + Z1 adapter responsibilities**

\- sending navigation goals and collecting navigation results,

\- converting the box detection result into the common object format,

\- executing the Z1 grasp plan,

\- checking the payload limit and box stability,

\- precise placement onto the handover fixture,

\- standardizing path blocked, grasp failed, and unstable payload errors.

**### G1 + BrainCo adapter responsibilities**

\- managing transitions between G1 locomotion and manipulation modes,

\- calling grasp primitives for the BrainCo hand,

\- normalizing perception results for the box lid and latch,

\- producing the drill handle pose,

\- publishing grasp, lift, extract, and shelf placement results in the common event format,

\- safe cancellation in contact, slip, and unexpected-force situations.

The robot adapter should not make high-level decisions. The adapter should only execute the given primitive safely and report the result honestly.

\---

**## 14. Execution event model**

All adapters should use the same error codes:

\`\`\`text

SUCCESS

PRECONDITION\_FAILED

OBJECT\_NOT\_FOUND

OBJECT\_POSE\_UNCERTAIN

GRASP\_FAILED

SLIP\_DETECTED

PATH\_BLOCKED

TARGET\_OCCUPIED

LID\_NOT\_ACCESSIBLE

PAYLOAD\_LIMIT\_EXCEEDED

HUMAN\_IN\_SAFETY\_ZONE

COMMUNICATION\_LOST

TIMEOUT

ROBOT\_FAULT

UNKNOWN\_FAILURE

\`\`\`

Example event:

\`\`\`json

{

  "task\_id": "store\_drill\_001",

  "node\_id": "n4",

  "robot\_id": "g1\_brainco\_01",

  "event": "ACTION\_FAILED",

  "error\_code": "LID\_NOT\_ACCESSIBLE",

  "observations": {

    "container\_id": "box\_07",

    "recommended\_yaw\_change\_deg": 90,

    "confidence": 0.87

  }

}

\`\`\`

\---

**## 15. Failure recovery**

Failure recovery is one of the main values of this project. The system should not work only in the ideal demo flow.

**### Example 1: The box lid is not facing G1**

\`\`\`text

G1: open\_container → LID\_NOT\_ACCESSIBLE

\`\`\`

Recovery Planner:

1\. Updates the World Model.

2\. Adds the `reposition\_container` action.

3\. Assigns B2-W + Z1 the task of rotating the box by 90 degrees.

4\. The new pose is physically verified.

5\. G1 retries the `open\_container` action.

**### Example 2: The drill handle is not visible**

\`\`\`text

G1: pick\_tool\_from\_container → OBJECT\_POSE\_UNCERTAIN

\`\`\`

Recovery options, in order:

1\. a different camera viewpoint,

2\. slightly repositioning the box,

3\. a clutter removal primitive,

4\. an alternative grasp region,

5\. a teleoperation request,

6\. transport to the exception station.

**### Example 3: Shelf B12 is full**

\`\`\`text

G1: place\_object\_on\_shelf → TARGET\_OCCUPIED

\`\`\`

The system:

1\. queries the inventory policy,

2\. selects a new target if an allowed alternative shelf exists,

3\. asks the operator for a decision if alternatives are prohibited,

4\. does not mark the task as completed without verification.

**### Example 4: B2-W fails**

When multiple B2-W robots are added to the system, the same transport task should be assigned to another suitable robot. If the transported box is still in the first robot's gripper, the physical state and safe transfer conditions must be verified before automatic reassignment.

\---

**## 16. LLM integration**

**### LLM responsibilities**

\- normalizing the user goal,

\- matching ambiguous objects and locations with the world model,

\- generating a task graph draft,

\- semantically interpreting the action failure description,

\- generating suggestions for the Recovery Planner from allowed actions.

**### What the LLM must not do**

\- generate robot motor commands directly,

\- invent undefined actions,

\- change a safety rule,

\- claim the task is complete without verification,

\- override robot capability limits,

\- assume an object location without a perception result.

**### Minimum context to provide to the Interpreter prompt**

\`\`\`text

1\. User goal

2\. Current world state summary

3\. Available action schemas

4\. Robot-independent capability names

5\. Task constraints

6\. Allowed goal predicates

7\. Output JSON Schema

\`\`\`

Instead of placing the raw telemetry of all robots into the prompt, a short, typed, and up-to-date summary produced by the World Model should be used.

\---

**## 17. Model training and execution-grounded data**

In the first version, it is recommended to use an existing model and schema-constrained generation instead of training an LLM from scratch. As real execution data accumulates, fine-tuning can be performed for task planning or robot selection.

The following record should be kept for every trial:

\`\`\`json

{

  "instruction": "...",

  "initial\_world\_state": {},

  "available\_robots": {},

  "generated\_task\_graph": {},

  "validated\_task\_graph": {},

  "assignments": {},

  "execution\_events": [],

  "recovery\_actions": [],

  "final\_world\_state": {},

  "success": true,

  "human\_interventions": 0,

  "duration\_s": 382.4

}

\`\`\`

This data can be used for three separate models:

1\. **\*\*Task graph generation:\*\*** Generate a valid action graph from the language goal.

2\. **\*\*Success prediction:\*\*** Predict the success probability of a specific robot-action-context combination.

3\. **\*\*Recovery ranking:\*\*** Rank which recovery action is more likely to succeed in a failure context.

For robot assignment, a success model learned from real execution data is more valuable than relying only on the manufacturer datasheet or a static capability list.

\---

**## 18. Execution flow**

The commands below assume the proposed ROS 2 package structure.

**### 18.1 Build the workspace**

\`\`\`bash

cd \~/brash\_ws

colcon build --symlink-install

source install/setup.bash

\`\`\`

**### 18.2 Start the Orchestrator and common services**

\`\`\`bash

ros2 launch brash\_bringup orchestrator.launch.py \\

  robots\_config:=config/robots.yaml \\

  actions\_config:=config/actions.yaml \\

  scheduler\_config:=config/scheduler.yaml

\`\`\`

**### 18.3 Start the robot adapters**

\`\`\`bash

ros2 launch b2w\_z1\_adapter adapter.launch.py robot\_id:=b2w\_z1\_01

\`\`\`

\`\`\`bash

ros2 launch g1\_brainco\_adapter adapter.launch.py robot\_id:=g1\_brainco\_01

\`\`\`

**### 18.4 Load the demo world model**

\`\`\`bash

ros2 service call \\

  /brash/world\_model/load\_scenario \\

  brash\_interfaces/srv/LoadScenario \\

  "{scenario\_path: 'config/world\_demo.yaml'}"

\`\`\`

**### 18.5 Run a dry run**

In dry-run mode, no commands are sent to the robots. The plan, assignment, precondition, and safety checks are displayed.

\`\`\`bash

ros2 param set /brash/execution\_manager dry\_run true

\`\`\`

Then the task is sent:

\`\`\`bash

ros2 action send\_goal \\

  /brash/execute\_task \\

  brash\_interfaces/action/ExecuteTask \\

  "{task\_id: 'store\_drill\_001', natural\_language\_goal: 'Take the drill inside the closed box in the maintenance area and place it on shelf B12 on the second floor.', constraints\_json: '{}'}" \\

  --feedback

\`\`\`

**### 18.6 Hardware execution**

After the dry-run output is approved and the robot safety checks have passed:

\`\`\`bash

ros2 param set /brash/execution\_manager dry\_run false

\`\`\`

During hardware execution, a separate safety supervisor and physical emergency stop must be active.

\---

**## 19. MVP development sequence**

**### Phase 0 — Offline task graph**

\- Natural-language goal → JSON task graph,

\- JSON Schema validation,

\- static robot capability matching,

\- no robot commands are sent.

**### Phase 1 — B2-W transport primitive**

\- \`locate\_container\`,

\- \`grasp\_container\`,

\- \`navigate\_with\_payload\`,

\- \`place\_at\_handover\`,

\- observed effects verification.

**### Phase 2 — G1 manipulation primitives**

\- \`open\_container\`,

\- \`detect\_tool\_in\_container\`,

\- \`grasp\_drill\_handle\`,

\- \`extract\_object\`,

\- \`place\_object\_on\_shelf\`,

\- \`verify\_object\_pose\`.

**### Phase 3 — End-to-end demo with a fixed task graph**

First, the two-robot scenario should be completed using a manually defined and deterministic task graph without using an LLM. This stage validates the adapter, world model, synchronization, and safety infrastructure.

**### Phase 4 — LLM Task Interpreter**

\- generating a graph from the user goal,

\- schema-constrained output,

\- limited graph repair using validator feedback,

\- keeping robot assignment in the scheduler.

**### Phase 5 — Closed-loop recovery**

\- inaccessible lid,

\- invisible drill handle,

\- full shelf,

\- blocked path,

\- grasp failure,

\- timeout scenarios.

**### Phase 6 — Multi-B2-W fleet**

\- dynamic robot availability,

\- cost-based assignment among robots with the same capability,

\- task reassignment when a robot fails,

\- shared traffic and charging management.

**### Phase 7 — New robot and payload integration**

A new robot, gripper, or sensor should be added to the system only by adding an adapter and capability manifest. There should be no vendor-specific condition in the Core planner.

\---

**## 20. Test strategy**

**### Unit test**

\- schema validation,

\- capability matching,

\- DAG cycle detection,

\- precondition evaluation,

\- cost calculation,

\- error-code mapping.

**### Integration test**

\- fake B2-W and fake G1 adapters,

\- event ordering,

\- timeout and cancellation,

\- world state reconciliation,

\- recovery plan insertion.

**### Simulation test**

\- random box position,

\- variable drill orientation,

\- blocked route,

\- full target shelf,

\- delayed robot feedback,

\- robot availability changes.

**### Hardware test**

Each primitive is tested independently first. End-to-end testing is performed only after the following conditions are met:

\- emergency stop verified,

\- payload limits verified,

\- robot workspaces marked,

\- handover fixture secured,

\- human safety zone active,

\- perception confidence threshold defined,

\- teleoperation fallback tested.

\---

**## 21. KPIs to measure**

\| KPI | Description |

\|---|---|

\| End-to-end success rate | Proportion of tasks in which the drill is physically verified on the correct shelf. |

\| Autonomous completion rate | Proportion of tasks completed without human intervention. |

\| Task graph validity | Proportion of graphs that pass the validator on the first LLM output. |

\| Wrong assignment rate | Proportion of incorrect robot assignments that fail because of capability or context. |

\| Recovery success rate | Proportion of tasks that recover autonomously after the first action failure. |

\| Mean intervention count | Number of human interventions per task. |

\| Mean task duration | Time from command to verified final state. |

\| New robot integration time | Time for a new robot adapter to reach its first working capability. |

\| New task engineering time | Amount of manual robot-specific code and time required for a new task. |

\| World-state consistency | Agreement between the expected state and the sensor-observed state. |

Example targets for the first controlled demo:

\`\`\`text

End-to-end success rate           ≥ 85%

Verified completion               = 100% of reported successes

Autonomous recovery               ≥ 70% of injected recoverable failures

Human intervention                < 0.3 per task

Invalid robot command             = 0

Safety-policy violations          = 0

\`\`\`

\---

**## 22. Safety boundaries**

BrashOrchestrator does not replace a safety-certified low-level controller. Each robot should retain its own manufacturer safety mechanisms.

Minimum rules:

\- Motion is stopped or the robot transitions to a safe mode when the human safety zone is violated.

\- Execution is not forced in cases of an unknown object, low perception confidence, or uncertain grasp.

\- Payload and center-of-mass limits are revalidated at the adapter level.

\- The LLM cannot change safety limits.

\- In case of communication loss, the robot transitions to the defined safe state.

\- The emergency stop operates through a physical path independent of the orchestration software.

\- Teleoperation is enabled only for an authorized operator and a recorded session.

\- A task success report is produced only after the goal predicate is verified.

\---

**## 23. Demo acceptance criteria**

The first BrashOrchestrator demo is considered successful when all of the following conditions are met:

\- [ ] The user submits the task in natural language.

\- [ ] The system converts the task into a typed task graph.

\- [ ] The Validator verifies all actions and dependencies.

\- [ ] B2-W + Z1 autonomously finds the box and transports it to the handover station.

\- [ ] G1 inspects the target shelf in parallel while B2-W is transporting the box.

\- [ ] The box is physically verified at the handover point.

\- [ ] G1 opens the box.

\- [ ] G1 detects the drill and produces an appropriate grasp pose.

\- [ ] G1 extracts the drill and places it on shelf B12.

\- [ ] The final state is verified through perception.

\- [ ] The World Model and inventory are updated.

\- [ ] The system performs autonomous recovery in at least one controlled failure scenario.

\- [ ] No safety violation or unverified success report occurs.

\---

**## 24. BrashOrchestrator's technical claim**

This demo should not be positioned as “the LLM assigns tasks to two robots.” The technical claim is:

\> BrashOrchestrator converts natural-language goals into verifiable physical task graphs; distributes tasks among heterogeneous robots using their embodiment, capability, risk, and execution history; updates the world model with real robot feedback; and performs closed-loop replanning in case of failures.

The drill scenario is the first easy-to-understand proof of this claim. The same infrastructure can later be extended to maintenance equipment transport, industrial inspection, part feeding, emergency logistics, and multi-robot field operations.

\---

**## 25. Next goals**

1\. Move from a fixed handover fixture to direct robot-to-robot handover.

2\. Make capability success estimates context-aware using real execution data.

3\. Add dynamic fleet allocation with multiple B2-W robots.

4\. Integrate a third robot from a different manufacturer without changing the core code.

5\. Enable plug-and-play introduction of new grippers and sensors through manifests.

6\. Run fast feasibility tests of task graphs in a digital twin.

7\. Apply risk-aware scheduling under uncertainty.

8\. Display the task graph, robot assignment, live state, and recovery decisions explainably in the operator interface.

\---

**## License**

If the license has not yet been determined, this section can be left as follows until a project decision is made:

\`\`\`text

Copyright © Brash Robotics. All rights reserved.

\`\`\`
