"""Allow running the runtime operator CLI."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from .discovery import (
    DEFAULT_DISCOVERY_TIMEOUT,
    WorkflowDiscoveryError,
    validate_discovery_timeout,
)
from .workspace_config import format_scan_targets, select_workflow_targets


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ava operator",
        description="Start the local Avalanche flow operator.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "flows",
        nargs="*",
        metavar="FLOW",
        help="workflow Python file or directory; uses workspace configuration when omitted",
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
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=DEFAULT_DISCOVERY_TIMEOUT,
        metavar="SECONDS",
        help=f"maximum seconds for one discovery scan (default: {DEFAULT_DISCOVERY_TIMEOUT:g})",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="WARNING",
        help="terminal log level (default: WARNING)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        validate_discovery_timeout(args.discovery_timeout)
        selection = select_workflow_targets(args.flows)
    except ValueError as exc:
        parser.error(str(exc))
    args.flows = list(selection.paths)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    print("Avalanche operator")
    for line in format_scan_targets(selection):
        print(line)
    print(f"  Executor: {'Ray' if args.ray else 'Local'}")
    print("  Discovering workflows...")

    from . import serve

    try:
        serve(
            args.flows,
            port=args.port,
            host=args.host,
            webhook_port=args.webhook_port,
            discovery_timeout=args.discovery_timeout,
            executor_backend="ray" if args.ray else "local",
        )
    except WorkflowDiscoveryError as exc:
        print("error: Avalanche operator discovery failed", file=sys.stderr)
        for diagnostic in exc.diagnostics:
            print(
                f"  {diagnostic.path}: {diagnostic.kind}: {diagnostic.message}",
                file=sys.stderr,
            )
        return 1
    print("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
