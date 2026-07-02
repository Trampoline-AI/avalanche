"""Allow running the runtime operator CLI."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m avalanche.operator",
        description="Start the local Avalanche flow operator.",
    )
    parser.add_argument(
        "--flows",
        nargs="+",
        required=True,
        metavar="PATH",
        help="flow file or directory to scan",
    )
    parser.add_argument("--port", type=int, default=7433, help="operator gRPC port")
    parser.add_argument("--ray", action="store_true", help="use the Ray executor")
    args = parser.parse_args(list(argv) if argv is not None else None)

    executor_factory = None
    if args.ray:
        from runtime.executor import RayExecutor

        executor_factory = RayExecutor
        print("Executor: Ray")
    else:
        print("Executor: Local")

    from . import serve

    print(f"Avalanche operator starting on port {args.port}")
    print(f"Scanning flows: {', '.join(args.flows)}")
    serve(args.flows, port=args.port, executor_factory=executor_factory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
