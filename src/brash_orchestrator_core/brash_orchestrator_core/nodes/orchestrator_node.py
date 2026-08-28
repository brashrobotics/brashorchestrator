"""ROS 2 wrapper around the orchestrator core.

This node is deliberately thin. It converts ROS types to plain Python, calls the
same :class:`Orchestrator` the CLI uses, and publishes events. All planning,
scheduling, and verification logic lives in the ROS-free core so it can be tested
without a ROS install.

Interfaces
==========

===================================  =========================================
``~/execute_task`` (action)          ``brash_interfaces/ExecuteTask``
``~/load_scenario`` (service)        ``brash_interfaces/LoadScenario``
``~/query_capabilities`` (service)   ``brash_interfaces/QueryCapabilities``
``~/register_robot`` (service)       ``brash_interfaces/RegisterRobot``
``/brash/execution_events`` (topic)  ``brash_interfaces/ExecutionEvent``
``/brash/world_state_delta`` (topic) ``brash_interfaces/WorldStateDelta``
===================================  =========================================

Cancelling the action cancels the underlying work, which propagates to the fleet
as a work-order cancellation.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

try:
    import rclpy
    from rclpy.action import ActionServer, CancelResponse, GoalResponse
    from rclpy.callback_groups import ReentrantCallbackGroup
    from rclpy.executors import MultiThreadedExecutor
    from rclpy.node import Node

    from brash_interfaces.action import ExecuteTask
    from brash_interfaces.msg import Capability, ExecutionEvent as ExecutionEventMsg
    from brash_interfaces.srv import LoadScenario, QueryCapabilities, RegisterRobot

    ROS_AVAILABLE = True
except ImportError:  # pragma: no cover - ROS-free environments
    ROS_AVAILABLE = False
    Node = object  # type: ignore[assignment,misc]

from brash_orchestrator_core.backends.fleet_control import FleetControlSettings
from brash_orchestrator_core.capability_registry import RobotProfile, parse_robot_manifest
from brash_orchestrator_core.events import EventLog, ExecutionEvent
from brash_orchestrator_core.orchestrator import Orchestrator, TaskResult
from brash_world_model.model import WorldModel
from brash_world_model.twin_bundle_loader import (
    TwinBundleError,
    ground_world_model,
    load_twin_bundle,
)


class OrchestratorNode(Node):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__("brash_orchestrator")

        self.declare_parameter("scenario_path", "")
        self.declare_parameter("twin_bundle_path", "")
        self.declare_parameter("llm_profile", "")
        self.declare_parameter("fleet_base_url", "")
        self.declare_parameter("fleet_site", "")
        self.declare_parameter("fleet_token", "")
        self.declare_parameter("dry_run", False)
        self.declare_parameter("record_trials", True)

        scenario = self.get_parameter("scenario_path").value or None
        twin_bundle = self.get_parameter("twin_bundle_path").value or None
        profile = self.get_parameter("llm_profile").value or None

        fleet_settings = FleetControlSettings.from_env()
        for parameter, attribute in (
            ("fleet_base_url", "base_url"),
            ("fleet_site", "site_id"),
            ("fleet_token", "token"),
        ):
            value = self.get_parameter(parameter).value
            if value:
                setattr(fleet_settings, attribute, value)

        self.orchestrator = Orchestrator.build(
            scenario_path=scenario,
            twin_bundle_path=twin_bundle,
            llm_profile=profile,
            fleet_settings=fleet_settings,
            dry_run=bool(self.get_parameter("dry_run").value),
            record_trials=bool(self.get_parameter("record_trials").value),
        )

        callback_group = ReentrantCallbackGroup()
        self.event_publisher = self.create_publisher(
            ExecutionEventMsg, "/brash/execution_events", 20
        )
        self._action_server = ActionServer(
            self,
            ExecuteTask,
            "~/execute_task",
            execute_callback=self._execute_task,
            goal_callback=self._accept_goal,
            cancel_callback=self._accept_cancel,
            callback_group=callback_group,
        )
        self.create_service(
            LoadScenario, "~/load_scenario", self._load_scenario, callback_group=callback_group
        )
        self.create_service(
            QueryCapabilities,
            "~/query_capabilities",
            self._query_capabilities,
            callback_group=callback_group,
        )
        self.create_service(
            RegisterRobot, "~/register_robot", self._register_robot, callback_group=callback_group
        )

        # One event loop owned by a background thread. The action callbacks are
        # synchronous from ROS's point of view and submit coroutines onto it.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="orchestrator-asyncio", daemon=True
        )
        self._loop_thread.start()

        self.get_logger().info(
            f"BrashOrchestrator ready. LLM profile "
            f"{self.orchestrator.llm_settings.profile_name}, backend "
            f"{self.orchestrator.backend.name}, dry_run="
            f"{self.orchestrator.dry_run}"
        )

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def destroy_node(self) -> bool:
        try:
            asyncio.run_coroutine_threadsafe(self.orchestrator.close(), self._loop).result(
                timeout=5
            )
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        return super().destroy_node()

    # ------------------------------------------------------------------
    def _accept_goal(self, goal_request: Any) -> Any:
        if not goal_request.natural_language_goal.strip():
            self.get_logger().warning("rejecting a task with an empty goal")
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _accept_cancel(self, _goal_handle: Any) -> Any:
        return CancelResponse.ACCEPT

    def _execute_task(self, goal_handle: Any) -> Any:
        request = goal_handle.request
        task_id = request.task_id or f"task-{self.get_clock().now().nanoseconds}"
        constraints = _parse_json_object(request.constraints_json)

        log = EventLog(task_id=task_id)
        node_ids: list[str] = []

        def on_event(event: ExecutionEvent) -> None:
            self._publish_event(event)
            if event.node_id and event.node_id != "-" and event.node_id not in node_ids:
                node_ids.append(event.node_id)
            feedback = ExecuteTask.Feedback()
            feedback.execution_state = event.event
            feedback.active_node_id = event.node_id
            feedback.assigned_robot_id = event.robot_id or ""
            feedback.progress = _progress(log)
            feedback.event_json = json.dumps(event.to_dict())
            goal_handle.publish_feedback(feedback)

        log.subscribe(on_event)

        future = asyncio.run_coroutine_threadsafe(
            self.orchestrator.run_task(
                task_id=task_id,
                goal=request.natural_language_goal,
                constraints=constraints,
                events=log,
            ),
            self._loop,
        )

        while not future.done():
            if goal_handle.is_cancel_requested:
                future.cancel()
                goal_handle.canceled()
                result = ExecuteTask.Result()
                result.success = False
                result.failure_code = "CANCELLED"
                result.summary = "the task was cancelled by the client"
                result.final_state_json = "{}"
                return result
            self._loop_sleep(0.1)

        try:
            task_result: TaskResult = future.result()
        except Exception as error:  # noqa: BLE001 - report, never crash the node
            self.get_logger().error(f"task {task_id} raised: {error}")
            goal_handle.abort()
            result = ExecuteTask.Result()
            result.success = False
            result.failure_code = "UNKNOWN_FAILURE"
            result.summary = str(error)
            result.final_state_json = "{}"
            return result

        if task_result.success:
            goal_handle.succeed()
        else:
            goal_handle.abort()

        result = ExecuteTask.Result()
        result.success = task_result.success
        result.failure_code = task_result.failure_code
        result.summary = task_result.summary
        result.final_state_json = json.dumps(
            task_result.outcome.to_dict() if task_result.outcome else task_result.to_dict()
        )
        return result

    @staticmethod
    def _loop_sleep(seconds: float) -> None:
        import time

        time.sleep(seconds)

    def _publish_event(self, event: ExecutionEvent) -> None:
        message = ExecutionEventMsg()
        message.stamp = self.get_clock().now().to_msg()
        message.task_id = event.task_id
        message.node_id = event.node_id
        message.robot_id = event.robot_id or ""
        message.action = event.action or ""
        message.event = event.event
        message.error_code = event.error_code
        message.attempt = int(event.attempt)
        message.detail = event.detail[:2000]
        message.external_reference = event.external_reference or ""
        message.event_json = json.dumps(event.to_dict())
        self.event_publisher.publish(message)

    # ------------------------------------------------------------------
    def _load_scenario(self, request: Any, response: Any) -> Any:
        try:
            world = WorldModel.load(request.scenario_path)
        except Exception as error:  # noqa: BLE001
            response.success = False
            response.message = f"could not load {request.scenario_path}: {error}"
            return response

        ungrounded: list[str] = []
        if request.twin_bundle_path:
            try:
                bundle = load_twin_bundle(request.twin_bundle_path)
                ungrounded = ground_world_model(world, bundle)
            except TwinBundleError as error:
                response.success = False
                response.message = f"twin bundle rejected: {error}"
                return response

        self.orchestrator.world = world
        response.success = True
        response.message = f"loaded {request.scenario_path}"
        response.location_count = len(world.locations)
        response.object_count = len(world.objects)
        response.robot_count = len(world.robots)
        response.ungrounded_location_ids = ungrounded
        self.get_logger().info(response.message)
        return response

    def _query_capabilities(self, request: Any, response: Any) -> Any:
        registry = self.orchestrator.registry
        robot_ids = [request.robot_id] if request.robot_id else list(registry.robot_ids)
        response.robot_ids = []
        response.capabilities = []
        response.ineligibility_reasons = []

        for robot_id in robot_ids:
            profile = registry.get(robot_id)
            if profile is None:
                continue
            names = (
                [request.capability_name] if request.capability_name else list(profile.capabilities)
            )
            reason = ""
            for name in names:
                capability = profile.capability(name)
                if capability is None:
                    reason = f"{name} is not enabled"
                    continue
                if (
                    request.required_mass_kg > 0
                    and capability.max_mass_kg is not None
                    and request.required_mass_kg > capability.max_mass_kg
                ):
                    reason = (
                        f"{name} supports {capability.max_mass_kg:.1f}kg, "
                        f"{request.required_mass_kg:.1f}kg requested"
                    )
                    continue
                response.capabilities.append(_to_capability_msg(capability))
            response.robot_ids.append(robot_id)
            response.ineligibility_reasons.append(reason)
        return response

    def _register_robot(self, request: Any, response: Any) -> Any:
        import yaml

        try:
            manifest = yaml.safe_load(request.capability_manifest) or {}
            if not isinstance(manifest, dict):
                raise ValueError("manifest must be a mapping")
            profile = parse_robot_manifest(request.robot_id, manifest)
        except Exception as error:  # noqa: BLE001
            response.accepted = False
            response.reject_reason = f"invalid manifest: {error}"
            return response

        self.orchestrator.registry.register(
            RobotProfile(
                robot_id=profile.robot_id,
                type=request.type or profile.type,
                vendor=request.vendor or profile.vendor,
                fleet_robot_id=profile.fleet_robot_id,
                capabilities=profile.capabilities,
                frame_id=request.frame_id or profile.frame_id,
                status_topic=request.status_topic or profile.status_topic,
                embodied_namespace=profile.embodied_namespace,
                energy_per_second_wh=profile.energy_per_second_wh,
            )
        )
        response.accepted = True
        response.reject_reason = ""
        response.registered_capabilities = [
            _to_capability_msg(capability) for capability in profile.capabilities.values()
        ]
        self.get_logger().info(f"registered robot {profile.robot_id}")
        return response


def _to_capability_msg(capability: Any) -> Any:
    message = Capability()
    message.name = capability.name
    message.enabled = capability.enabled
    message.success_prior = float(capability.success_prior)
    message.max_mass_kg = float(capability.max_mass_kg or 0.0)
    message.min_height_m = float(capability.min_height_m or 0.0)
    message.max_height_m = float(capability.max_height_m or 0.0)
    message.pose_tolerance_m = float(capability.pose_tolerance_m or 0.0)
    message.yaw_tolerance_deg = float(capability.yaw_tolerance_deg or 0.0)
    message.qualifier_keys = [str(key) for key in capability.qualifiers]
    message.qualifier_values = [str(value) for value in capability.qualifiers.values()]
    return message


def _parse_json_object(text: str) -> dict[str, Any]:
    if not text or not text.strip():
        return {}
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return document if isinstance(document, dict) else {}


def _progress(log: EventLog) -> float:
    """Fraction of node-level work that reached a terminal state."""
    terminal = {"NODE_SUCCEEDED", "NODE_FAILED", "NODE_SKIPPED", "NODE_CANCELLED"}
    started = {event.node_id for event in log.events if event.event == "NODE_STARTED"}
    finished = {event.node_id for event in log.events if event.event in terminal}
    if not started:
        return 0.0
    return min(1.0, len(finished) / max(len(started), 1))


def main(argv: list[str] | None = None) -> int:
    if not ROS_AVAILABLE:
        print(
            "rclpy is not available. Source a ROS 2 Jazzy install, or use the "
            "ROS-free CLI: python -m brash_orchestrator_core.cli --help"
        )
        return 1

    rclpy.init(args=argv)
    node = OrchestratorNode()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
