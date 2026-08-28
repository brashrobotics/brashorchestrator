"""Run the operator console: ``python -m brash_orchestrator_core.web``.

The full flag set, including scenario and Fleet Control options, lives on the
CLI's ``serve`` subcommand. This entry point exists for the common case.
"""

from __future__ import annotations

import argparse
import sys

from brash_orchestrator_core.web.app import DEFAULT_HOST, DEFAULT_PORT, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m brash_orchestrator_core.web",
        description="Web operator console for BrashOrchestrator.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--scenario", help="world model scenario YAML")
    parser.add_argument("--llm-profile", help="profile from config/llm.yaml")
    args = parser.parse_args(argv)

    try:
        serve(
            host=args.host,
            port=args.port,
            scenario_path=args.scenario,
            llm_profile=args.llm_profile,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # noqa: BLE001 - report, do not traceback
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
