"""Mirror brashembodiedcontroller telemetry into normalized robot state.

This node is strictly read-only. The orchestrator does not command robots
directly: work goes to Fleet Control, which owns the vendor adapters, traffic
management, and safety interlocks. Subscribing to a robot's own controller for
pose and safety status gives the world model fresher, higher-rate information
than a fleet snapshot poll, without creating a second command path that could
contradict the fleet.

Per-robot topic namespaces come from ``embodied_namespace`` in
``config/robots.yaml``. For each namespace the node reads:

===================================  =========================================
``/{ns}/localization/odometry``       fused pose, from the LIO/odometry stack
``/{ns}/safety/enable``               whether the robot's safety gate permits motion
===================================  =========================================

and republishes ``brash_interfaces/RobotState`` on
``/brash/robots/{robot_id}/state``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

try:
    import rclpy
    from nav_msgs.msg import Odometry
    from rclpy.node import Node
    from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
    from std_msgs.msg import Bool

    from brash_interfaces.msg import RobotState

    ROS_AVAILABLE = True
except ImportError:  # pragma: no cover - ROS-free environments
    ROS_AVAILABLE = False
    Node = object  # type: ignore[assignment,misc]

import yaml


@dataclass(frozen=True, slots=True)
class _RobotTopics:
    """Just the topic wiring, read straight from the robot manifest.

    This node reads the YAML directly rather than importing the capability
    registry so that ``brash_world_model`` does not depend on
    ``brash_orchestrator_core``, which already depends on it.
    """

    robot_id: str
    namespace: str
    frame_id: str


def _read_robot_topics(path: str | None) -> list[_RobotTopics]:
    if not path:
        from pathlib import Path

        candidate = Path(__file__).resolve().parents[4] / "config" / "robots.yaml"
        if not candidate.is_file():
            return []
        path = str(candidate)
    document = yaml.safe_load(open(path, encoding="utf-8")) or {}
    robots = document.get("robots") or {}
    return [
        _RobotTopics(
            robot_id=str(robot_id),
            namespace=str((raw or {}).get("embodied_namespace", "")),
            frame_id=str((raw or {}).get("frame_id", "")),
        )
        for robot_id, raw in robots.items()
        if (raw or {}).get("embodied_namespace")
    ]


def _sensor_qos() -> Any:
    """Best-effort, depth 1: the latest pose matters, history does not."""
    return QoSProfile(
        reliability=QoSReliabilityPolicy.BEST_EFFORT,
        durability=QoSDurabilityPolicy.VOLATILE,
        history=QoSHistoryPolicy.KEEP_LAST,
        depth=1,
    )


class EmbodiedTelemetryNode(Node):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__("brash_embodied_telemetry")

        self.declare_parameter("robots_config", "")
        self.declare_parameter("publish_period_s", 1.0)

        self.robots = _read_robot_topics(self.get_parameter("robots_config").value or None)

        self._publishers: dict[str, Any] = {}
        self._frames: dict[str, str] = {}
        self._pose: dict[str, tuple[float, float, float]] = {}
        self._safety_enabled: dict[str, bool] = {}

        for robot in self.robots:
            self._frames[robot.robot_id] = robot.frame_id
            self._publishers[robot.robot_id] = self.create_publisher(
                RobotState, f"/brash/robots/{robot.robot_id}/state", 10
            )
            self.create_subscription(
                Odometry,
                f"/{robot.namespace}/localization/odometry",
                self._make_odometry_handler(robot.robot_id),
                _sensor_qos(),
            )
            self.create_subscription(
                Bool,
                f"/{robot.namespace}/safety/enable",
                self._make_safety_handler(robot.robot_id),
                10,
            )
            self.get_logger().info(
                f"mirroring {robot.namespace} telemetry as {robot.robot_id}"
            )

        period = float(self.get_parameter("publish_period_s").value or 1.0)
        self.create_timer(period, self._publish_all)

        if not self.robots:
            self.get_logger().warning(
                "no robot declares embodied_namespace, so no telemetry will be mirrored"
            )

    # ------------------------------------------------------------------
    def _make_odometry_handler(self, robot_id: str):
        def handler(message: Any) -> None:
            position = message.pose.pose.position
            orientation = message.pose.pose.orientation
            self._pose[robot_id] = (
                float(position.x),
                float(position.y),
                _yaw_from_quaternion(orientation),
            )

        return handler

    def _make_safety_handler(self, robot_id: str):
        def handler(message: Any) -> None:
            self._safety_enabled[robot_id] = bool(message.data)

        return handler

    def _publish_all(self) -> None:
        stamp = self.get_clock().now().to_msg()
        for robot_id, publisher in self._publishers.items():
            pose = self._pose.get(robot_id)
            message = RobotState()
            message.stamp = stamp
            message.robot_id = robot_id
            message.frame_id = self._frames.get(robot_id, "")
            # No pose yet means the controller has not been heard from, which is
            # reported as DEGRADED rather than guessed at.
            message.connection = "ONLINE" if pose is not None else "DEGRADED"
            enabled = self._safety_enabled.get(robot_id)
            message.availability = (
                "AVAILABLE" if enabled in (None, True) else "QUARANTINED"
            )
            message.fault_code = "" if enabled in (None, True) else "SAFETY_GATE_CLOSED"
            if pose is not None:
                message.x, message.y, message.yaw = pose
            publisher.publish(message)


def _yaw_from_quaternion(orientation: Any) -> float:
    x, y, z, w = (
        float(orientation.x),
        float(orientation.y),
        float(orientation.z),
        float(orientation.w),
    )
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def main(argv: list[str] | None = None) -> int:
    if not ROS_AVAILABLE:
        print("rclpy is not available; this node requires a ROS 2 Jazzy install")
        return 1
    rclpy.init(args=argv)
    node = EmbodiedTelemetryNode()
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
