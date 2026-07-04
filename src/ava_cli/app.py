"""Thin `ava` command layer over Avalanche implementation packages."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

_RUNTIME_OPTIONAL_MODULES = {"runtime", "grpc", "watchfiles", "croniter"}
_TUI_OPTIONAL_MODULES = {"tui", "textual", "grpc"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 2
    return args.handler(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ava",
        description="Avalanche command line interface.",
    )
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")

    operator = subcommands.add_parser(
        "operator",
        help="start the local flow operator",
        description="Start the local Avalanche operator and discover flows from files.",
    )
    operator.add_argument(
        "--flows",
        nargs="+",
        required=True,
        metavar="PATH",
        help="flow file or directory to scan",
    )
    operator.add_argument("--port", type=int, default=7433, help="operator gRPC port")
    operator.add_argument("--ray", action="store_true", help="use the Ray executor")
    operator.set_defaults(handler=_run_operator)

    run = subcommands.add_parser(
        "run",
        help="start a flow run on a local operator",
        description="Start a flow run through a running Avalanche operator.",
    )
    run.add_argument("flow", help="flow name to run")
    run.add_argument(
        "--connect",
        default="localhost:7433",
        metavar="HOST:PORT",
        help="operator address",
    )
    run.add_argument("--input", dest="input_json", help="JSON object for workflow input")
    run.add_argument("--context", dest="context_json", help="JSON object for run context")
    run.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="FIELD=PATH",
        help="attach local file bytes as a top-level input field",
    )
    run.add_argument(
        "--s3-file",
        action="append",
        default=[],
        metavar="FIELD=S3_URI",
        help="pass an S3 object reference as a top-level input field",
    )
    run.set_defaults(handler=_run_flow)

    tui = subcommands.add_parser(
        "tui",
        help="launch the terminal UI",
        description="Launch the Avalanche TUI in mock mode or connected mode.",
    )
    tui.add_argument("flow", nargs="?", help="optional flow[/node] deep link")
    tui.add_argument("--connect", metavar="HOST:PORT", help="operator address")
    tui.add_argument("--token", help="operator bearer token")
    tui.add_argument("--tls", action="store_true", help="use TLS for gRPC")
    tui.add_argument("--insecure", action="store_true", help="disable TLS for gRPC")
    tui.add_argument("--tls-ca-cert", metavar="PATH", help="TLS CA certificate")
    tui.set_defaults(handler=_run_tui)

    dev = subcommands.add_parser(
        "dev",
        help="start a local operator and connected TUI",
        description="Start a local operator, wait for readiness, then launch the TUI.",
    )
    dev.add_argument(
        "--flows",
        nargs="+",
        required=True,
        metavar="PATH",
        help="flow file or directory to scan",
    )
    dev.add_argument("--port", type=int, default=None, help="operator gRPC port")
    dev.add_argument("--ray", action="store_true", help="use the Ray executor")
    dev.add_argument(
        "--readiness-timeout",
        type=float,
        default=10.0,
        help="seconds to wait for operator readiness",
    )
    dev.set_defaults(handler=_run_dev)

    return parser


def _run_operator(args: argparse.Namespace) -> int:
    runtime_args = ["--flows", *args.flows, "--port", str(args.port)]
    if args.ray:
        runtime_args.append("--ray")
    return _operator_main(runtime_args)


def _operator_main(argv: list[str]) -> int:
    try:
        from runtime.operator.__main__ import main as operator_main
    except ModuleNotFoundError as exc:
        _raise_optional_extra_error(exc, "operator", "runtime", _RUNTIME_OPTIONAL_MODULES)
    return operator_main(argv)


def _run_flow(args: argparse.Namespace) -> int:
    provider = _make_provider(args.connect)
    try:
        input_payload = _parse_json_object(args.input_json, "--input")
        context_payload = _parse_json_object(args.context_json, "--context")
        file_payloads = _parse_file_inputs(args.file)
        s3_file_payloads = _parse_s3_file_inputs(args.s3_file)
        try:
            run_id = provider.start_run(
                args.flow,
                input=input_payload,
                context=context_payload,
                files=file_payloads,
                s3_files=s3_file_payloads,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if run_id:
            print(run_id)
            return 0
        if last_error := getattr(provider, "last_error", ""):
            print(last_error, file=sys.stderr)
        return 1
    finally:
        provider.close()


def _run_tui(args: argparse.Namespace) -> int:
    delegated_args: list[str] = []
    if args.connect:
        delegated_args.extend(["--connect", args.connect])
    if args.token:
        delegated_args.extend(["--token", args.token])
    if args.tls:
        delegated_args.append("--tls")
    if args.insecure:
        delegated_args.append("--insecure")
    if args.tls_ca_cert:
        delegated_args.extend(["--tls-ca-cert", args.tls_ca_cert])
    if args.flow:
        delegated_args.append(args.flow)
    _launch_tui(delegated_args)
    return 0


def _launch_tui(argv: Sequence[str]) -> None:
    try:
        from tui import launch_tui
    except ModuleNotFoundError as exc:
        _raise_optional_extra_error(exc, "tui", "tui", _TUI_OPTIONAL_MODULES)
    launch_tui(list(argv))


def _run_dev(args: argparse.Namespace) -> int:
    port = args.port if args.port is not None else _find_free_port()
    process = _start_operator_process(args.flows, port, args.ray)
    provider = None
    try:
        address = f"localhost:{port}"
        provider = _make_provider(address)
        _wait_for_provider(provider, timeout=args.readiness_timeout)
        _launch_connected_tui(address)
        return 0
    finally:
        if provider is not None:
            provider.close()
        _stop_operator_process(process)


def _find_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_operator_process(
    flows: list[str],
    port: int,
    use_ray: bool,
) -> subprocess.Popen:
    cmd = [
        sys.executable,
        "-m",
        "avalanche.operator",
        "--flows",
        *flows,
        "--port",
        str(port),
    ]
    if use_ray:
        cmd.append("--ray")
    return subprocess.Popen(cmd)


def _make_provider(address: str):
    try:
        from avalanche.operator.client import GrpcStateProvider
    except ModuleNotFoundError as exc:
        _raise_optional_extra_error(exc, "operator", "runtime", _RUNTIME_OPTIONAL_MODULES)
    return GrpcStateProvider(address)


def _parse_json_object(payload: str | None, flag: str) -> dict | None:
    if payload is None:
        return None
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{flag} must be valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{flag} must be a JSON object")
    return value


def _parse_assignment(value: str, flag: str) -> tuple[str, str]:
    field, separator, payload = value.partition("=")
    if not separator or not field or not payload:
        raise ValueError(f"{flag} expects FIELD=VALUE")
    return field, payload


def _parse_file_inputs(values: list[str]):
    from avalanche.runtime import File

    files = {}
    for value in values:
        field, path = _parse_assignment(value, "--file")
        files[field] = File.from_path(Path(path))
    return files


def _parse_s3_file_inputs(values: list[str]):
    from avalanche.runtime import S3File

    files = {}
    for value in values:
        field, uri = _parse_assignment(value, "--s3-file")
        files[field] = S3File(uri=uri)
    return files


def _wait_for_provider(provider, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if provider.ping():
            return
        time.sleep(0.1)
    raise RuntimeError(
        "Timed out waiting for Avalanche operator readiness. "
        f"Last error: {getattr(provider, 'last_error', '')}"
    )


def _launch_connected_tui(address: str) -> None:
    _launch_tui(["--connect", address])


def _stop_operator_process(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _raise_optional_extra_error(
    exc: ModuleNotFoundError,
    component: str,
    extra: str,
    optional_names: set[str],
) -> None:
    if exc.name in optional_names:
        raise ModuleNotFoundError(
            f"avalanche.{component} is optional. Install it with `avalanche-ai[{extra}]` "
            f"or run `uv sync --extra {extra}`.",
            name=exc.name,
        ) from exc
    raise exc
