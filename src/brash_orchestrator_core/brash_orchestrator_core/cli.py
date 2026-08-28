"""ROS-free command line for the orchestrator.

Everything except the ROS topic and action surface is reachable from here, which
means the planning and execution pipeline can be developed, demonstrated, and
debugged on any machine without a ROS install.

    brash-orchestrator plan "Take the drill ... place it on shelf B12"
    brash-orchestrator validate my_graph.json
    brash-orchestrator run "Move the tool box to the handover station" --dry-run
    brash-orchestrator run "..." --fleet-url http://127.0.0.1:4373
    brash-orchestrator inspect
    brash-orchestrator serve --port 8088
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from brash_orchestrator_core.backends.fleet_control import FleetControlSettings
from brash_orchestrator_core.events import ExecutionEvent
from brash_orchestrator_core.orchestrator import Orchestrator, TaskResult, derive_task_id
from brash_orchestrator_core.scheduler import describe_plan
from brash_orchestrator_core.task_graph import TaskGraph, TaskGraphError
from brash_orchestrator_core.web.app import DEFAULT_HOST as WEB_DEFAULT_HOST
from brash_orchestrator_core.web.app import DEFAULT_PORT as WEB_DEFAULT_PORT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brash-orchestrator",
        description="Capability-aware task planning and execution for robot teams.",
    )
    parser.add_argument("--scenario", help="world model scenario YAML")
    parser.add_argument("--twin-bundle", help="brash.twin-map-bundle/v2 directory or manifest")
    parser.add_argument("--llm-profile", help="profile from config/llm.yaml")
    parser.add_argument("--fleet-url", help="Fleet Control base URL")
    parser.add_argument("--fleet-site", help="value sent as the X-Fleet-Site header")
    parser.add_argument("--fleet-token", help="bearer token for Fleet Control")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--no-trials", action="store_true", help="do not append a trial record"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser(
        "plan", help="interpret a goal and show the graph and assignments, executing nothing"
    )
    plan.add_argument("goal")
    _add_task_arguments(plan)

    validate = subparsers.add_parser(
        "validate", help="validate a task graph JSON file against the world model"
    )
    validate.add_argument("graph_path")
    _add_task_arguments(validate)

    run = subparsers.add_parser("run", help="plan and execute a goal end to end")
    run.add_argument("goal")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="run the full pipeline but submit no work order",
    )
    run.add_argument("--quiet", action="store_true", help="do not stream events")
    _add_task_arguments(run)

    subparsers.add_parser(
        "inspect", help="show the loaded actions, robots, world state, and backend health"
    )

    serve = subparsers.add_parser("serve", help="run the web operator console")
    serve.add_argument("--host", default=WEB_DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=WEB_DEFAULT_PORT)
    serve.add_argument(
        "--approve",
        action="append",
        dest="approvals",
        default=[],
        help="grant an operator approval token at startup; repeatable",
    )
    return parser


def _add_task_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-id", help="defaults to a name derived from the goal")
    parser.add_argument("--deadline", type=float, help="deadline in seconds")
    parser.add_argument(
        "--max-risk", choices=("low", "medium", "high"), help="risk ceiling for assignment"
    )
    parser.add_argument(
        "--allow-teleoperation",
        action="store_true",
        help="permit teleoperation as a recovery option",
    )
    parser.add_argument(
        "--robot",
        action="append",
        dest="allowed_robot_ids",
        help="restrict assignment to this robot; repeatable",
    )
    parser.add_argument(
        "--approve",
        action="append",
        dest="approvals",
        default=[],
        help="grant an operator approval token; repeatable",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The console owns its own event loop, so it runs outside _dispatch.
    if args.command == "serve":
        return _command_serve(args)
    try:
        return asyncio.run(_dispatch(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - a CLI reports, it does not traceback
        print(f"error: {error}", file=sys.stderr)
        return 1


async def _dispatch(args: argparse.Namespace) -> int:
    orchestrator = Orchestrator.build(
        scenario_path=args.scenario,
        twin_bundle_path=args.twin_bundle,
        llm_profile=args.llm_profile,
        fleet_settings=_fleet_settings(args),
        dry_run=getattr(args, "dry_run", False),
        record_trials=not args.no_trials,
        approvals=getattr(args, "approvals", None) or (),
    )
    try:
        if args.command == "plan":
            return await _command_plan(orchestrator, args)
        if args.command == "validate":
            return _command_validate(orchestrator, args)
        if args.command == "run":
            return await _command_run(orchestrator, args)
        if args.command == "inspect":
            return await _command_inspect(orchestrator, args)
        return 2
    finally:
        await orchestrator.close()


def _fleet_settings(args: argparse.Namespace) -> FleetControlSettings:
    settings = FleetControlSettings.from_env()
    if args.fleet_url:
        settings.base_url = args.fleet_url.rstrip("/")
    if args.fleet_site:
        settings.site_id = args.fleet_site
    if args.fleet_token:
        settings.token = args.fleet_token
    return settings


def _constraints(args: argparse.Namespace) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    if getattr(args, "deadline", None):
        constraints["deadline_s"] = args.deadline
    if getattr(args, "max_risk", None):
        constraints["max_risk"] = args.max_risk
    if getattr(args, "allow_teleoperation", False):
        constraints["allow_teleoperation"] = True
    if getattr(args, "allowed_robot_ids", None):
        constraints["allowed_robot_ids"] = list(args.allowed_robot_ids)
    if getattr(args, "dry_run", False):
        constraints["dry_run"] = True
    return constraints


def _task_id(args: argparse.Namespace, goal: str) -> str:
    return derive_task_id(goal, getattr(args, "task_id", None))


# ----------------------------------------------------------------------
async def _command_plan(orchestrator: Orchestrator, args: argparse.Namespace) -> int:
    result = await orchestrator.plan_task(
        task_id=_task_id(args, args.goal),
        goal=args.goal,
        constraints=_constraints(args),
        approvals=args.approvals or (),
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0 if result.plan and result.plan.feasible else 1

    interpretation = result.interpretation
    if interpretation is None or interpretation.graph is None:
        print("the goal could not be turned into a valid task graph.")
        _print_errors(result)
        return 1

    graph = interpretation.graph
    attempts = len(interpretation.attempts)
    print(f"goal: {args.goal}")
    print(f"task: {graph.task_id}")
    print(f"goal predicate: {graph.goal_predicate}")
    print(
        f"interpreted by {orchestrator.llm_settings.profile_name} in "
        f"{attempts} attempt(s)"
    )
    if graph.rationale:
        print(f"rationale: {graph.rationale}")
    print()
    for node in graph.nodes:
        depends = ", ".join(node.depends_on) or "-"
        parameters = ", ".join(f"{key}={value}" for key, value in node.parameters.items())
        print(f"  {node.id:>4}  {node.action}({parameters})  after [{depends}]")
    print()
    if result.plan is None:
        _print_errors(result)
        return 1
    print(describe_plan(result.plan, graph.execution_layers()))
    if not result.plan.feasible:
        print()
        print(f"plan is not executable: {result.rejection_detail}")
        return 1
    return 0


def _command_validate(orchestrator: Orchestrator, args: argparse.Namespace) -> int:
    path = Path(args.graph_path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"could not read {path}: {error}")
        return 1

    schema_errors = orchestrator.schemas.validation_errors("task_graph", document)
    if schema_errors:
        print(f"{path} does not satisfy brash.task-graph/v1:")
        for error in schema_errors:
            print(f"  {error}")
        return 1

    try:
        graph = TaskGraph.from_dict(document)
    except TaskGraphError as error:
        print(f"{path} is not a usable task graph: {error}")
        return 1

    report = orchestrator.validate_graph(
        graph, constraints=_constraints(args), approvals=args.approvals or ()
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0 if report.ok else 1

    for warning in report.warnings:
        print(f"warning: {warning}")
    if report.ok:
        print(f"{path} is valid: {len(graph)} nodes, goal {graph.goal_predicate}")
        print(f"execution waves: {graph.execution_layers()}")
        return 0
    print(f"{path} was rejected:")
    for error in report.errors:
        print(f"  {error}")
    return 1


async def _command_run(orchestrator: Orchestrator, args: argparse.Namespace) -> int:
    from brash_orchestrator_core.events import EventLog

    log = EventLog(task_id=_task_id(args, args.goal))
    if not args.quiet and not args.json:
        log.subscribe(_print_event)

    result = await orchestrator.run_task(
        task_id=log.task_id,
        goal=args.goal,
        constraints=_constraints(args),
        approvals=args.approvals or (),
        events=log,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0 if result.success else 1

    print()
    _print_summary(result)
    return 0 if result.success else 1


def _command_serve(args: argparse.Namespace) -> int:
    from brash_orchestrator_core.web.app import serve

    try:
        serve(
            host=args.host,
            port=args.port,
            scenario_path=args.scenario,
            twin_bundle_path=args.twin_bundle,
            llm_profile=args.llm_profile,
            fleet_settings=_fleet_settings(args),
            record_trials=not args.no_trials,
            approvals=tuple(args.approvals or ()),
        )
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # noqa: BLE001 - a CLI reports, it does not traceback
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


async def _command_inspect(orchestrator: Orchestrator, args: argparse.Namespace) -> int:
    health = await orchestrator.backend.health()
    payload = {
        "llm_profile": orchestrator.llm_settings.profile_name,
        "llm_provider": orchestrator.provider.name,
        "backend": orchestrator.backend.name,
        "backend_health": health.to_dict(),
        "actions": list(orchestrator.library.names),
        "robots": {
            robot_id: sorted(
                name
                for name, capability in orchestrator.registry[robot_id].capabilities.items()
                if capability.enabled
            )
            for robot_id in orchestrator.registry.robot_ids
        },
        "recovery_policies": list(orchestrator.recovery.policies.error_codes),
        "world": orchestrator.world.summary(),
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(f"LLM: {payload['llm_profile']} via {payload['llm_provider']}")
    print(f"backend: {payload['backend']}")
    print(f"  reachable: {health.reachable}")
    print(f"  {health.detail}")
    print(f"  supported kinds: {', '.join(sorted(health.supported_kinds)) or 'none'}")
    print()
    print(f"actions ({len(payload['actions'])}): {', '.join(payload['actions'])}")
    print()
    for robot_id, capabilities in payload["robots"].items():
        print(f"{robot_id}: {', '.join(capabilities)}")
    print()
    world = orchestrator.world
    print(
        f"world: {len(world.locations)} locations, {len(world.objects)} objects, "
        f"{len(world.robots)} robots, map {world.fleet_map_id}"
    )
    for fact in world.facts():
        mark = "observed" if fact.verified else "claimed "
        print(f"  [{mark}] {fact.predicate}")
    return 0 if health.reachable else 1


# ----------------------------------------------------------------------
def _print_event(event: ExecutionEvent) -> None:
    print(f"  {event.occurred_at[11:23]}  {event.describe()}")


def _print_errors(result: TaskResult) -> None:
    if result.interpretation is not None:
        for error in result.interpretation.report.errors:
            print(f"  {error}")
    if result.rejection_detail:
        print(f"  {result.rejection_detail}")


def _print_summary(result: TaskResult) -> None:
    outcome = result.outcome
    verdict = "SUCCEEDED" if result.success else f"FAILED [{result.failure_code}]"
    print(f"task {result.task_id}: {verdict}")
    print(f"  {result.summary}")
    if outcome is None:
        return
    print(f"  goal predicate: {outcome.goal_predicate}")
    print(f"  goal verified by observation: {outcome.goal_verified}")
    print(f"  duration: {outcome.duration_s:.1f}s")
    if outcome.recovery_actions:
        print(f"  recovery actions: {len(outcome.recovery_actions)}")
        for action in outcome.recovery_actions:
            print(
                f"    {action['candidate_id']} ({action['strategy']}, "
                f"ranked by {action['ranked_by']}): {action['reason']}"
            )
    if outcome.human_interventions:
        print(f"  human interventions: {outcome.human_interventions}")
    print("  nodes:")
    for node in outcome.nodes:
        robot = node.assigned_robot_id or "-"
        marker = "ok " if node.state == "SUCCEEDED" else "!! "
        print(
            f"    {marker}{node.node_id:>4} {node.action:<26} {robot:<16} "
            f"{node.state:<10} {node.error_code}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
