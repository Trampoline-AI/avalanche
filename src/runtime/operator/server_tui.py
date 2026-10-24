"""Bounded operator resource pages for the TuiExtensions v1 boundary.

Continuations are server-issued, target-bound and evicted with a fixed count.
Run history reuses the operator's immutable structural baseline, never a live
list sliced by the client. Artifacts remain references until explicit download.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

import grpc
from pydantic import JsonValue

from avalanche.file import File
from avalanche.workspace import Workspace

from .operator import StructuralBaselineUnavailableError
from .proto import operator_pb2 as pb
from .registry import AmbiguousWorkflow

if TYPE_CHECKING:
    from .server_v2 import OperatorV2Servicer


def result_file_references(value_json: str) -> tuple[tuple[str, File | Workspace], ...]:
    """Walk the existing tagged result codec, without materializing file bodies."""
    document = json.loads(value_json)
    pending = [("/value", document["value"])]
    files: list[tuple[str, File | Workspace]] = []
    while pending:
        path, value = pending.pop()
        if isinstance(value, dict):
            if value.get("kind") == "file" and "ref" in value:
                files.append((path, File.model_validate(value)))
            elif value.get("kind") == "workspace":
                # Retain the directory descriptor, never its embedded bodies.
                files.append((path, Workspace.from_manifest(value["manifest"])))
            else:
                pending.extend(
                    (path + "/" + key.replace("~", "~0").replace("/", "~1"), item)
                    for key, item in reversed(tuple(value.items()))
                )
        elif isinstance(value, list):
            pending.extend(
                (path + "/" + str(index), value[index]) for index in reversed(range(len(value)))
            )
    return tuple(files)


@dataclass(frozen=True)
class _Position:
    target: tuple[str, ...]
    revision: str
    offset: int = 0
    run_token: str = ""


class TuiResourceReader:
    def __init__(self, server: OperatorV2Servicer) -> None:
        self._server = server
        self._lock = Lock()
        self._positions: OrderedDict[str, _Position] = OrderedDict()

    def read(self, request: pb.ReadTuiResourceRequestV2, context: grpc.ServicerContext):
        if not 1 <= request.limit <= 500 or not 1 <= request.byte_limit <= 1_048_576:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid resource page bounds")
        if request.run_id and request.workflow_selector:
            snapshot = self._server._op.get_latest_run_snapshot(
                request.run_id, operator_instance_id=self._server._op.operator_instance_id
            )
            if snapshot is None or snapshot.summary.workflow_id != request.workflow_selector:
                context.abort(grpc.StatusCode.NOT_FOUND, "Run does not belong to this workflow")
        target = (
            request.resource,
            request.workflow_selector,
            request.version,
            request.run_id,
            request.node_id,
            request.attempt,
        )
        position = None
        if request.cursor:
            with self._lock:
                position = self._positions.get(request.cursor)
            if position is None or position.target != target:
                context.abort(grpc.StatusCode.OUT_OF_RANGE, "cursor_expired")
        items: list[str] = []
        size = 2  # encoded JSON array delimiters
        truncated = False
        next_position = position
        revision = ""
        try:
            for _ in range(request.limit):
                row, after, revision = self._next(request, next_position, target, context)
                if row is None:
                    next_position = None
                    break
                encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                if len(encoded.encode()) + 2 > request.byte_limit:
                    row = {
                        "id": row["id"],
                        "truncated": True,
                        "detail_ref": f"{request.resource}/{row['id']}",
                    }
                    encoded = json.dumps(row, separators=(",", ":"))
                    truncated = True
                cost = len(encoded.encode()) + (1 if items else 0)
                if size + cost > request.byte_limit:
                    if not items:
                        context.abort(
                            grpc.StatusCode.INVALID_ARGUMENT,
                            "byte_limit cannot hold a resource descriptor",
                        )
                    break
                items.append(encoded)
                size += cost
                next_position = after
                if after is None:
                    break
        except StructuralBaselineUnavailableError:
            context.abort(grpc.StatusCode.OUT_OF_RANGE, "cursor_expired")
        except AmbiguousWorkflow:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Ambiguous workflow selector")
        except (KeyError, ValueError):
            context.abort(grpc.StatusCode.NOT_FOUND, "Resource target unavailable")
        cursor = ""
        if next_position is not None:
            cursor = uuid4().hex
            with self._lock:
                self._positions[cursor] = next_position
                while len(self._positions) > 1024:
                    self._positions.popitem(last=False)
        return pb.TuiResourcePageV2(
            items_json=items,
            next_cursor=cursor,
            truncated=truncated,
            revision=revision,
        )

    def _next(
        self,
        request: pb.ReadTuiResourceRequestV2,
        position: _Position | None,
        target: tuple[str, ...],
        context: grpc.ServicerContext,
    ) -> tuple[dict[str, JsonValue] | None, _Position | None, str]:
        server = self._server
        op = server._op
        offset = position.offset if position is not None else 0
        if request.resource == "runs":
            page = op.list_run_summaries(
                request.workflow_selector,
                page_size=1,
                page_token=position.run_token if position is not None else "",
            )
            revision = f"{op.operator_instance_id}:runs:{page.as_of_sequence}"
            if position is not None and position.revision != revision:
                context.abort(grpc.StatusCode.OUT_OF_RANGE, "cursor_expired")
            if not page.runs:
                return None, None, revision
            run = page.runs[0]
            row: dict[str, JsonValue] = {
                "id": run.run_id,
                "flow": run.workflow_id or run.flow_name,
                "status": run.status.value,
                "triggered_at": run.triggered_at,
                "revision": run.revision,
            }
            after = (
                _Position(target, revision, run_token=page.next_page_token)
                if page.next_page_token
                else None
            )
            return row, after, revision
        if request.resource == "artifacts":
            payload = server._result_payload(request.run_id, context)
            revision = f"{op.operator_instance_id}:artifacts:{request.run_id}"
            if position is not None and position.revision != revision:
                context.abort(grpc.StatusCode.OUT_OF_RANGE, "cursor_expired")
            files = result_file_references(payload.value_json)
            if offset >= len(files):
                return None, None, revision
            identifier, file = files[offset]
            if isinstance(file, File):
                row = {
                    "id": identifier,
                    "kind": "file",
                    "name": file.name,
                    "media_type": file.content_type,
                    "bytes": file.size,
                    "sha256": file.sha256,
                    "reference": file.model_dump(mode="json"),
                    "downloadable": True,
                }
            else:
                row = {
                    "id": identifier,
                    "kind": "workspace",
                    "downloadable": False,
                    "reason": "Directory export requires ava result --output-dir",
                    "reference": file.manifest() if file.is_reference else None,
                    "entry_count": None if file.is_reference else len(file.entries),
                }
            after = _Position(target, revision, offset + 1) if offset + 1 < len(files) else None
            return row, after, revision
        if request.resource != "schedules":
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Unsupported TUI resource")
        catalog = op.get_catalog()
        flows = sorted(
            (flow for flow in catalog.workflows if flow.cron), key=lambda flow: flow.selector
        )
        # Dispatch observations change without a catalog declaration revision.
        # Pin those values too; a continuation must never mix schedule pages.
        observation = json.dumps(
            [
                (
                    flow.selector,
                    flow.rendered_name,
                    flow.cron,
                    flow.next_run_at,
                    flow.last_run_at,
                )
                for flow in flows
            ],
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(observation.encode()).hexdigest()
        revision = f"{op.operator_instance_id}:schedules:{catalog.revision}:{digest}"
        if position is not None and position.revision != revision:
            context.abort(grpc.StatusCode.OUT_OF_RANGE, "cursor_expired")
        if offset >= len(flows):
            return None, None, revision
        flow = flows[offset]
        row = {
            "id": flow.selector,
            "flow": flow.rendered_name,
            "cron": flow.cron,
            "next_run_at": flow.next_run_at,
            "last_run_at": flow.last_run_at,
            "control": "inspection_only",
        }
        after = _Position(target, revision, offset + 1) if offset + 1 < len(flows) else None
        return row, after, revision
