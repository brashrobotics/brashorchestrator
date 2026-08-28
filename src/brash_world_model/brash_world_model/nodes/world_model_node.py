"""Publish and maintain the shared world model over ROS 2.

The orchestrator node keeps its own in-process world model for planning. This
node exists so that other tools, dashboards, and the digital twin can observe
the same symbolic state and contribute observations to it, without any of them
needing to reach into the orchestrator.
"""

from __future__ import annotations

import json
from typing import Any

try:
    import rclpy
    from rclpy.node import Node
    from std_msgs.msg import String

    from brash_interfaces.msg import RobotState, WorldStateDelta as WorldStateDeltaMsg
    from brash_interfaces.srv import LoadScenario

    ROS_AVAILABLE = True
except ImportError:  # pragma: no cover - ROS-free environments
    ROS_AVAILABLE = False
    Node = object  # type: ignore[assignment,misc]

from brash_world_model.model import WorldModel, WorldStateDelta
from brash_world_model.twin_bundle_loader import (
    TwinBundleError,
    ground_world_model,
    load_twin_bundle,
)


class WorldModelNode(Node):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__("brash_world_model")

        self.declare_parameter("scenario_path", "")
        self.declare_parameter("twin_bundle_path", "")
        self.declare_parameter("publish_period_s", 2.0)

        scenario_path = self.get_parameter("scenario_path").value
        self.world = (
            WorldModel.load(scenario_path) if scenario_path else WorldModel()
        )

        bundle_path = self.get_parameter("twin_bundle_path").value
        if bundle_path:
            try:
                ungrounded = ground_world_model(self.world, load_twin_bundle(bundle_path))
                if ungrounded:
                    self.get_logger().warning(
                        "twin bundle did not ground: " + ", ".join(ungrounded)
                    )
            except TwinBundleError as error:
                self.get_logger().error(f"twin bundle rejected: {error}")

        self.snapshot_publisher = self.create_publisher(String, "/brash/world_state", 5)
        self.create_subscription(
            WorldStateDeltaMsg, "/brash/world_state_delta", self._on_delta, 20
        )
        self.create_subscription(RobotState, "/brash/robot_state", self._on_robot_state, 20)
        self.create_service(LoadScenario, "~/load_scenario", self._load_scenario)

        self.create_timer(
            float(self.get_parameter("publish_period_s").value or 2.0), self._publish_snapshot
        )
        self.get_logger().info(
            f"world model ready: {len(self.world.locations)} locations, "
            f"{len(self.world.objects)} objects, {len(self.world.robots)} robots"
        )

    # ------------------------------------------------------------------
    def _on_delta(self, message: Any) -> None:
        attributes: dict[str, Any] = {}
        for entry in message.attributes:
            path, _, value = str(entry).partition("=")
            if path:
                attributes[path] = _coerce(value)
        try:
            self.world.apply_delta(
                WorldStateDelta(
                    asserted=list(message.asserted),
                    retracted=list(message.retracted),
                    attributes=attributes,
                    source=message.source or "OBSERVATION",
                    confidence=float(message.confidence or 1.0),
                    task_id=message.task_id,
                    node_id=message.node_id,
                )
            )
        except Exception as error:  # noqa: BLE001 - a bad delta must not stop the node
            self.get_logger().warning(f"rejected world state delta: {error}")

    def _on_robot_state(self, message: Any) -> None:
        self.world.set_robot_state(
            message.robot_id,
            availability=message.availability.lower() or None,
            connection=message.connection.lower() or None,
            battery_percent=float(message.battery_percent) or None,
            location=message.location_id or None,
            fault_code=message.fault_code,
        )

    def _publish_snapshot(self) -> None:
        payload = String()
        payload.data = json.dumps(self.world.snapshot(), default=str)
        self.snapshot_publisher.publish(payload)

    def _load_scenario(self, request: Any, response: Any) -> Any:
        try:
            world = WorldModel.load(request.scenario_path)
        except Exception as error:  # noqa: BLE001
            response.success = False
            response.message = str(error)
            return response
        ungrounded: list[str] = []
        if request.twin_bundle_path:
            try:
                ungrounded = ground_world_model(world, load_twin_bundle(request.twin_bundle_path))
            except TwinBundleError as error:
                response.success = False
                response.message = f"twin bundle rejected: {error}"
                return response
        self.world = world
        response.success = True
        response.message = f"loaded {request.scenario_path}"
        response.location_count = len(world.locations)
        response.object_count = len(world.objects)
        response.robot_count = len(world.robots)
        response.ungrounded_location_ids = ungrounded
        return response


def _coerce(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def main(argv: list[str] | None = None) -> int:
    if not ROS_AVAILABLE:
        print("rclpy is not available; this node requires a ROS 2 Jazzy install")
        return 1
    rclpy.init(args=argv)
    node = WorldModelNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
