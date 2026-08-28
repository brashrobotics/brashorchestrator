"""Web operator console for BrashOrchestrator.

An aiohttp application over the same :class:`~brash_orchestrator_core.orchestrator.Orchestrator`
facade the CLI and the ROS 2 node use, plus a zero-build single page front end.
There is no Node toolchain and no new runtime dependency: aiohttp is already
required by the Fleet Control backend, and the browser assets are plain files
served from ``static/``.
"""

from brash_orchestrator_core.web.app import HUB, build_app, serve
from brash_orchestrator_core.web.hub import HubError, TaskHub, TaskRecord

__all__ = ["HUB", "HubError", "TaskHub", "TaskRecord", "build_app", "serve"]
