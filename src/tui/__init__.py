"""Avalanche TUI — terminal UI for monitoring avalanche workflows."""

from __future__ import annotations


def launch_tui(argv: list[str] | None = None) -> None:
    """Launch the Avalanche TUI.

    Usage: python -m avalanche.tui [--connect HOST:PORT] [flow[/node]]
    Examples:
        python -m avalanche.tui                          # mock mode
        python -m avalanche.tui --connect localhost:7433  # real operator
        python -m avalanche.tui ml_workflow
    """
    import os
    import sys
    from pathlib import Path

    try:
        from .app import AvalancheApp
    except ModuleNotFoundError as exc:
        if exc.name == "textual":
            raise ModuleNotFoundError(
                "avalanche.tui is optional. Install it with `avalanche-ai[tui]` "
                "or run `uv sync --extra tui`.",
                name="textual",
            ) from exc
        raise

    args = list(argv) if argv is not None else sys.argv[1:]
    connect = None
    token = os.environ.get("AVALANCHE_TUI_GRPC_TOKEN") or None
    tls = os.environ.get("AVALANCHE_TUI_GRPC_TLS", "").lower() in {"1", "true", "yes"}
    tls_ca_cert = os.environ.get("AVALANCHE_TUI_GRPC_TLS_CA_CERT") or None
    flow = None
    node = None

    # Parse --connect flag
    if "--connect" in args:
        idx = args.index("--connect")
        if idx + 1 < len(args):
            connect = args[idx + 1]
            args = args[:idx] + args[idx + 2:]

    if "--token" in args:
        idx = args.index("--token")
        if idx + 1 < len(args):
            token = args[idx + 1]
            args = args[:idx] + args[idx + 2:]

    if "--tls" in args:
        idx = args.index("--tls")
        tls = True
        args = args[:idx] + args[idx + 1:]

    if "--insecure" in args:
        idx = args.index("--insecure")
        tls = False
        args = args[:idx] + args[idx + 1:]

    if "--tls-ca-cert" in args:
        idx = args.index("--tls-ca-cert")
        if idx + 1 < len(args):
            tls_ca_cert = args[idx + 1]
            tls = True
            args = args[:idx] + args[idx + 2:]

    # Remaining positional arg is flow[/node]
    if args:
        arg = args[0]
        if "/" in arg:
            flow, node = arg.split("/", 1)
        else:
            flow = arg

    # Build provider
    provider = None
    if connect:
        from avalanche.operator.client import GrpcStateProvider
        provider_kwargs = {}
        if token is not None:
            provider_kwargs["token"] = token
        if tls:
            provider_kwargs["tls"] = True
        if tls_ca_cert is not None:
            provider_kwargs["root_certificates"] = Path(tls_ca_cert).read_bytes()
        provider = GrpcStateProvider(connect, **provider_kwargs)

    app = AvalancheApp(provider=provider, workflow=flow, node=node)
    app.run()
