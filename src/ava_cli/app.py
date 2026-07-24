"""Thin `ava` command layer over Avalanche implementation packages."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

_RUNTIME_OPTIONAL_MODULES = {"runtime", "grpc", "watchfiles", "croniter"}
_TUI_OPTIONAL_MODULES = {"tui", "textual", "grpc"}
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_STAGED_OUTPUT_NAME = "result"
_MAX_STAGED_OUTPUT_ENTRIES = 2048
_MAX_STAGED_OUTPUT_DEPTH = 8
_MAX_PARENT_IDENTITY_SCAN = 4096


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
    operator.add_argument("--ray", action="store_true", help="use the Ray executor")
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
    runtime_args = [
        "--flows",
        *args.flows,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--webhook-port",
        str(args.webhook_port),
    ]
    if args.ray:
        runtime_args.append("--ray")
    return _operator_main(runtime_args)


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
        try:
            run_id = provider.start_run(
                args.flow,
                input=input_payload,
                context=context_payload,
                files=file_payloads,
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
    from runtime.operator.results import encode_workflow_result

    _reject_repeated_result_files(value, File)
    encode_workflow_result(value)
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
            {metadata_name, *(item["path"] for item in files)},
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
            if entry.is_dir(follow_symlinks=False):
                raise ValueError("Result output staging directory contains a directory")
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
