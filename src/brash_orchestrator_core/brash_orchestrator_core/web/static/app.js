/* BrashOrchestrator operator console.
 *
 * No framework and no build step. The whole client is a small state object plus
 * render functions driven by one WebSocket, which mirrors how the server works:
 * the execution event stream is the single narrative, and this file is one more
 * subscriber to it rather than a second source of truth.
 *
 * Everything is written into the DOM as text, never as HTML, because goals, LLM
 * output, and backend failure prose all reach this file unsanitised.
 */
"use strict";

const state = {
  health: null,
  library: null,
  world: null,
  tasks: [],
  /** The task being displayed, which is the running one unless history is clicked. */
  view: null,
  /** node id -> {node, state, error_code, robot_id, attempt} */
  nodes: new Map(),
  layers: [],
  mode: "execute",
  failuresOnly: false,
  socket: null,
  retryDelay: 500,
};

const MODE_HINTS = {
  plan: "Interpret and schedule only. No work order is submitted.",
  dry_run: "Walk the whole pipeline including safety, but submit nothing to the fleet.",
  execute: "Submit work orders and drive the execution backend.",
};

const MILESTONES = new Set([
  "TASK_ACCEPTED", "GRAPH_GENERATED", "GRAPH_VALIDATED", "PLAN_ASSIGNED", "TASK_SUCCEEDED",
]);
const RECOVERIES = new Set([
  "RECOVERY_PROPOSED", "RECOVERY_APPLIED", "RECOVERY_EXHAUSTED", "APPROVAL_REQUIRED",
]);
const NODE_STATE_FOR = {
  NODE_STARTED: "RUNNING",
  NODE_SUCCEEDED: "SUCCEEDED",
  NODE_FAILED: "FAILED",
  NODE_SKIPPED: "SKIPPED",
  NODE_CANCELLED: "CANCELLED",
};

/* ------------------------------------------------------------------ dom */
const $ = (selector) => document.querySelector(selector);

/** Build an element. Children are always text or nodes, never markup. */
function el(tag, attributes, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === "object" ? child : document.createTextNode(String(child)));
  }
  return node;
}

function replace(target, ...children) {
  target.replaceChildren(...children.flat().filter(Boolean));
}

const clock = (iso) => (iso || "").slice(11, 23);

/* ------------------------------------------------------------------ api */
async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = (payload && payload.error) || {};
    throw new Error(error.message || `${path} returned ${response.status}`);
  }
  return payload;
}

function notify(message, kind) {
  const notice = $("#notice");
  notice.textContent = message || "";
  notice.className = `notice ${kind || "info"}`;
  notice.hidden = !message;
}

/* ------------------------------------------------------------------ socket */
function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${scheme}://${location.host}/api/stream`);
  state.socket = socket;

  socket.onopen = () => {
    state.retryDelay = 500;
    setConnection("live", "live");
  };
  socket.onclose = () => {
    setConnection("lost", "reconnecting");
    setTimeout(connect, state.retryDelay);
    state.retryDelay = Math.min(state.retryDelay * 2, 8000);
  };
  socket.onerror = () => setConnection("lost", "disconnected");
  socket.onmessage = (message) => {
    try {
      handle(JSON.parse(message.data));
    } catch (error) {
      console.error("unreadable message", error);
    }
  };
}

function setConnection(className, text) {
  const box = $("#connection");
  box.className = `connection ${className}`;
  box.querySelector("[data-role=text]").textContent = text;
}

function handle(message) {
  switch (message.type) {
    case "snapshot":
      state.health = message.health;
      state.library = message.library;
      state.world = message.world;
      state.tasks = message.tasks || [];
      renderHealth();
      renderLibrary();
      renderWorld();
      renderTasks();
      if (message.current_task) selectTask(message.current_task);
      else if (!state.view && state.tasks.length) loadTask(state.tasks[0].task_id);
      else renderSummary();
      break;

    case "task_started":
      selectTask({ ...message.task, events: [], result: null });
      state.tasks.unshift(message.task);
      renderTasks();
      showTab("events");
      setRunning(true);
      break;

    case "event":
      if (state.view && message.task_id === state.view.task_id) {
        state.view.events.push(message.event);
        applyEvent(message.event);
        appendEvent(message.event);
        renderEventCount();
      }
      break;

    case "task_finished":
      state.tasks = state.tasks.map((task) =>
        task.task_id === message.task.task_id ? message.task : task);
      renderTasks();
      setRunning(false);
      if (state.view && state.view.task_id === message.task.task_id) {
        loadTask(message.task.task_id, { keepEvents: true });
      }
      break;

    case "world":
      state.world = message.world;
      renderWorld();
      break;

    case "tasks":
      state.tasks = message.tasks || [];
      state.view = null;
      state.nodes.clear();
      renderTasks();
      renderSummary();
      renderGraph();
      renderAssignments();
      renderLlm();
      replace($("#feed"));
      renderEventCount();
      break;

    case "approvals":
      if (state.health) state.health.approvals = message.approvals;
      renderApprovals();
      break;

    default:
      break;
  }
  if (message.dropped) {
    notify(`${message.dropped} update(s) were dropped while this tab was busy.`, "info");
  }
}

/* ------------------------------------------------------------------ header */
function renderHealth() {
  const health = state.health;
  if (!health) return;
  const llm = health.llm;
  const backend = health.backend;
  const chips = [
    chip("LLM", llm.mock ? "mock replay" : `${llm.model || llm.provider}`, llm.mock ? "mock" : "good"),
    chip("decoding", llm.mock ? "n/a" : llm.decoding || "auto"),
    chip("backend", backend.reachable ? backend.name : `${backend.name} unreachable`,
      backend.reachable ? "good" : "bad"),
  ];
  if (backend.extensions && backend.extensions.length) {
    chips.push(chip("extension", backend.extensions.join(", "), "good"));
  }
  chips.push(chip("scenario", (health.scenario || "").split(/[\\/]/).pop()));
  replace($("#chips"), chips);

  $("[data-tab=dev]").hidden = !health.control_surface;
  renderApprovals();
}

function chip(label, value, kind) {
  return el("span", { class: `chip ${kind || ""}`, title: `${label}: ${value}` },
    el("b", { text: label }), String(value));
}

function renderApprovals() {
  const tokens = (state.health && state.health.approvals) || [];
  replace($("#approvals"), tokens.map((token) =>
    el("button", {
      type: "button",
      title: `revoke ${token}`,
      onclick: () => setApproval(token, false),
    }, token, el("span", { text: "\u00d7" }))));
}

async function setApproval(token, granted) {
  try {
    await api("/api/approvals", { method: "POST", body: JSON.stringify({ token, granted }) });
  } catch (error) {
    notify(error.message, "error");
  }
}

/* ------------------------------------------------------------------ library */
function renderLibrary() {
  const library = state.library;
  if (!library) return;

  replace($("#examples"), (library.example_goals || []).map((goal) =>
    el("button", { type: "button", title: goal, onclick: () => { $("#goal").value = goal; } },
      goal.length > 46 ? `${goal.slice(0, 44)}\u2026` : goal)));

  replace($("#robot-picker"), (library.robots || []).map((robot) =>
    el("label", { class: "check" },
      el("input", { type: "checkbox", value: robot.robot_id, "data-role": "robot" }),
      `${robot.robot_id} (${robot.type})`)));

  replace($("#fault-primitive"), (library.primitives || []).map((primitive) =>
    el("option", { value: primitive, text: primitive })));
  replace($("#fault-code"), (library.error_codes || []).map((code) =>
    el("option", { value: code, text: code, selected: code === "LID_NOT_ACCESSIBLE" })));
}

/* ------------------------------------------------------------------ tasks */
function renderTasks() {
  replace($("#tasks"), state.tasks.map((task) => el("li", {},
    el("button", {
      type: "button",
      "data-state": task.state,
      class: state.view && state.view.task_id === task.task_id ? "selected" : "",
      onclick: () => loadTask(task.task_id),
    },
      el("span", { class: "goal", text: task.goal }),
      el("span", {
        class: "meta",
        text: `${clock(task.submitted_at)}  ${task.mode}  ${task.state}${task.failure_code ? ` ${task.failure_code}` : ""}`,
      })))));
}

async function loadTask(taskId, options) {
  try {
    const task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    selectTask(task, options);
  } catch (error) {
    notify(error.message, "error");
  }
}

/** Make one task the displayed task and redraw everything derived from it. */
function selectTask(task, options) {
  state.view = { ...task, events: task.events || [] };
  state.nodes.clear();
  state.layers = [];
  for (const event of state.view.events) applyEvent(event);

  if (!options || !options.keepEvents) renderFeed();
  renderTasks();
  renderSummary();
  renderGraph();
  renderAssignments();
  renderLlm();
  renderEventCount();
  setRunning(task.state === "RUNNING");
}

function setRunning(running) {
  $("#cancel").disabled = !running;
  $("#submit").disabled = running;
  $("#submit").textContent = running ? "Running\u2026" : labelForMode(state.mode);
}

const labelForMode = (mode) => (mode === "plan" ? "Plan" : mode === "dry_run" ? "Dry run" : "Execute");

/* ------------------------------------------------------------------ events */
function applyEvent(event) {
  if (event.event === "GRAPH_VALIDATED" && event.observations && event.observations.task_graph) {
    adoptGraph(event.observations.task_graph);
  }
  if (event.event === "PLAN_ASSIGNED" && event.observations) {
    const assignments = event.observations.assignments || {};
    for (const [nodeId, robotId] of Object.entries(assignments)) {
      const entry = state.nodes.get(nodeId);
      if (entry) entry.robot_id = robotId;
    }
    if (event.observations.layers) state.layers = event.observations.layers;
    renderGraph();
  }
  const nodeState = NODE_STATE_FOR[event.event];
  if (nodeState && event.node_id && event.node_id !== "-") {
    let entry = state.nodes.get(event.node_id);
    if (!entry) {
      // Recovery can insert a node that was not in the interpreted graph.
      entry = { node: { id: event.node_id, action: event.action || "", parameters: {}, depends_on: [] } };
      state.nodes.set(event.node_id, entry);
      state.layers = [];
    }
    entry.state = nodeState;
    entry.error_code = event.error_code;
    entry.attempt = event.attempt;
    if (event.robot_id) entry.robot_id = event.robot_id;
    entry.detail = event.detail;
    renderGraph();
  }
}

function adoptGraph(graph) {
  state.nodes.clear();
  for (const node of graph.nodes || []) {
    state.nodes.set(node.id, { node, state: "PENDING", error_code: "SUCCESS" });
  }
  state.layers = [];
  if (state.view) state.view.goal_predicate = graph.goal_predicate;
  renderGraph();
  renderSummary();
}

function eventClass(event) {
  if (event.error_code && event.error_code !== "SUCCESS") return "fail";
  if (RECOVERIES.has(event.event)) return "recovery";
  if (MILESTONES.has(event.event)) return "milestone";
  if (event.event === "NODE_SUCCEEDED" || event.event === "EFFECT_VERIFIED") return "good";
  return "";
}

function eventRow(event) {
  const isFailure = event.error_code && event.error_code !== "SUCCESS";
  return el("li", { class: eventClass(event) },
    el("span", { class: "at", text: clock(event.occurred_at) }),
    el("span", { class: "body" },
      el("span", { class: "line" },
        el("span", { class: "kind", text: event.event }),
        event.node_id && event.node_id !== "-"
          ? el("span", { class: "node-ref", text: event.node_id })
          : null,
        event.robot_id ? el("span", { class: "node-ref", text: event.robot_id }) : null,
        event.attempt > 1 ? el("span", { class: "at", text: `try ${event.attempt}` }) : null,
        isFailure ? el("span", { class: "code", text: event.error_code }) : null),
      event.detail ? el("span", { class: "detail", text: event.detail }) : null));
}

function renderFeed() {
  const events = ((state.view && state.view.events) || [])
    .filter((event) => !state.failuresOnly || (event.error_code && event.error_code !== "SUCCESS"));
  replace($("#feed"), events.map(eventRow));
  scrollFeed();
}

function appendEvent(event) {
  if (state.failuresOnly && (!event.error_code || event.error_code === "SUCCESS")) return;
  $("#feed").append(eventRow(event));
  scrollFeed();
}

function scrollFeed() {
  const feed = $("#feed");
  feed.scrollTop = feed.scrollHeight;
}

function renderEventCount() {
  const events = (state.view && state.view.events) || [];
  const failures = events.filter((event) => event.error_code && event.error_code !== "SUCCESS").length;
  $("#event-count").textContent =
    `${events.length} event${events.length === 1 ? "" : "s"}${failures ? `, ${failures} failed` : ""}`;
}

/* ------------------------------------------------------------------ summary */
function renderSummary() {
  const view = state.view;
  const box = $("#summary");
  if (!view) {
    box.className = "card summary empty";
    replace(box, el("span", { text: "Submit a goal to begin." }));
    return;
  }
  box.className = "card summary";
  const outcome = (view.result && view.result.outcome) || null;
  const facts = [];
  facts.push(chip("mode", view.mode));
  if (outcome) {
    facts.push(chip("goal", outcome.goal_predicate));
    facts.push(chip("verified", outcome.goal_verified ? "by observation" : "no",
      outcome.goal_verified ? "good" : "bad"));
    facts.push(chip("duration", `${outcome.duration_s}s`));
    if (outcome.recovery_actions && outcome.recovery_actions.length) {
      facts.push(chip("recoveries", outcome.recovery_actions.length));
    }
    if (outcome.human_interventions) {
      facts.push(chip("escalations", outcome.human_interventions, "bad"));
    }
  } else if (view.goal_predicate) {
    facts.push(chip("goal", view.goal_predicate));
  }

  replace(box,
    el("div", { class: "summary-head" },
      el("span", { class: `verdict ${view.state}`, text: verdictText(view) }),
      el("span", { class: "task-id", text: view.task_id })),
    el("div", { class: "goal-text", text: view.goal }),
    el("div", { class: "facts" }, facts),
    view.summary ? el("div", { class: "prose", text: view.summary }) : null);
}

function verdictText(view) {
  if (view.state === "RUNNING") return "RUNNING";
  if (view.state === "SUCCEEDED") return view.mode === "plan" ? "PLANNED" : "SUCCEEDED";
  return view.failure_code ? `${view.state} \u00b7 ${view.failure_code}` : view.state;
}

/* ------------------------------------------------------------------ graph */
function renderGraph() {
  const box = $("#graph");
  if (!state.nodes.size) {
    replace(box, el("p", {
      class: "placeholder",
      text: state.view && state.view.state === "RUNNING"
        ? "Waiting for the interpreter to produce a valid task graph\u2026"
        : "No task graph yet.",
    }));
    $("#graph-note").textContent = "";
    return;
  }

  const layers = state.layers.length ? state.layers : deriveLayers();
  const counts = { SUCCEEDED: 0, FAILED: 0 };
  for (const entry of state.nodes.values()) {
    if (entry.state in counts) counts[entry.state] += 1;
  }
  $("#graph-note").textContent =
    `${state.nodes.size} nodes, ${layers.length} wave(s), ${counts.SUCCEEDED} done` +
    (counts.FAILED ? `, ${counts.FAILED} failed` : "");

  replace(box, layers.map((layer, index) => el("div", { class: "wave" },
    el("div", {
      class: "wave-label",
      text: `wave ${index + 1}${layer.length > 1 ? "\u2225" : ""}`,
      title: layer.length > 1 ? "these nodes run concurrently" : "",
    }),
    el("div", { class: "wave-nodes" }, layer.map(nodeCard)))));
}

function nodeCard(nodeId) {
  const entry = state.nodes.get(nodeId);
  const node = entry.node;
  const parameters = Object.entries(node.parameters || {});
  return el("div", { class: "node", "data-state": entry.state || "PENDING" },
    el("div", { class: "node-head" },
      el("span", { class: "id", text: node.id }),
      el("span", { class: "action", text: node.action })),
    parameters.length
      ? el("div", { class: "params" }, parameters.map(([key, value]) =>
        el("span", { class: "param", text: `${key}=${value}` })))
      : null,
    el("div", { class: "node-foot" },
      el("span", { text: entry.state || "PENDING" }),
      entry.robot_id ? el("span", { class: "robot", text: entry.robot_id }) : null,
      entry.attempt > 1 ? el("span", { text: `try ${entry.attempt}` }) : null,
      entry.error_code && entry.error_code !== "SUCCESS"
        ? el("span", { class: "err", text: entry.error_code })
        : null,
      (node.depends_on || []).length
        ? el("span", { text: `after ${node.depends_on.join(", ")}` })
        : null));
}

/** Fall back to computing waves locally when no plan has arrived yet. */
function deriveLayers() {
  const pending = new Map();
  for (const [nodeId, entry] of state.nodes) {
    pending.set(nodeId, new Set((entry.node.depends_on || []).filter((id) => state.nodes.has(id))));
  }
  const layers = [];
  while (pending.size) {
    const ready = [...pending.keys()].filter((nodeId) => pending.get(nodeId).size === 0);
    if (!ready.length) {
      layers.push([...pending.keys()]);
      break;
    }
    layers.push(ready);
    for (const nodeId of ready) pending.delete(nodeId);
    for (const remaining of pending.values()) {
      for (const nodeId of ready) remaining.delete(nodeId);
    }
  }
  return layers;
}

/* ------------------------------------------------------------------ plan */
function renderAssignments() {
  const plan = state.view && state.view.result && state.view.result.plan;
  const box = $("#assignments");
  if (!plan) {
    replace(box, el("p", {
      class: "placeholder",
      text: "Assignment rationale appears when the scheduler has finished.",
    }));
    return;
  }
  replace(box, Object.entries(plan.assignments).map(([nodeId, assignment]) =>
    el("details", {},
      el("summary", {},
        el("span", { class: "node-ref", text: nodeId }),
        el("strong", { text: assignment.assigned_robot_id || "NO ELIGIBLE ROBOT" }),
        el("span", { class: "why", text: assignment.action })),
      el("div", {},
        el("p", { class: "hint", text: assignment.rationale }),
        candidateTable(assignment.candidates || [])))));
}

function candidateTable(candidates) {
  if (!candidates.length) return null;
  return el("table", {},
    el("thead", {}, el("tr", {},
      ["robot", "cost", "success", "duration", "risk", "why not"].map((label) =>
        el("th", { text: label })))),
    el("tbody", {}, candidates.map((candidate) => el("tr",
      { class: candidate.eligible ? "" : "ineligible" },
      el("td", { class: "mono", text: candidate.robot_id }),
      el("td", { text: candidate.cost === null ? "\u2014" : candidate.cost.toFixed(3) }),
      el("td", { text: candidate.success_probability.toFixed(2) }),
      el("td", { text: `${candidate.estimated_duration_s}s` }),
      el("td", { text: candidate.risk.toFixed(2) }),
      el("td", { class: "reason", text: (candidate.reasons || []).join("; ") || "" })))));
}

/* ------------------------------------------------------------------ llm */
function renderLlm() {
  const interpretation = state.view && state.view.result && state.view.result.interpretation;
  const summaryBox = $("#llm-summary");
  const attemptsBox = $("#llm-attempts");

  if (!interpretation) {
    replace(summaryBox, el("p", {
      class: "placeholder",
      text: "Interpretation detail appears once the model has answered.",
    }));
    replace(attemptsBox);
    return;
  }

  const attempts = interpretation.attempts || [];
  const graph = interpretation.task_graph;
  const llm = (state.health && state.health.llm) || {};
  const totalLatency = attempts.reduce((sum, attempt) => sum + (attempt.latency_s || 0), 0);

  replace(summaryBox,
    el("div", { class: "facts" },
      chip("attempts", attempts.length, interpretation.valid_first_try ? "good" : "mock"),
      chip("valid first try", interpretation.valid_first_try ? "yes" : "no",
        interpretation.valid_first_try ? "good" : "bad"),
      chip("repair budget", llm.max_repair_attempts ?? "?"),
      chip("latency", `${totalLatency.toFixed(2)}s`)),
    graph && graph.rationale
      ? el("p", { class: "rationale", text: `\u201c${graph.rationale}\u201d` })
      : null);

  replace(attemptsBox, attempts.map((attempt, index) => {
    const rejected = (attempt.errors || []).length > 0;
    return el("details", {
      class: `attempt ${rejected ? "rejected" : "ok"}`,
      open: index === attempts.length - 1,
    },
      el("summary", {},
        el("span", { class: "n", text: `Attempt ${attempt.attempt}` }),
        el("span", { class: "tag", text: attempt.model || llm.model || "model" }),
        el("span", { class: "tag", text: attempt.decoding || "\u2014" }),
        el("span", { class: "tag", text: `${(attempt.latency_s || 0).toFixed(2)}s` }),
        el("span", {
          class: `tag ${rejected ? "bad" : "ok"}`,
          text: rejected ? `${attempt.errors.length} rejection(s)` : "accepted",
        })),
      el("div", { class: "attempt-body" },
        rejected
          ? el("ul", { class: "errors" }, attempt.errors.map((error) => el("li", {},
            el("span", { class: "where", text: `${error.check || "?"} ${error.code || ""}` }),
            " ",
            el("span", { text: error.message || JSON.stringify(error) }))))
          : null,
        el("pre", { text: JSON.stringify(attempt.task_graph, null, 2) })));
  }));
}

/* ------------------------------------------------------------------ world */
function renderWorld() {
  const world = state.world;
  const box = $("#world");
  if (!world) {
    replace(box, el("p", { class: "placeholder", text: "No world state." }));
    return;
  }

  const robots = el("section", {},
    el("h3", { text: "Robots" }),
    el("table", {}, el("tbody", {}, Object.entries(world.robots || {}).map(([id, robot]) =>
      el("tr", {},
        el("td", { class: "mono", text: id }),
        el("td", { class: "mono", text: robot.location || "\u2014" }),
        el("td", {}, el("span", {
          class: `pill ${robot.battery_percent < 30 ? "bad" : "good"}`,
          text: `${Math.round(robot.battery_percent)}%`,
        })),
        el("td", { class: "mono", text: (robot.payload_object_ids || []).join(", ") || "empty" }),
        el("td", {}, robot.fault_code
          ? el("span", { class: "pill bad", text: robot.fault_code })
          : el("span", { class: "pill", text: robot.availability })))))));

  const objects = el("section", {},
    el("h3", { text: "Objects" }),
    el("table", {}, el("tbody", {}, Object.entries(world.objects || {}).map(([id, object]) =>
      el("tr", {},
        el("td", { class: "mono", text: id }),
        el("td", { text: object.subtype || object.type }),
        el("td", { class: "mono", text: `${object.relation} ${object.location || "\u2014"}` }),
        el("td", { text: object.state || "" }),
        el("td", { text: object.mass_kg ? `${object.mass_kg}kg` : "" }))))));

  const facts = el("section", {},
    el("h3", { text: `Facts (${(world.facts || []).length})` }),
    el("ul", { class: "facts-list" }, (world.facts || []).map((fact) => el("li", {},
      el("span", {
        class: `badge ${fact.verified ? "observed" : "claimed"}`,
        text: fact.verified ? "observed" : "claimed",
      }),
      el("span", { text: fact.predicate })))));

  replace(box, robots, objects, facts);
}

/* ------------------------------------------------------------------ actions */
function readConstraints() {
  const constraints = {};
  const deadline = Number($("#deadline").value);
  if (deadline > 0) constraints.deadline_s = deadline;
  const risk = $("#max-risk").value;
  if (risk) constraints.max_risk = risk;
  if ($("#allow-teleoperation").checked) constraints.allow_teleoperation = true;
  const robots = [...document.querySelectorAll("[data-role=robot]:checked")].map((box) => box.value);
  if (robots.length) constraints.allowed_robot_ids = robots;
  return constraints;
}

async function submit() {
  const goal = $("#goal").value.trim();
  if (!goal) {
    notify("Enter a goal first.", "error");
    return;
  }
  notify("");
  $("#submit").disabled = true;
  try {
    await api("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ goal, mode: state.mode, constraints: readConstraints() }),
    });
  } catch (error) {
    notify(error.message, "error");
    $("#submit").disabled = false;
  }
}

async function cancel() {
  if (!state.view) return;
  try {
    await api(`/api/tasks/${encodeURIComponent(state.view.task_id)}/cancel`, { method: "POST" });
  } catch (error) {
    notify(error.message, "error");
  }
}

async function injectFault(form) {
  form.preventDefault();
  const body = {
    primitive: $("#fault-primitive").value,
    errorCode: $("#fault-code").value,
    remaining: Number($("#fault-remaining").value) || 1,
  };
  const nodeId = $("#fault-node").value.trim();
  if (nodeId) body.nodeId = nodeId;
  if ($("#fault-unverifiable").checked) body.observations = { verification_confirmed: false };
  try {
    const result = await api("/api/faults", { method: "POST", body: JSON.stringify(body) });
    notify(`Declared ${body.errorCode} on ${body.primitive}. ${result.faults} fault(s) armed.`, "info");
  } catch (error) {
    notify(error.message, "error");
  }
}

async function reset() {
  try {
    const result = await api("/api/reset", { method: "POST" });
    notify(`Reloaded ${result.scenario.split(/[\\/]/).pop()}.`, "info");
  } catch (error) {
    notify(error.message, "error");
  }
}

/* ------------------------------------------------------------------ tabs */
function showTab(name) {
  for (const button of document.querySelectorAll("#tabs button")) {
    button.classList.toggle("active", button.dataset.tab === name);
  }
  for (const pane of document.querySelectorAll(".pane")) {
    pane.classList.toggle("active", pane.dataset.pane === name);
  }
  if (name === "events") scrollFeed();
}

/* ------------------------------------------------------------------ boot */
function bind() {
  $("#submit").addEventListener("click", submit);
  $("#cancel").addEventListener("click", cancel);
  $("#reset").addEventListener("click", reset);
  $("#fault-form").addEventListener("submit", injectFault);

  $("#approval-form").addEventListener("submit", (form) => {
    form.preventDefault();
    const input = $("#approval-token");
    if (input.value.trim()) setApproval(input.value.trim(), true);
    input.value = "";
  });

  $("#goal").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) submit();
  });

  for (const button of document.querySelectorAll("#modes button")) {
    button.addEventListener("click", () => {
      state.mode = button.dataset.mode;
      for (const other of document.querySelectorAll("#modes button")) {
        other.classList.toggle("active", other === button);
      }
      $("#mode-hint").textContent = MODE_HINTS[state.mode];
      if (!$("#submit").disabled) $("#submit").textContent = labelForMode(state.mode);
    });
  }

  for (const button of document.querySelectorAll("#tabs button")) {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  }

  $("#failures-only").addEventListener("change", (event) => {
    state.failuresOnly = event.target.checked;
    renderFeed();
  });

  $("#mode-hint").textContent = MODE_HINTS[state.mode];
}

bind();
renderSummary();
connect();
