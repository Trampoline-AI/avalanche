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
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "listen host (default: loopback); non-loopback exposure requires an "
            "external trusted and authenticated boundary"
        ),
    )
    parser.add_argument(
        "--webhook-port", type=int, default=7434, help="loopback webhook HTTP port"
    )
    parser.add_argument("--ray", action="store_true", help="use the Ray executor")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.ray:
        print("Executor: Ray")
    else:
        print("Executor: Local")

    from . import serve

    print(f"Avalanche operator starting on {args.host}:{args.port}")
    print(f"Scanning flows: {', '.join(args.flows)}")
    serve(
        args.flows,
        port=args.port,
        host=args.host,
        webhook_port=args.webhook_port,
        executor_backend="ray" if args.ray else "local",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
