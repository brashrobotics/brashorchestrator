"""Expose safety policy state and operator approvals over ROS 2.

The orchestrator consults the safety manager in process, before every
submission, so this node is not in the enforcement path. It publishes the active
policy for visibility and accepts approval grants so an operator can authorize a
zone or a hazardous primitive without restarting anything.

A grant is always specific and always recorded. There is no blanket "allow
everything" token, because a permission that broad is indistinguishable from
having no policy at all.
"""

from __future__ import annotations

import json
from typing import Any

try:
    import rclpy
    from rclpy.node import Node
    from std_msgs.msg import String
    from std_srvs.srv import SetBool

    ROS_AVAILABLE = True
except ImportError:  # pragma: no cover - ROS-free environments
    ROS_AVAILABLE = False
    Node = object  # type: ignore[assignment,misc]

from brash_safety_manager.policy import SafetyManager, SafetyPolicy


class SafetyManagerNode(Node):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__("brash_safety_manager")

        self.declare_parameter("policy_path", "")
        self.declare_parameter("publish_period_s", 5.0)

        policy_path = self.get_parameter("policy_path").value or None
        self.manager = SafetyManager(SafetyPolicy.load(policy_path))

        self.policy_publisher = self.create_publisher(String, "/brash/safety/policy", 5)
        self.blocked_publisher = self.create_publisher(String, "/brash/safety/blocked", 20)
        self.create_subscription(String, "/brash/safety/grant", self._on_grant, 10)
        self.create_subscription(String, "/brash/safety/revoke", self._on_revoke, 10)
        self.create_service(SetBool, "~/set_enabled", self._set_enabled)

        self.create_timer(
            float(self.get_parameter("publish_period_s").value or 5.0), self._publish
        )
        self._published_blocks = 0
        self.get_logger().info(
            f"safety policy active: payload limit "
            f"{self.manager.policy.global_max_payload_kg:.1f}kg, "
            f"{len(self.manager.policy.approval_zones)} approval zone(s)"
        )

    def _on_grant(self, message: Any) -> None:
        token = str(message.data).strip()
        if token:
            self.manager.grant(token)
            self.get_logger().warning(f"operator approval granted for {token!r}")

    def _on_revoke(self, message: Any) -> None:
        token = str(message.data).strip()
        if token:
            self.manager.revoke(token)
            self.get_logger().info(f"operator approval revoked for {token!r}")

    def _set_enabled(self, request: Any, response: Any) -> Any:
        self.manager.policy.enabled = bool(request.data)
        response.success = True
        response.message = f"safety policy enabled={self.manager.policy.enabled}"
        # Disabling policy enforcement is a deliberate, loud, auditable act.
        log = self.get_logger().info if request.data else self.get_logger().error
        log(response.message)
        return response

    def _publish(self) -> None:
        policy = String()
        policy.data = json.dumps(
            {
                "enabled": self.manager.policy.enabled,
                "global_max_payload_kg": self.manager.policy.global_max_payload_kg,
                "human_clearance_m": dict(self.manager.policy.human_clearance_m),
                "forbidden_zones": sorted(self.manager.policy.forbidden_zones),
                "approval_zones": sorted(self.manager.policy.approval_zones),
                "approval_required_primitives": sorted(
                    self.manager.policy.approval_required_primitives
                ),
                "granted_approvals": sorted(self.manager.approvals),
            }
        )
        self.policy_publisher.publish(policy)

        while self._published_blocks < len(self.manager.blocked):
            entry = self.manager.blocked[self._published_blocks]
            self._published_blocks += 1
            message = String()
            message.data = json.dumps(entry)
            self.blocked_publisher.publish(message)


def main(argv: list[str] | None = None) -> int:
    if not ROS_AVAILABLE:
        print("rclpy is not available; this node requires a ROS 2 Jazzy install")
        return 1
    rclpy.init(args=argv)
    node = SafetyManagerNode()
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
