"""Self-contained drill demo: orchestrator plus the Fleet Control stub.

    ros2 launch brash_bringup demo.launch.py

This runs the whole pipeline with no external dependency: the bundled fleet stub
implements the target work-order contract including manipulation, and the ``mock``
LLM profile replays curated task graphs. Once a real vLLM or NIM endpoint is up,
override ``llm_profile`` to plan with the actual model against the same stub.

The stub is not a robot simulator. It answers the fleet's HTTP contract so the
orchestrator's real client, real event stream, and real recovery paths are all
exercised end to end.
"""

from __future__ import annotations

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    declarations = [
        DeclareLaunchArgument("stub_port", default_value="4373"),
        DeclareLaunchArgument(
            "llm_profile",
            default_value="mock",
            description="use 'cosmos_reason' once a local vLLM or NIM endpoint is serving",
        ),
        DeclareLaunchArgument("scenario_path", default_value=""),
        DeclareLaunchArgument("stub_delay", default_value="0.1"),
    ]

    stub = ExecuteProcess(
        cmd=[
            "python3",
            "-m",
            "tools.fleet_stub",
            "--port",
            LaunchConfiguration("stub_port"),
            "--delay",
            LaunchConfiguration("stub_delay"),
        ],
        output="screen",
        name="fleet_stub",
    )

    orchestrator = Node(
        package="brash_orchestrator_core",
        executable="orchestrator_node",
        name="brash_orchestrator",
        output="screen",
        parameters=[
            {
                "scenario_path": LaunchConfiguration("scenario_path"),
                "llm_profile": LaunchConfiguration("llm_profile"),
                "fleet_base_url": ["http://127.0.0.1:", LaunchConfiguration("stub_port")],
                "fleet_site": "plant-a",
                "fleet_token": "brash-dev-operator",
                "dry_run": False,
            }
        ],
    )

    safety = Node(
        package="brash_safety_manager",
        executable="safety_manager_node",
        name="brash_safety_manager",
        output="screen",
    )

    return LaunchDescription([*declarations, stub, orchestrator, safety])
