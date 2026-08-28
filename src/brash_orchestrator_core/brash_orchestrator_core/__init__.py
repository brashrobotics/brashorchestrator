"""BrashOrchestrator core: interpretation, validation, scheduling, and execution.

Everything in this package is deliberately ROS-free so that the planning and
execution logic can be developed and tested on any platform. The thin ROS 2
wrappers live in :mod:`brash_orchestrator_core.nodes`.
"""

from __future__ import annotations

__version__ = "0.1.0"

TASK_GRAPH_CONTRACT = "brash.task-graph/v1"
EXECUTION_EVENT_CONTRACT = "brash.execution-event/v1"
RECOVERY_PROPOSAL_CONTRACT = "brash.recovery-proposal/v1"
EXECUTION_RESULT_CONTRACT = "brash.execution-result/v1"
TASK_REQUEST_CONTRACT = "brash.task-request/v1"
MANIPULATION_WORK_ORDER_CONTRACT = "brash.manipulation-work-order/v1"
