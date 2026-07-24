"""Isolated workflow discovery worker and root identity normalization."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from croniter import croniter

from avalanche.dag import Workflow

from .models import (
    WorkflowDescriptor,
    WorkflowDiscoveryDiagnostic,
    WorkflowLocator,
    display_name_from_id,
)
from .windows_job import WindowsJob, assign_process, close_job, create_kill_on_close_job


@dataclass(frozen=True)
class ConfiguredRoot:
    alias: str
    path: Path
    target: Path


DEFAULT_DISCOVERY_TIMEOUT = 15.0
_DISCOVERY_TERMINATE_GRACE = 1.0


def configure_roots(paths: list[str]) -> tuple[ConfiguredRoot, ...]:
    """Normalize configured file/directory inputs and deterministic aliases."""
    pending: list[tuple[str | None, Path, Path]] = []
    for value in paths:
        explicit_alias, raw_path = _split_alias(value)
        target = Path(raw_path).expanduser().resolve()
        root = target if target.is_dir() else target.parent
        pending.append((explicit_alias, root, target))

    targets = [target for _, _, target in pending]
    if len(set(targets)) != len(targets):
        raise ValueError("Repeated configured workflow target")

    base_aliases = [alias or root.name or "root" for alias, root, _ in pending]
    counts = {name: base_aliases.count(name) for name in set(base_aliases)}
    roots: list[ConfiguredRoot] = []
    for (explicit_alias, root, target), base_alias in zip(pending, base_aliases, strict=True):
        if counts[base_alias] > 1:
            if explicit_alias is None:
                raise ValueError(
                    f"Configured root basename {base_alias!r} is ambiguous; "
                    "provide explicit stable aliases"
                )
            raise ValueError(f"Duplicate configured root alias: {base_alias}")
        roots.append(ConfiguredRoot(alias=base_alias, path=root, target=target))
    return tuple(roots)


def discover(
    roots: tuple[ConfiguredRoot, ...], *, timeout: float = DEFAULT_DISCOVERY_TIMEOUT
) -> tuple[tuple[WorkflowDescriptor, ...], tuple[WorkflowDiscoveryDiagnostic, ...]]:
    """Discover in a short-lived interpreter and return value-only results."""
    payload = {
        "roots": [
            {"alias": root.alias, "path": str(root.path), "target": str(root.target)}
            for root in roots
        ]
    }
    if timeout <= 0:
        raise ValueError("Discovery timeout must be positive")
    with tempfile.TemporaryDirectory(prefix="avalanche-discovery-") as temp_dir:
        temp = Path(temp_dir)
        payload_path = temp / "request.json"
        result_path = temp / "result.json"
        assignment_path = temp / "assigned"
        stdout_path = temp / "stdout.log"
        stderr_path = temp / "stderr.log"
        payload_path.write_text(json.dumps(payload))
        with stdout_path.open("w+") as stdout, stderr_path.open("w+") as stderr:
            windows_job = create_kill_on_close_job()
            process: subprocess.Popen[str] | None = None
            terminated = False
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "runtime.operator.discovery",
                        "--worker",
                        str(payload_path),
                        str(result_path),
                        str(assignment_path),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    start_new_session=os.name != "nt",
                )
                assign_process(windows_job, process.pid)
                assignment_path.touch()
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    return (), (_discovery_failure(f"Discovery exceeded {timeout:.1f}s"),)
                finally:
                    _terminate_discovery_process(process, windows_job)
                    terminated = True
            except BaseException:
                if process is not None and not terminated:
                    _terminate_discovery_process(process, windows_job)
                else:
                    close_job(windows_job)
                raise
            stdout.seek(0)
            stderr.seek(0)
            captured = _bounded_diagnostic(stderr.read() or stdout.read())

        if process.returncode != 0:
            return (), (_discovery_failure(captured or "Discovery worker failed"),)
        try:
            result = json.loads(result_path.read_text())
            descriptors = tuple(_descriptor_from_dict(item) for item in result["descriptors"])
            diagnostics = tuple(
                WorkflowDiscoveryDiagnostic(**item) for item in result["diagnostics"]
            )
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return (), (
                _discovery_failure(f"Invalid discovery result: {_format_exception(exc)}"),
            )
        return descriptors, diagnostics


def _terminate_discovery_process(
    process: subprocess.Popen[str], windows_job: WindowsJob | None
) -> None:
    if process.pid is None:
        close_job(windows_job)
        return
    if os.name != "nt":
        group_signalled = _signal_process_group(process.pid, 15)
        if not group_signalled and process.poll() is None:
            process.terminate()
        deadline = time.monotonic() + _DISCOVERY_TERMINATE_GRACE
        while time.monotonic() < deadline and _process_group_exists(process.pid):
            time.sleep(0.02)
        if _process_group_exists(process.pid):
            _signal_process_group(process.pid, 9)
        elif process.poll() is None:
            process.kill()
    else:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_DISCOVERY_TERMINATE_GRACE)
            except subprocess.TimeoutExpired:
                process.kill()
    try:
        process.wait(timeout=_DISCOVERY_TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        pass
    close_job(windows_job)


def _signal_process_group(process_group: int, signal_number: int) -> bool:
    try:
        os.killpg(process_group, signal_number)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def _bounded_diagnostic(value: str, limit: int = 4000) -> str:
    """Bound worker diagnostics; source file contents are never included."""
    return value.strip()[-limit:]


def _discovery_failure(message: str) -> WorkflowDiscoveryDiagnostic:
    return WorkflowDiscoveryDiagnostic(path="<discovery>", kind="import_error", message=message)


def load_builder(root: ConfiguredRoot, locator: WorkflowLocator):
    """Slice-1 compatibility seam: freshly import a scanned builder at call time."""
    file_path = (root.path / locator.relative_file).resolve()
    if not file_path.is_relative_to(root.path) or not file_path.is_file():
        raise KeyError(f"Workflow source is unavailable: {locator.relative_file}")
    _purge_modules_under(root.path)
    module = _import_file(file_path)
    builder = getattr(module, locator.builder_symbol, None)
    if (
        not callable(builder)
        or not getattr(builder, "__avalanche_workflow__", False)
        or builder.__module__ != module.__name__
    ):
        raise KeyError(f"Workflow builder is unavailable: {locator.builder_symbol}")
    return builder


def _split_alias(value: str) -> tuple[str | None, str]:
    if "=" not in value:
        return None, value
    alias, path = value.split("=", 1)
    if alias and path:
        return alias, path
    return None, value


def _iter_files(root: ConfiguredRoot) -> list[Path]:
    if root.target.is_file():
        return [root.target] if root.target.suffix == ".py" else []
    if not root.target.is_dir():
        return []
    return [path for path in sorted(root.target.rglob("*.py")) if not path.name.startswith("_")]


def _package_module_name(file_path: Path) -> tuple[str, Path] | None:
    parts = [file_path.stem]
    parent = file_path.parent
    while (parent / "__init__.py").exists():
        parts.append(parent.name)
        parent = parent.parent
    if len(parts) == 1:
        return None
    return ".".join(reversed(parts)), parent


def _import_file(file_path: Path):
    package_info = _package_module_name(file_path)
    if package_info is not None:
        module_name, import_root = package_info
        sys.path.insert(0, str(import_root))
        importlib.invalidate_caches()
        top_level = module_name.split(".", 1)[0]
        for loaded_name in tuple(sys.modules):
            if loaded_name == top_level or loaded_name.startswith(f"{top_level}."):
                sys.modules.pop(loaded_name, None)
        return importlib.import_module(module_name)

    parent = str(file_path.parent)
    sys.path.insert(0, parent)
    digest = hashlib.sha256(str(file_path).encode()).hexdigest()[:12]
    module_name = f"_avalanche_discovered_{digest}_{file_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError("Python import machinery could not load this file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _purge_modules_under(root: Path) -> None:
    for module_name, module in tuple(sys.modules.items()):
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            continue
        try:
            if Path(module_file).resolve().is_relative_to(root):
                sys.modules.pop(module_name, None)
        except (OSError, RuntimeError):
            continue


def _worker(payload: dict[str, Any]) -> dict[str, Any]:
    roots = tuple(
        ConfiguredRoot(
            alias=item["alias"], path=Path(item["path"]), target=Path(item["target"])
        )
        for item in payload["roots"]
    )
    multiple_roots = len(roots) > 1
    descriptors: list[dict[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    baseline_sys_path = tuple(sys.path)

    for root in roots:
        for file_path in _iter_files(root):
            _reset_discovery_import_state(roots, baseline_sys_path)
            resolved_file = file_path.resolve()
            try:
                relative_file = resolved_file.relative_to(root.path).as_posix()
            except ValueError:
                diagnostics.append(
                    _diagnostic(resolved_file, "import_error", "Path escapes root")
                )
                continue
            try:
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    module = _import_file(resolved_file)
            except Exception as exc:
                diagnostics.append(
                    _diagnostic(resolved_file, "import_error", _format_exception(exc))
                )
                continue

            found = False
            for symbol, builder in sorted(vars(module).items()):
                if symbol.startswith("_") or not callable(builder):
                    continue
                if not getattr(builder, "__avalanche_workflow__", False):
                    continue
                if getattr(builder, "__module__", None) != module.__name__:
                    continue
                found = True
                try:
                    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                        workflow = builder()
                    if not isinstance(workflow, Workflow):
                        raise TypeError("Marked builder did not return a Workflow")
                except Exception as exc:
                    diagnostics.append(
                        _diagnostic(resolved_file, "build_error", _format_exception(exc))
                    )
                    continue
                if workflow.cron is not None:
                    try:
                        croniter(workflow.cron)
                    except Exception as exc:
                        diagnostics.append(
                            _diagnostic(
                                resolved_file, "invalid_schedule", _format_exception(exc)
                            )
                        )
                        continue
                workflow_id = f"{relative_file}::{symbol}"
                if multiple_roots:
                    workflow_id = f"{root.alias}/{workflow_id}"
                descriptors.append(
                    _descriptor_to_dict(
                        workflow_id, root.alias, relative_file, symbol, workflow
                    )
                )
            if not found:
                diagnostics.append(
                    _diagnostic(
                        resolved_file,
                        "skipped",
                        "No workflows discovered in this file.",
                    )
                )
    return {"descriptors": descriptors, "diagnostics": diagnostics}


def _reset_discovery_import_state(
    roots: tuple[ConfiguredRoot, ...], baseline_sys_path: tuple[str, ...]
) -> None:
    """Start every file from the same path and configured-root module state."""
    sys.path[:] = baseline_sys_path
    for root in roots:
        _purge_modules_under(root.path.resolve())
    importlib.invalidate_caches()


def _descriptor_to_dict(
    workflow_id: str,
    root_alias: str,
    relative_file: str,
    symbol: str,
    workflow: Workflow,
) -> dict[str, Any]:
    node_ids = workflow._topological_sort()
    agent_node_ids = []
    agent_metadata_json = []
    for node_id in node_ids:
        spec = getattr(workflow.nodes[node_id].node.fn, "__agent_step__", None)
        if spec is None:
            continue
        agent_node_ids.append(node_id)
        try:
            metadata = spec.declaration_metadata(workflow.agent_defaults)
            agent_metadata_json.append(
                [
                    node_id,
                    json.dumps(
                        metadata,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ]
            )
        except Exception:
            continue
    return {
        "workflow_id": workflow_id,
        "display_name": workflow.name,
        "locator": {
            "root_alias": root_alias,
            "relative_file": relative_file,
            "builder_symbol": symbol,
        },
        "node_ids": node_ids,
        "graph": [[key, value] for key, value in workflow.graph.items()],
        "node_types": [[nid, workflow.nodes[nid].node.node_type.value] for nid in node_ids],
        "display_names": [[nid, display_name_from_id(nid)] for nid in node_ids],
        "agent_node_ids": agent_node_ids,
        "agent_metadata_json": agent_metadata_json,
        "cron": workflow.cron,
        "webhook_path": workflow.webhook.path if workflow.webhook else None,
        "webhook_enabled": workflow.webhook is not None,
    }


def _descriptor_from_dict(item: dict[str, Any]) -> WorkflowDescriptor:
    return WorkflowDescriptor(
        workflow_id=item["workflow_id"],
        display_name=item["display_name"],
        locator=WorkflowLocator(**item["locator"]),
        node_ids=tuple(item["node_ids"]),
        graph=tuple((key, tuple(value)) for key, value in item["graph"]),
        node_types=tuple((key, value) for key, value in item["node_types"]),
        display_names=tuple((key, value) for key, value in item["display_names"]),
        agent_node_ids=tuple(item.get("agent_node_ids", ())),
        agent_metadata_json=tuple(
            (key, value) for key, value in item.get("agent_metadata_json", ())
        ),
        cron=item["cron"],
        webhook_path=item.get("webhook_path"),
        webhook_enabled=item.get("webhook_enabled", False),
    )


def _diagnostic(path: Path, kind: str, message: str) -> dict[str, str]:
    return {"path": str(path), "kind": kind, "message": message}


def _format_exception(exc: Exception) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


if __name__ == "__main__" and len(sys.argv) == 5 and sys.argv[1] == "--worker":
    os.environ.pop("PYTHONINSPECT", None)
    request_path = Path(sys.argv[2])
    result_path = Path(sys.argv[3])
    assignment_path = Path(sys.argv[4])
    assignment_deadline = time.monotonic() + 30.0
    while not assignment_path.exists():
        if time.monotonic() >= assignment_deadline:
            raise TimeoutError("Discovery process ownership was not confirmed")
        time.sleep(0.01)
    result = _worker(json.loads(request_path.read_text()))
    temporary_result = result_path.with_suffix(".tmp")
    temporary_result.write_text(json.dumps(result))
    temporary_result.replace(result_path)
