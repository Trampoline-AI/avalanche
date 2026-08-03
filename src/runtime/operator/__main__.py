"""Allow running the runtime operator CLI."""

from __future__ import annotations

import argparse
import logging
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
    parser.add_argument("--web", action="store_true", help="serve the local browser UI")
    parser.add_argument(
        "--web-host",
        default="127.0.0.1",
        help="browser UI listen host (default: loopback)",
    )
    parser.add_argument("--web-port", type=int, default=7435, help="browser UI HTTP port")
    parser.add_argument(
        "--web-trusted-proxy",
        action="store_true",
        help=(
            "confirm non-loopback browser traffic is protected by an external trusted "
            "and authenticated boundary"
        ),
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="WARNING",
        help="terminal log level (default: WARNING)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

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
        web=args.web,
        web_host=args.web_host,
        web_port=args.web_port,
        web_trusted_proxy=args.web_trusted_proxy,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
