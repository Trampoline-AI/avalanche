"""Thin `ava` command layer over Avalanche implementation packages."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import logging
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence
from importlib.resources import as_file, files
from pathlib import Path, PurePosixPath
from uuid import uuid4

from runtime.operator.discovery import (
    DEFAULT_DISCOVERY_TIMEOUT,
    WorkflowDiscoveryError,
    validate_discovery_timeout,
)
from runtime.operator.workspace_config import format_scan_targets, select_workflow_targets

_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_STAGED_OUTPUT_NAME = "result"
_MAX_STAGED_OUTPUT_ENTRIES = 2048
_MAX_STAGED_OUTPUT_DEPTH = 8
_MAX_PARENT_IDENTITY_SCAN = 4096

_OPERATOR_READY_TIMEOUT_SECONDS = 5.0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "dev" and args.port == args.web_port:
        parser.error("ava dev --port and --web-port must differ")
    if args.command in {"dev", "operator"}:
        try:
            validate_discovery_timeout(args.discovery_timeout)
            if args.command == "dev":
                selection = select_workflow_targets(args.flows)
                args.flows = list(selection.paths)
                args.flow_target_selection = selection
        except ValueError as exc:
            parser.error(str(exc))
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

    init = subcommands.add_parser(
        "init",
        help="create a verified Avalanche demo workspace in the current empty directory",
    )
    init.add_argument(
        "--editable-deps",
        action="store_true",
        help="clone Avalanche and PredictRLM as editable dependencies",
    )
    init.set_defaults(handler=_run_init)
    operator = subcommands.add_parser(
        "operator",
        help="start the local flow operator",
        description="Start the local Avalanche operator and discover flows from files.",
        allow_abbrev=False,
    )
    operator.add_argument(
        "flows",
        nargs="*",
        metavar="FLOW",
        help="workflow Python file or directory; uses workspace configuration when omitted",
    )
    operator.add_argument("--port", type=int, default=7433, help="operator gRPC port")
    operator.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "listen host (default: loopback); non-loopback exposure requires an "
            "external trusted and authenticated boundary"
        ),
    )
    operator.add_argument(
        "--webhook-port", type=int, default=7434, help="loopback webhook HTTP port"
    )
    operator.add_argument(
        "--log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="WARNING",
        help="terminal log level (default: WARNING)",
    )
    operator.add_argument("--ray", action="store_true", help="use the Ray executor")
    operator.add_argument(
        "--discovery-timeout",
        type=float,
        default=DEFAULT_DISCOVERY_TIMEOUT,
        metavar="SECONDS",
        help=f"maximum seconds for one discovery scan (default: {DEFAULT_DISCOVERY_TIMEOUT:g})",
    )
    operator.set_defaults(handler=_run_operator)

    webhooks = subcommands.add_parser("webhooks", help="inspect local webhook routes")
    webhook_commands = webhooks.add_subparsers(dest="webhook_command", metavar="COMMAND")
    for name, handler in (("list", _list_webhooks), ("get", _get_webhook)):
        command = webhook_commands.add_parser(name, help=f"{name} local webhook routes")
        if name == "get":
            command.add_argument("selector", help="canonical workflow selector")
        command.add_argument("--connect", default="localhost:7433", metavar="HOST:PORT")
        command.set_defaults(handler=handler)

    run = subcommands.add_parser(
        "run",
        help="start a flow run on a local operator",
        description=(
            "Start a flow run through a running Avalanche operator and print its run ID. "
            "Use `ava result RUN_ID --output-dir PATH` to download its terminal result."
        ),
        epilog=(
            "Results can also be retrieved from Python with "
            "GrpcStateProvider.get_run_result(run_id)."
        ),
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
        "--workspace",
        action="append",
        default=[],
        metavar="FIELD=DIR",
        help="capture a local directory as a top-level Workspace input field",
    )
    run.set_defaults(handler=_run_flow)

    result = subcommands.add_parser(
        "result",
        help="download a successful run result",
        description=(
            "Retrieve a successful run result into a new destination directory. "
            "The CLI builds and verifies a private staged tree, publishes its name "
            "with an atomic no-replace rename, immediately verifies the destination "
            "identity, and emits JSON metadata without printing binary content."
        ),
        epilog=(
            "Security contract: the output parent is a caller-owned local namespace. "
            "Descriptor-authenticated catchable state is cleaned. An interruption "
            "after the holding mkdir side effect but before descriptor acquisition "
            "can leave a private empty holding residue because safe cleanup cannot "
            "distinguish a same-name replacement; the requested destination remains "
            "absent. "
            "Concurrent hostile mutation by another process running as the same user "
            "is outside this CLI threat model."
        ),
    )
    result.add_argument("run_id", help="run ID to retrieve")
    result.add_argument(
        "--connect",
        default="localhost:7433",
        metavar="HOST:PORT",
        help="operator address",
    )
    result.add_argument(
        "--output-dir",
        required=True,
        metavar="PATH",
        help=(
            "new destination directory for result metadata and downloaded files "
            "(must not already exist)"
        ),
    )
    result.add_argument(
        "--wait",
        action="store_true",
        help="wait for a nonterminal run before retrieving its result",
    )
    result.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="maximum seconds to wait with --wait (default: 300)",
    )
    result.set_defaults(handler=_run_result)

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

    web = subcommands.add_parser(
        "web",
        help="launch the local browser UI",
        description="Launch the browser UI and proxy it to an Avalanche operator.",
    )
    web.add_argument(
        "--connect",
        default="localhost:7433",
        metavar="HOST:PORT",
        help="operator address",
    )
    web.add_argument("--host", default="127.0.0.1", help="browser UI listen host")
    web.add_argument("--port", type=int, default=7435, help="browser UI HTTP port")
    web.add_argument(
        "--trusted-proxy",
        action="store_true",
        help="confirm non-loopback browser traffic is protected by a trusted proxy",
    )
    web.set_defaults(handler=_run_web)

    dev = subcommands.add_parser(
        "dev",
        help="start a local operator",
        description="Start a local operator and wait until it stops.",
    )
    dev.add_argument(
        "flows",
        nargs="*",
        metavar="FLOW",
        help="workflow Python file or directory; uses workspace configuration when omitted",
    )
    dev.add_argument("--port", type=int, default=7433, help="operator gRPC port")
    dev.add_argument("--web-port", type=int, default=7435, help="browser UI HTTP port")
    dev.add_argument("--ray", action="store_true", help="use the Ray executor")
    dev.add_argument(
        "--log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="WARNING",
        help="terminal log level (default: WARNING)",
    )
    dev.add_argument(
        "--discovery-timeout",
        type=float,
        default=DEFAULT_DISCOVERY_TIMEOUT,
        metavar="SECONDS",
        help=f"maximum seconds for one discovery scan (default: {DEFAULT_DISCOVERY_TIMEOUT:g})",
    )
    dev.set_defaults(handler=_run_dev)

    return parser


def _run_init(args: argparse.Namespace) -> int:
    script = files("ava_cli").joinpath("init.sh")
    if not script.is_file():
        script = Path(__file__).parents[2] / "init.sh"
    command = ["bash", str(script)]
    if args.editable_deps:
        command.append("--editable-deps")
    with as_file(script) as script_path:
        command[1] = str(script_path)
        return subprocess.run(command, check=False).returncode


def _run_operator(args: argparse.Namespace) -> int:
    runtime_args = [
        *args.flows,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--webhook-port",
        str(args.webhook_port),
        "--log-level",
        args.log_level,
        "--discovery-timeout",
        str(args.discovery_timeout),
    ]
    if args.ray:
        runtime_args.append("--ray")
    return _operator_main(runtime_args)


def _run_web(args: argparse.Namespace) -> int:
    from runtime.operator.web import start_browser_server

    server = start_browser_server(
        args.connect,
        host=args.host,
        port=args.port,
        trust_non_loopback=args.trusted_proxy,
    )
    print(f"Avalanche web UI: {server.endpoint}")
    try:
        _open_browser(server.endpoint)
        server.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        server.close()
    return 0


def _webhook_record(workflow) -> dict[str, object]:
    return {
        "selector": workflow.selector,
        "method": "POST",
        "path": workflow.webhook_path,
        "url": workflow.webhook_url or workflow.webhook_path,
        "active": workflow.webhook_active,
    }


def _list_webhooks(args: argparse.Namespace) -> int:
    provider = _make_provider(args.connect)
    try:
        records = [
            _webhook_record(item) for item in provider.list_workflows() if item.webhook_path
        ]
        print(json.dumps(records, sort_keys=True))
        return 0
    finally:
        provider.close()


def _get_webhook(args: argparse.Namespace) -> int:
    provider = _make_provider(args.connect)
    try:
        matches = [
            item
            for item in provider.list_workflows()
            if item.webhook_path and item.selector == args.selector
        ]
        if not matches:
            print(f"Webhook not found: {args.selector}", file=sys.stderr)
            return 1
        print(json.dumps(_webhook_record(matches[0]), sort_keys=True))
        return 0
    finally:
        provider.close()


def _operator_main(argv: list[str]) -> int:
    from runtime.operator.__main__ import main as operator_main

    return operator_main(argv)


def _run_flow(args: argparse.Namespace) -> int:
    provider = _make_provider(args.connect)
    from runtime.operator.client import OperatorCallError

    try:
        try:
            input_payload = _parse_json_object(args.input_json, "--input")
            context_payload = _parse_json_object(args.context_json, "--context")
            file_payloads = _parse_file_inputs(args.file)
            workspace_payloads = _parse_workspace_inputs(args.workspace)
            duplicate_fields = set(file_payloads).intersection(workspace_payloads)
            if duplicate_fields:
                raise ValueError(f"Duplicate input field '{sorted(duplicate_fields)[0]}'")
            if input_payload is not None:
                duplicate_fields = set(input_payload).intersection(
                    file_payloads | workspace_payloads
                )
                if duplicate_fields:
                    raise ValueError(f"Duplicate input field '{sorted(duplicate_fields)[0]}'")
            input_payload = {**(input_payload or {}), **workspace_payloads} or None
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        try:
            run_id = provider.start_run(
                args.flow,
                input=input_payload,
                context=context_payload,
                files=file_payloads,
            )
        except (OperatorCallError, ValueError) as exc:
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


def _run_result(args: argparse.Namespace) -> int:
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("--timeout must be positive and finite", file=sys.stderr)
        return 2
    provider = _make_provider(args.connect)
    try:
        if args.wait and not _wait_for_terminal_run(
            provider,
            args.run_id,
            timeout=args.timeout,
        ):
            if last_error := getattr(provider, "last_error", ""):
                print(last_error, file=sys.stderr)
            return 1
        try:
            value = provider.get_run_result(args.run_id)
        except Exception as exc:
            error = getattr(provider, "last_error", "") or str(exc)
            print(error, file=sys.stderr)
            return 1
        try:
            metadata = _materialize_result(
                args.run_id,
                value,
                Path(args.output_dir),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(metadata, allow_nan=False, indent=2, sort_keys=True))
        return 0
    finally:
        provider.close()


def _wait_for_terminal_run(provider, run_id: str, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        run = provider.get_run(run_id)
        if run is None:
            if not getattr(provider, "last_error", ""):
                provider.last_error = f"Run {run_id} not found"
            return False
        status = getattr(run.status, "value", run.status)
        if status in {"success", "failed", "cancelled"}:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            provider.last_error = f"Timed out waiting for run {run_id}"
            return False
        time.sleep(min(0.1, remaining))


def _materialize_result(run_id: str, value, output_directory: Path) -> dict:
    from avalanche.runtime import File
    from avalanche.workspace import Workspace
    from runtime.operator.results import encode_workflow_result

    _reject_repeated_result_files(value, File)
    encode_workflow_result(value)
    _preflight_result_materialization(value, File, Workspace)
    _require_anchored_output_io()
    parent_directory = output_directory.parent
    destination_name = output_directory.name
    if not destination_name or destination_name in {".", ".."}:
        raise ValueError("Result output directory must have a destination name")
    parent_fd, parent_identity = _open_output_parent(parent_directory)
    holding_name = f".avalanche-result-{uuid4().hex}.tmp"
    holding_fd = None
    holding_identity = None
    staging_fd = None
    staging_identity = None
    publication_attempted = False
    publication_returned = False
    files: list[dict] = []
    workspaces: list[dict] = []
    workspace_identities: dict[str, tuple[int, int]] = {}

    def materialize(item):
        if isinstance(item, File):
            index = len(files) + 1
            digest = hashlib.sha256(item.content).hexdigest()
            if item.sha256 != digest:
                raise ValueError("Result file digest does not match its content")
            filename = _generated_result_filename(index, item.name)
            _write_exclusive_file(
                filename,
                item.content,
                staging_fd,
            )
            metadata = {
                "path": filename,
                "name": item.name,
                "media_type": item.content_type,
                "sha256": digest,
                "size": len(item.content),
            }
            files.append(metadata)
            return {"file": metadata}
        if isinstance(item, Workspace):
            index = len(workspaces) + 1
            root_name = f"workspace-{index:04d}-{uuid4().hex}"
            manifest, root_identity = _materialize_workspace_tree(
                item,
                root_name,
                staging_fd,
            )
            workspace_identities[root_name] = root_identity
            metadata = {
                "path": root_name,
                "entries": len(item.entries),
                "sha256": hashlib.sha256(
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            workspaces.append(metadata)
            return {"workspace": metadata}
        if type(item) is tuple:
            return [materialize(child) for child in item]
        if type(item) is list:
            return [materialize(child) for child in item]
        if type(item) is dict:
            return {key: materialize(child) for key, child in item.items()}
        if item is None or type(item) in {bool, int, float, str}:
            if type(item) is float and not math.isfinite(item):
                raise ValueError("Result metadata contains a non-finite number")
            return item
        raise TypeError(f"Unsupported result value {type(item).__name__}")

    try:
        _verify_output_parent(
            parent_directory,
            parent_fd,
            parent_identity,
        )
        _require_absent_destination(parent_fd, destination_name)
        os.mkdir(holding_name, mode=0o700, dir_fd=parent_fd)
        holding_fd, holding_identity = _open_private_directory(
            parent_fd,
            holding_name,
            label="holding",
        )
        os.mkdir(_STAGED_OUTPUT_NAME, mode=0o700, dir_fd=holding_fd)
        staging_fd, staging_identity = _open_private_directory(
            holding_fd,
            _STAGED_OUTPUT_NAME,
            label="staging",
        )
        document = {
            "run_id": run_id,
            "result": materialize(value),
            "files": files,
        }
        if workspaces:
            document["workspaces"] = workspaces
        metadata_name = f"result-{uuid4().hex}.json"
        metadata_bytes = (
            json.dumps(document, allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        _write_exclusive_file(
            metadata_name,
            metadata_bytes,
            staging_fd,
        )
        for item in files:
            digest, size = _hash_output_file(
                item["path"],
                staging_fd,
                maximum_bytes=item["size"],
            )
            if digest != item["sha256"] or size != item["size"]:
                raise ValueError("Downloaded result file failed digest verification")
        if (
            _read_output_file(
                metadata_name,
                staging_fd,
                maximum_bytes=len(metadata_bytes),
            )
            != metadata_bytes
        ):
            raise ValueError("Downloaded result metadata failed verification")
        _validate_staged_entries(
            staging_fd,
            {
                metadata_name,
                *(item["path"] for item in files),
                *(item["path"] for item in workspaces),
            },
        )
        for root_name, root_identity in workspace_identities.items():
            _validate_directory_entry_identity(
                staging_fd,
                root_name,
                root_identity,
                label="workspace",
            )
        os.fsync(staging_fd)
        _verify_output_parent(
            parent_directory,
            parent_fd,
            parent_identity,
        )
        _require_absent_destination(parent_fd, destination_name)
        _validate_directory_entry_identity(
            holding_fd,
            _STAGED_OUTPUT_NAME,
            staging_identity,
            label="staged output",
        )
        # POSIX and macOS have no portable rename operation conditioned on the
        # inode retained by staging_fd. Under the caller-owned namespace contract,
        # rename the staged name and immediately verify the destination identity.
        publication_attempted = True
        _rename_directory_noreplace(
            _STAGED_OUTPUT_NAME,
            destination_name,
            holding_fd,
            parent_fd,
        )
        publication_returned = True
        published_identity = _directory_entry_identity(parent_fd, destination_name)
        if published_identity != staging_identity:
            raise ValueError("Result output published output identity changed")
        os.fsync(parent_fd)
        _remove_directory_entry_by_identity(
            parent_fd,
            holding_name,
            holding_identity,
            maximum_entries=_MAX_PARENT_IDENTITY_SCAN,
        )
    except BaseException:
        if holding_fd is not None and holding_identity is not None:
            _cleanup_staged_output(
                parent_fd,
                destination_name,
                holding_name,
                holding_fd,
                holding_identity,
                _STAGED_OUTPUT_NAME,
                staging_fd,
                staging_identity,
                publication_attempted=publication_attempted,
                publication_returned=publication_returned,
            )
        raise
    finally:
        if staging_fd is not None:
            os.close(staging_fd)
        if holding_fd is not None:
            os.close(holding_fd)
        os.close(parent_fd)
    return {**document, "metadata_path": metadata_name}


def _reject_repeated_result_files(value, file_type: type) -> None:
    """Preflight the CLI tree so one in-memory blob is never written repeatedly."""
    seen: set[int] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, file_type):
            identity = id(item)
            if identity in seen:
                raise ValueError("Result repeats the same file attachment")
            seen.add(identity)
        elif type(item) in {tuple, list}:
            stack.extend(item)
        elif type(item) is dict:
            stack.extend(item.values())


def _preflight_result_materialization(
    value,
    file_type: type,
    workspace_type: type,
) -> None:
    """Reject result trees that cannot be deterministically cleaned."""
    staged_entries = 1  # The metadata document.
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, file_type):
            staged_entries += 1
        elif isinstance(item, workspace_type):
            manifest = item.manifest()
            validated = workspace_type.from_manifest(manifest)
            staged_entries += 1 + len(validated.entries)
            for entry in validated.entries:
                path_depth = len(PurePosixPath(entry.path).parts)
                cleanup_depth = path_depth + (entry.kind == "directory")
                if cleanup_depth > _MAX_STAGED_OUTPUT_DEPTH:
                    raise ValueError(
                        "Workspace exceeds the CLI result materialization depth limit"
                    )
        elif type(item) in {tuple, list}:
            stack.extend(item)
        elif type(item) is dict:
            stack.extend(item.values())
        elif item is None or type(item) in {bool, int, float, str}:
            if type(item) is float and not math.isfinite(item):
                raise ValueError("Result metadata contains a non-finite number")
        else:
            raise TypeError(f"Unsupported result value {type(item).__name__}")
        if staged_entries > _MAX_STAGED_OUTPUT_ENTRIES:
            raise ValueError("Result exceeds the CLI materialization cleanup entry limit")


def _generated_result_filename(index: int, original_name: str | None) -> str:
    hint = re.split(r"[/\\]", original_name or "")[-1]
    hint = re.sub(r"[^A-Za-z0-9._-]+", "_", hint).strip("._-")[:64]
    suffix = f"-{hint}" if hint else ""
    return f"attachment-{index:04d}-{uuid4().hex}{suffix}"


def _require_anchored_output_io() -> None:
    if not all(
        function in os.supports_dir_fd
        for function in (
            os.open,
            os.mkdir,
            os.unlink,
            os.rmdir,
            os.rename,
            os.stat,
        )
    ):
        raise RuntimeError(
            "Secure result materialization is unavailable: this platform does not "
            "support directory-anchored file operations"
        )
    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        raise RuntimeError(
            "Secure result materialization is unavailable: this platform cannot "
            "open a directory without following symbolic links"
        )
    _atomic_noreplace_rename()


def _open_output_parent(parent_directory: Path) -> tuple[int, tuple[int, int]]:
    before = parent_directory.stat(follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError("Result output parent must be a directory")
    identity = (before.st_dev, before.st_ino)
    descriptor = os.open(
        parent_directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != identity:
        os.close(descriptor)
        raise ValueError("Result output parent changed while opening")
    return descriptor, identity


def _verify_output_parent(
    parent_directory: Path,
    parent_fd: int,
    identity: tuple[int, int],
) -> None:
    try:
        metadata = parent_directory.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ValueError("Result output parent was removed during materialization") from exc
    if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != identity:
        raise ValueError("Result output parent changed during materialization")
    opened = os.fstat(parent_fd)
    if (opened.st_dev, opened.st_ino) != identity:
        raise ValueError("Result output parent descriptor changed")


def _require_absent_destination(parent_fd: int, destination_name: str) -> None:
    try:
        os.stat(
            destination_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    raise FileExistsError(
        f"Result output destination {destination_name!r} must not already exist"
    )


def _rename_directory_noreplace(
    source_name: str,
    destination_name: str,
    source_directory_fd: int,
    destination_directory_fd: int,
) -> None:
    rename, flag = _atomic_noreplace_rename()
    result = rename(
        source_directory_fd,
        os.fsencode(source_name),
        destination_directory_fd,
        os.fsencode(destination_name),
        flag,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            f"Result output destination {destination_name!r} must not already exist"
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        destination_name,
    )


def _atomic_noreplace_rename():
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(library, "renameatx_np", None)
        flag = _RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = getattr(library, "renameat2", None)
        flag = _RENAME_NOREPLACE
    else:
        rename = None
        flag = 0
    if rename is None:
        raise RuntimeError(
            "Secure result materialization is unavailable: this platform cannot "
            "atomically publish a new directory without replacing an existing path"
        )
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    return rename, flag


def _open_private_directory(
    parent_fd: int,
    name: str,
    *,
    label: str,
) -> tuple[int, tuple[int, int]]:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(descriptor, 0o700)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"Result output {label} entry is not a directory")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError(f"Result output {label} directory is not private")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ValueError(f"Result output {label} directory has a different owner")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, (metadata.st_dev, metadata.st_ino)


def _write_exclusive_file(
    name: str,
    content: bytes,
    directory_fd: int,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset : offset + 1024 * 1024])
            if written <= 0:
                raise OSError("Result materialization made no write progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        _unlink_output_file(name, directory_fd)
        raise
    else:
        os.close(descriptor)


def _materialize_workspace_tree(
    workspace,
    root_name: str,
    staging_fd: int,
) -> tuple[dict, tuple[int, int]]:
    """Write, verify, and recursively sync one validated workspace tree."""
    from avalanche.workspace import Workspace

    manifest = workspace.manifest()
    validated = Workspace.from_manifest(manifest)
    os.mkdir(root_name, mode=0o700, dir_fd=staging_fd)
    root_fd, root_identity = _open_private_directory(
        staging_fd,
        root_name,
        label="workspace",
    )
    directory_identities: dict[str, tuple[int, int]] = {"": root_identity}
    try:
        directories = [entry for entry in validated.entries if entry.kind == "directory"]
        directories.sort(key=lambda entry: (len(PurePosixPath(entry.path).parts), entry.path))
        for entry in directories:
            path = PurePosixPath(entry.path)
            parent_path = "" if str(path.parent) == "." else str(path.parent)
            parent_fd = _open_workspace_directory(
                root_fd,
                parent_path,
                directory_identities,
            )
            try:
                os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
                child_fd, child_identity = _open_private_directory(
                    parent_fd,
                    path.name,
                    label="workspace",
                )
                os.close(child_fd)
                directory_identities[entry.path] = child_identity
            finally:
                os.close(parent_fd)

        files = [entry for entry in validated.entries if entry.kind == "file"]
        for entry in files:
            path = PurePosixPath(entry.path)
            parent_path = "" if str(path.parent) == "." else str(path.parent)
            content = entry.content
            digest = entry.sha256
            if type(content) is not bytes or type(digest) is not str:
                raise ValueError("Malformed workspace file entry")
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError(
                    f"Workspace file {entry.path!r} digest does not match its content"
                )
            parent_fd = _open_workspace_directory(
                root_fd,
                parent_path,
                directory_identities,
            )
            try:
                _write_exclusive_file(path.name, content, parent_fd)
            finally:
                os.close(parent_fd)

        for entry in files:
            path = PurePosixPath(entry.path)
            parent_path = "" if str(path.parent) == "." else str(path.parent)
            content = entry.content
            digest = entry.sha256
            if type(content) is not bytes or type(digest) is not str:
                raise ValueError("Malformed workspace file entry")
            parent_fd = _open_workspace_directory(
                root_fd,
                parent_path,
                directory_identities,
            )
            try:
                actual_digest, actual_size = _hash_output_file(
                    path.name,
                    parent_fd,
                    maximum_bytes=len(content),
                )
            finally:
                os.close(parent_fd)
            if actual_digest != digest or actual_size != len(content):
                raise ValueError(
                    f"Materialized workspace file {entry.path!r} failed digest verification"
                )

        for directory in sorted(
            (path for path in directory_identities if path),
            key=lambda path: len(PurePosixPath(path).parts) if path else 0,
            reverse=True,
        ):
            directory_fd = _open_workspace_directory(
                root_fd,
                directory,
                directory_identities,
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return manifest, root_identity


def _open_workspace_directory(
    root_fd: int,
    relative_path: str,
    identities: dict[str, tuple[int, int]],
) -> int:
    """Open one workspace directory while retaining only its ancestor chain."""
    descriptor = os.dup(root_fd)
    current_path = ""
    try:
        for part in PurePosixPath(relative_path).parts if relative_path else ():
            child_fd, child_identity = _open_private_directory(
                descriptor,
                part,
                label="workspace",
            )
            current_path = f"{current_path}/{part}" if current_path else part
            if child_identity != identities[current_path]:
                os.close(child_fd)
                raise ValueError("Result output workspace identity changed")
            os.close(descriptor)
            descriptor = child_fd
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _hash_output_file(
    name: str,
    directory_fd: int,
    *,
    maximum_bytes: int,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        metadata = os.fstat(descriptor)
        _validate_output_file_metadata(metadata)
        total = 0
        while chunk := os.read(
            descriptor,
            min(1024 * 1024, maximum_bytes - total + 1),
        ):
            total += len(chunk)
            if total > maximum_bytes:
                raise ValueError(f"Downloaded result file exceeds {maximum_bytes} bytes")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), total


def _read_output_file(
    name: str,
    directory_fd: int,
    *,
    maximum_bytes: int,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    chunks = []
    total = 0
    try:
        _validate_output_file_metadata(os.fstat(descriptor))
        while chunk := os.read(
            descriptor,
            min(1024 * 1024, maximum_bytes - total + 1),
        ):
            total += len(chunk)
            if total > maximum_bytes:
                raise ValueError(f"Downloaded result file exceeds {maximum_bytes} bytes")
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _validate_output_file_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("Downloaded result file is not a private regular file")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("Downloaded result file permissions are not private")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError("Downloaded result file has a different owner")


def _unlink_output_file(name: str, directory_fd: int) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _cleanup_staged_output(
    parent_fd: int,
    destination_name: str,
    holding_name: str,
    holding_fd: int,
    holding_identity: tuple[int, int],
    staged_name: str,
    staging_fd: int | None,
    staging_identity: tuple[int, int] | None,
    *,
    publication_attempted: bool,
    publication_returned: bool,
) -> None:
    budget = [_MAX_STAGED_OUTPUT_ENTRIES]
    destination_fd = None
    destination_identity = None
    if publication_attempted:
        try:
            destination_fd = os.open(
                destination_name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            metadata = os.fstat(destination_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("Result output destination is not a directory")
            destination_identity = metadata.st_dev, metadata.st_ino
        except BaseException:
            if destination_fd is not None:
                os.close(destination_fd)
                destination_fd = None

    if staging_fd is not None and staging_identity is not None:
        try:
            _bounded_clear_directory(
                staging_fd,
                budget,
                depth=0,
            )
        except BaseException:
            pass
        try:
            os.fsync(staging_fd)
        except BaseException:
            pass
        try:
            if destination_identity == staging_identity:
                _remove_directory_entry_by_identity(
                    parent_fd,
                    destination_name,
                    staging_identity,
                    maximum_entries=_MAX_PARENT_IDENTITY_SCAN,
                )
            else:
                _remove_directory_entry_by_identity(
                    holding_fd,
                    staged_name,
                    staging_identity,
                    maximum_entries=_MAX_STAGED_OUTPUT_ENTRIES,
                )
        except BaseException:
            if destination_identity == staging_identity:
                try:
                    _remove_directory_entry_by_identity(
                        holding_fd,
                        staged_name,
                        staging_identity,
                        maximum_entries=_MAX_STAGED_OUTPUT_ENTRIES,
                    )
                except BaseException:
                    pass

    if (
        publication_returned
        and destination_fd is not None
        and destination_identity != staging_identity
    ):
        try:
            _bounded_clear_directory(
                destination_fd,
                [_MAX_STAGED_OUTPUT_ENTRIES],
                depth=0,
            )
            os.fsync(destination_fd)
        except BaseException:
            pass
        try:
            _remove_directory_entry_by_identity(
                parent_fd,
                destination_name,
                destination_identity,
                maximum_entries=_MAX_PARENT_IDENTITY_SCAN,
            )
        except BaseException:
            pass
    if destination_fd is not None:
        os.close(destination_fd)

    try:
        _bounded_clear_directory(holding_fd, budget, depth=0)
    except BaseException:
        pass
    try:
        os.fsync(holding_fd)
    except BaseException:
        pass
    try:
        _remove_directory_entry_by_identity(
            parent_fd,
            holding_name,
            holding_identity,
            maximum_entries=_MAX_PARENT_IDENTITY_SCAN,
        )
    except BaseException:
        pass
    try:
        os.fsync(parent_fd)
    except BaseException:
        pass


def _validate_staged_entries(directory_fd: int, expected_names: set[str]) -> None:
    actual_names: set[str] = set()
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            actual_names.add(entry.name)
            if len(actual_names) > _MAX_STAGED_OUTPUT_ENTRIES:
                raise ValueError("Result output staging directory has too many entries")
            if not entry.is_dir(follow_symlinks=False) and not entry.is_file(
                follow_symlinks=False
            ):
                raise ValueError(
                    "Result output staging directory contains an unsupported entry"
                )
    if actual_names != expected_names:
        raise ValueError("Result output staging directory contains unexpected entries")


def _validate_directory_entry_identity(
    directory_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    *,
    label: str,
) -> None:
    if _directory_entry_identity(directory_fd, name) != expected_identity:
        raise ValueError(f"Result output {label} identity changed")


def _directory_entry_identity(directory_fd: int, name: str) -> tuple[int, int]:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Result output entry is not a directory")
        return metadata.st_dev, metadata.st_ino
    finally:
        os.close(descriptor)


def _bounded_clear_directory(
    directory_fd: int,
    remaining_entries: list[int],
    *,
    depth: int,
) -> None:
    if depth > _MAX_STAGED_OUTPUT_DEPTH:
        raise ValueError("Result output cleanup exceeded its directory depth limit")
    with os.scandir(directory_fd) as iterator:
        for entry in iterator:
            remaining_entries[0] -= 1
            if remaining_entries[0] < 0:
                raise ValueError("Result output cleanup exceeded its entry limit")
            metadata = entry.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                os.unlink(entry.name, dir_fd=directory_fd)
                continue
            child_fd = os.open(
                entry.name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    raise ValueError("Result output cleanup directory identity changed")
                _bounded_clear_directory(
                    child_fd,
                    remaining_entries,
                    depth=depth + 1,
                )
            finally:
                os.close(child_fd)
            current = os.stat(
                entry.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (current.st_dev, current.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise ValueError("Result output cleanup directory identity changed")
            os.rmdir(entry.name, dir_fd=directory_fd)


def _remove_directory_entry_by_identity(
    parent_fd: int,
    preferred_name: str,
    expected_identity: tuple[int, int],
    *,
    maximum_entries: int,
) -> None:
    try:
        metadata = os.stat(
            preferred_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        metadata = None
    if (
        metadata is not None
        and (
            metadata.st_dev,
            metadata.st_ino,
        )
        == expected_identity
    ):
        os.rmdir(preferred_name, dir_fd=parent_fd)
        return

    inspected = 0
    with os.scandir(parent_fd) as entries:
        for entry in entries:
            inspected += 1
            if inspected > maximum_entries:
                raise ValueError("Result output cleanup exceeded its parent scan limit")
            metadata = entry.stat(follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                continue
            if (metadata.st_dev, metadata.st_ino) == expected_identity:
                os.rmdir(entry.name, dir_fd=parent_fd)
                return
    raise FileNotFoundError("Result output directory identity is no longer linked")


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
    from tui import launch_tui

    launch_tui(list(argv))


def _run_dev(args: argparse.Namespace) -> int:
    import grpc

    from runtime.operator.operator import Operator
    from runtime.operator.server import serve as serve_operator
    from runtime.operator.web import start_browser_server

    _configure_terminal_logging(args.log_level)
    started = time.monotonic()
    stage = "discovery"
    operator = None
    grpc_server = None
    browser_server = None
    exit_code = 0
    stop_requested = threading.Event()
    previous_handlers = {}

    def request_shutdown(_signum, _frame) -> None:
        stop_requested.set()

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_shutdown)
    try:
        print("Avalanche dev")
        for line in format_scan_targets(args.flow_target_selection):
            print(line)
        print("  Discovering workflows...")
        operator = Operator(
            args.flows,
            discovery_timeout=args.discovery_timeout,
            executor_backend="ray" if args.ray else "local",
        )
        if stop_requested.is_set():
            raise KeyboardInterrupt
        workflow_count = len(operator.get_catalog().workflows)
        print(
            f"  Discovered {workflow_count} workflow"
            f"{'' if workflow_count == 1 else 's'} in {time.monotonic() - started:.2f}s"
        )

        stage = "operator startup"
        grpc_server = serve_operator(operator, port=args.port, block=False)
        operator_address = f"127.0.0.1:{args.port}"
        channel = grpc.insecure_channel(operator_address)
        try:
            grpc.channel_ready_future(channel).result(timeout=_OPERATOR_READY_TIMEOUT_SECONDS)
        finally:
            channel.close()
        print(f"  Operator ready: grpc://{operator_address}")
        if stop_requested.is_set():
            raise KeyboardInterrupt

        stage = "web UI startup"
        browser_server = start_browser_server(operator_address, port=args.web_port)
        print(f"  Web UI ready: {browser_server.endpoint}")
        _open_browser(browser_server.endpoint)
        print("Ready. Press Ctrl-C to stop.")

        stage = "workflow watching"
        while not stop_requested.is_set():
            failure = operator.wait_for_failure(timeout=0.1)
            if failure is not None:
                raise failure
        print("Stopping Avalanche dev...")
    except KeyboardInterrupt:
        print("Stopping Avalanche dev...")
    except WorkflowDiscoveryError as exc:
        _report_dev_failure(stage, exc)
        exit_code = 1
    except Exception as exc:
        _report_dev_failure(stage, exc)
        exit_code = 1
    finally:
        cleanup_error: Exception | None = None
        if browser_server is not None:
            try:
                browser_server.close()
            except Exception as exc:
                cleanup_error = exc
        if grpc_server is not None:
            try:
                grpc_server.stop(grace=1.0).wait(timeout=2.0)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if operator is not None:
            try:
                operator.close()
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            _report_dev_failure("shutdown", cleanup_error)
            exit_code = 1
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    if exit_code == 0:
        print("Stopped.")
    return exit_code


def _configure_terminal_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _open_browser(endpoint: str) -> None:
    try:
        opened = webbrowser.open(endpoint, new=2)
    except webbrowser.Error as exc:
        print(
            f"Could not open a browser automatically ({exc}); open {endpoint} manually.",
            file=sys.stderr,
        )
    else:
        if not opened:
            print(
                f"Could not open a browser automatically; open {endpoint} manually.",
                file=sys.stderr,
            )


def _report_dev_failure(stage: str, error: Exception) -> None:
    print(f"error: Avalanche dev failed during {stage}", file=sys.stderr)
    if isinstance(error, WorkflowDiscoveryError):
        for diagnostic in error.diagnostics:
            print(
                f"  {diagnostic.path}: {diagnostic.kind}: {diagnostic.message}",
                file=sys.stderr,
            )
        return
    print(f"  {type(error).__name__}: {error}", file=sys.stderr)


def _make_provider(address: str):
    from runtime.operator.client import GrpcStateProvider

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
        if field in files:
            raise ValueError(f"Duplicate input field '{field}'")
        files[field] = File.from_path(Path(path))
    return files


def _parse_workspace_inputs(values: list[str]):
    from avalanche.workspace import Workspace

    workspaces = {}
    for value in values:
        field, path = _parse_assignment(value, "--workspace")
        if field in workspaces:
            raise ValueError(f"Duplicate input field '{field}'")
        workspaces[field] = Workspace.from_path(Path(path))
    return workspaces


def _stop_operator_process(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
