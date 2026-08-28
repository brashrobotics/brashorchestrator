"""Bring up the orchestrator against a running Fleet Control.

    ros2 launch brash_bringup orchestrator.launch.py \
        scenario_path:=/path/to/world_demo.yaml \
        fleet_base_url:=http://fleet-control.local:4273 \
        llm_profile:=cosmos_reason

Set ``dry_run:=true`` to plan, validate, and assign without submitting a single
work order, which is the safe way to try a new goal on a live site.
"""

from __future__ import annotations

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

ARGUMENTS = (
    ("scenario_path", "", "world model scenario YAML; empty loads config/world_demo.yaml"),
    ("twin_bundle_path", "", "brash.twin-map-bundle/v2 directory used to ground locations"),
    ("llm_profile", "", "profile from config/llm.yaml; empty uses active_profile"),
    ("fleet_base_url", "http://127.0.0.1:4273", "Fleet Control base URL"),
    ("fleet_site", "plant-a", "value sent as the X-Fleet-Site header"),
    ("fleet_token", "brash-dev-operator", "bearer token for Fleet Control"),
    ("dry_run", "false", "plan and assign without submitting work orders"),
    ("record_trials", "true", "append execution-grounded trial records"),
    ("enable_telemetry_mirror", "false", "mirror brashembodiedcontroller telemetry"),
    ("log_level", "info", "ROS logging level"),
)


def generate_launch_description() -> LaunchDescription:
    declarations = [
        DeclareLaunchArgument(name, default_value=default, description=description)
        for name, default, description in ARGUMENTS
    ]
    log_level = LaunchConfiguration("log_level")

    orchestrator = Node(
        package="brash_orchestrator_core",
        executable="orchestrator_node",
        name="brash_orchestrator",
        output="screen",
        arguments=["--ros-args", "--log-level", log_level],
        parameters=[
            {
                "scenario_path": LaunchConfiguration("scenario_path"),
                "twin_bundle_path": LaunchConfiguration("twin_bundle_path"),
                "llm_profile": LaunchConfiguration("llm_profile"),
                "fleet_base_url": LaunchConfiguration("fleet_base_url"),
                "fleet_site": LaunchConfiguration("fleet_site"),
                "fleet_token": LaunchConfiguration("fleet_token"),
                "dry_run": LaunchConfiguration("dry_run"),
                "record_trials": LaunchConfiguration("record_trials"),
            }
        ],
    )

    world_model = Node(
        package="brash_world_model",
        executable="world_model_node",
        name="brash_world_model",
        output="screen",
        parameters=[
            {
                "scenario_path": LaunchConfiguration("scenario_path"),
                "twin_bundle_path": LaunchConfiguration("twin_bundle_path"),
            }
        ],
    )

    safety = Node(
        package="brash_safety_manager",
        executable="safety_manager_node",
        name="brash_safety_manager",
        output="screen",
    )

    telemetry = Node(
        package="brash_world_model",
        executable="embodied_telemetry_node",
        name="brash_embodied_telemetry",
        output="screen",
        condition=_if("enable_telemetry_mirror"),
    )

    return LaunchDescription([*declarations, orchestrator, world_model, safety, telemetry])


def _if(argument: str):
    from launch.conditions import IfCondition

    return IfCondition(LaunchConfiguration(argument))
