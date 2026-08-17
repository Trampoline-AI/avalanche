"""gRPC servicer implementing the native V2 operator contract.

The local loopback operator has one implicit authorization scope bound to the
operator instance id. Scope references, lifecycle cursors, and continuations
are revalidated against that scope on every request; staged object-store
attachments are rejected because no remote object plane exists locally.
"""

from __future__ import annotations

import hashlib
import queue
from typing import Any

import grpc

from avalanche.runtime import File

from .convert_v2 import (
    DETAIL_URI_SCHEME,
    RESULT_URI_SCHEME,
    TRACE_URI_SCHEME,
    agent_event_activity_to_v2,
    flow_list_to_v2,
    log_activity_to_v2,
    run_snapshot_to_v2,
    run_summary_to_v2,
    sha256_hex,
    update_envelope_to_v2,
    workflow_info_to_v2,
)
from .operator import (
    InvalidRunIdError,
    Operator,
    RunAlreadyExistsError,
    RunResultNotReadyError,
    RunResultUnavailableError,
    StructuralBaselineUnavailableError,
    _bounded_page_size,
)
from .proto import operator_pb2 as pb
from .proto import operator_pb2_grpc as pb_grpc
from .registry import AmbiguousWorkflow, UnknownWorkflow
from .server import TRACE_CHUNK_BYTES, _ambiguous_detail, _decode_json_object

_FLOWS_STREAM = "flows"
_RUN_SUMMARIES_STREAM = "run-summaries"
_EVENTS_STREAM = "operator-events"


class OperatorV2Servicer(pb_grpc.OperatorServiceV2Servicer):
    """V2 gRPC servicer that delegates to an Operator instance."""

    def __init__(self, operator: Operator) -> None:
        self._op = operator

    @property
    def _generation(self) -> int:
        return int.from_bytes(
            hashlib.sha256(self._op.operator_instance_id.encode("utf-8")).digest()[:8],
            "big",
        )

    # ── Scope / cursor helpers ────────────────────────────

    def _scope(self) -> pb.ScopeReferenceV2:
        return pb.ScopeReferenceV2(reference=self._op.operator_instance_id)

    def _cursor(
        self, stream: str, source_sequence: int, *, topology_fingerprint: str = ""
    ) -> pb.LifecycleCursorV2:
        return pb.LifecycleCursorV2(
            stream=stream,
            topology_fingerprint=topology_fingerprint,
            stream_generation=self._generation,
            source_sequence=source_sequence,
        )

    def _validate_scope(self, scope_ref: pb.ScopeReferenceV2, context) -> None:
        reference = scope_ref.reference
        if reference and reference != self._op.operator_instance_id:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Scope reference is stale; rebaseline against the current operator scope",
            )

    def _validate_cursor(self, cursor: pb.LifecycleCursorV2, context) -> None:
        if cursor.stream_generation not in (0, self._generation):
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Cursor belongs to a different stream generation; rebaseline required",
            )

    def _continuation_token(self, continuation: pb.ContinuationRefV2, context) -> str:
        if not continuation.continuation_id:
            return ""
        self._validate_scope(continuation.scope_ref, context)
        self._validate_cursor(continuation.cursor, context)
        return continuation.continuation_id

    def _continuation(
        self, stream: str, token: str, source_sequence: int
    ) -> pb.ContinuationRefV2 | None:
        if not token:
            return None
        return pb.ContinuationRefV2(
            scope_ref=self._scope(),
            continuation_id=token,
            cursor=self._cursor(stream, source_sequence),
        )

    # ── Discovery ─────────────────────────────────────────

    def DiscoverFlows(self, request, context):  # noqa: N802
        catalog = self._op.get_catalog()
        token = self._continuation_token(request.continuation, context)
        offset = 0
        if token:
            if not token.startswith("flows:"):
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid flows continuation")
            try:
                offset = int(token[len("flows:") :])
            except ValueError:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid flows continuation")
        flows = sorted(catalog.workflows, key=lambda info: info.selector)
        size = _bounded_page_size(request.page_size)
        page = flows[offset : offset + size]
        next_offset = offset + len(page)
        next_page = (
            self._continuation(_FLOWS_STREAM, f"flows:{next_offset}", catalog.as_of_sequence)
            if next_offset < len(flows)
            else None
        )
        return flow_list_to_v2(
            catalog,
            cursor=self._cursor(
                _FLOWS_STREAM,
                catalog.as_of_sequence,
                topology_fingerprint=str(catalog.revision),
            ),
            flows=[workflow_info_to_v2(info) for info in page],
            next_page=next_page,
            scope_ref=self._scope(),
        )

    # ── Run control ───────────────────────────────────────

    def StartRun(self, request, context):  # noqa: N802
        if not request.run_id:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "run_id is required as the idempotency key",
            )
        try:
            run_input = _decode_input_payload_v2(request)
            run_context = _decode_json_object(request.context_json, "context_json")
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        try:
            run_id = self._op.start_run(
                request.workflow_selector,
                run_id=request.run_id,
                input=run_input,
                context=run_context,
            )
            return pb.StartRunResponseV2(run_id=run_id)
        except InvalidRunIdError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except RunAlreadyExistsError as exc:
            context.abort(grpc.StatusCode.ALREADY_EXISTS, str(exc))
        except AmbiguousWorkflow as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, _ambiguous_detail(exc))
        except UnknownWorkflow as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, exc.args[0])
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))

    def CancelRun(self, request, context):  # noqa: N802
        self._op.cancel_run(request.run_id)
        return pb.CancelRunResponseV2(run_id=request.run_id)

    # ── Run projections ───────────────────────────────────

    def ListRunSummaries(self, request, context):  # noqa: N802
        token = self._continuation_token(request.continuation, context)
        try:
            page = self._op.list_run_summaries(
                request.workflow_selector,
                page_size=request.page_size,
                page_token=token,
            )
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except AmbiguousWorkflow as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, _ambiguous_detail(exc))
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        message = pb.RunSummaryPageV2(
            cursor=self._cursor(_RUN_SUMMARIES_STREAM, page.as_of_sequence),
            runs=[run_summary_to_v2(item) for item in page.runs],
            scope_ref=self._scope(),
        )
        next_page = self._continuation(
            _RUN_SUMMARIES_STREAM, page.next_page_token, page.as_of_sequence
        )
        if next_page is not None:
            message.next_page.CopyFrom(next_page)
        return message

    def GetRunSnapshot(self, request, context):  # noqa: N802
        try:
            snapshot = self._op.get_latest_run_snapshot(
                request.run_id,
                operator_instance_id=self._op.operator_instance_id,
            )
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        if snapshot is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Run {request.run_id} not found")
        return run_snapshot_to_v2(
            snapshot,
            cursor=self._cursor(f"run:{request.run_id}", snapshot.as_of_sequence),
            scope_ref=self._scope(),
        )

    # ── Activity ──────────────────────────────────────────

    def ListRunActivity(self, request, context):  # noqa: N802
        token = self._continuation_token(request.continuation, context)
        order = int(request.order)
        stream = f"activity:{request.run_id}:{request.node_id or 'logs'}"
        try:
            if request.node_id:
                page = self._op.list_agent_events(
                    request.run_id,
                    request.node_id,
                    page_token=token,
                    page_size=request.page_size,
                    order=order,
                )
                activities = [
                    agent_event_activity_to_v2(
                        item,
                        run_id=page.run_id,
                        node_id=page.node_id,
                        scope_ref=self._scope(),
                    )
                    for item in page.events
                ]
            else:
                page = self._op.list_logs(
                    request.run_id,
                    page_token=token,
                    page_size=request.page_size,
                    order=order,
                )
                activities = [
                    log_activity_to_v2(item, run_id=request.run_id, scope_ref=self._scope())
                    for item in page.logs
                ]
        except StructuralBaselineUnavailableError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except KeyError:
            context.abort(grpc.StatusCode.NOT_FOUND, "Activity target not found")
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        message = pb.RunActivityPageV2(
            cursor=self._cursor(stream, page.as_of_sequence),
            run_id=request.run_id,
            activities=activities,
            scope_ref=self._scope(),
        )
        next_page = self._continuation(stream, page.next_page_token, page.as_of_sequence)
        if next_page is not None:
            message.next_page.CopyFrom(next_page)
        return message

    def ReadActivityDetail(self, request, context):  # noqa: N802
        detail_ref = request.detail_ref
        self._validate_scope(detail_ref.scope_ref, context)
        uri = detail_ref.object_uri
        if uri.startswith(DETAIL_URI_SCHEME):
            body_token = uri[len(DETAIL_URI_SCHEME) :]
            if body_token != detail_ref.object_key:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "Detail reference object key does not match its URI",
                )
            try:
                data = self._op.read_detail(body_token)
            except StructuralBaselineUnavailableError as exc:
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            except KeyError:
                context.abort(grpc.StatusCode.NOT_FOUND, "Detail body not found")
            except ValueError as exc:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        elif uri.startswith(TRACE_URI_SCHEME):
            parts = uri[len(TRACE_URI_SCHEME) :].split("/")
            if len(parts) != 3 or parts[2] != detail_ref.object_key.split("/")[-1]:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT, "Trace reference does not match its URI"
                )
            run_id, node_id, revision_text = parts
            if run_id != detail_ref.run_id:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "Trace reference run does not match its URI",
                )
            try:
                trace = self._op.read_trace(
                    run_id,
                    node_id,
                    operator_instance_id=self._op.operator_instance_id,
                    revision=int(revision_text),
                )
            except StructuralBaselineUnavailableError as exc:
                context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            except KeyError:
                context.abort(grpc.StatusCode.NOT_FOUND, "Trace body not found")
            data = trace.data
        else:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"Unsupported activity detail URI scheme: {uri!r}",
            )
        yield from _stream_chunks(data, pb.ActivityDetailChunkV2)

    # ── Results ───────────────────────────────────────────

    def _result_payload(self, run_id: str, context):
        try:
            return self._op._get_run_result_payload(run_id)
        except KeyError:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Run {run_id} not found")
        except (RunResultNotReadyError, RunResultUnavailableError) as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            context.abort(grpc.StatusCode.DATA_LOSS, str(exc))

    def _artifact_ref(self, run_id: str, item, run_sequence: int) -> pb.RunOutputArtifactRefV2:
        return pb.RunOutputArtifactRefV2(
            run_id=run_id,
            scope_ref=self._scope(),
            artifact_id=item.attachment_id,
            run_sequence=run_sequence,
            object_uri=f"{RESULT_URI_SCHEME}{run_id}/{item.attachment_id}",
            object_key=item.attachment_id,
            sha256=item.sha256,
            size_bytes=len(item.content),
        )

    def _run_sequence(self, run_id: str) -> int:
        snapshot = self._op.get_latest_run_snapshot(
            run_id, operator_instance_id=self._op.operator_instance_id
        )
        return snapshot.summary.created_sequence if snapshot is not None else 0

    @staticmethod
    def _set_file_metadata(descriptor, item) -> None:
        if item.name is not None:
            descriptor.name = item.name
        if item.media_type is not None:
            descriptor.media_type = item.media_type

    def GetRunResult(self, request, context):  # noqa: N802
        payload = self._result_payload(request.run_id, context)
        run_sequence = self._run_sequence(request.run_id)
        value_bytes = payload.value_json.encode("utf-8")
        return pb.RunResultV2(
            cursor=self._cursor(f"run:{request.run_id}", run_sequence),
            run_id=request.run_id,
            scope_ref=self._scope(),
            value=pb.ResultValueV2(
                value_json=payload.value_json,
                sha256=sha256_hex(value_bytes),
                size_bytes=len(value_bytes),
            ),
            files=[
                self._result_file_descriptor(request.run_id, item, run_sequence)
                for item in payload.files
            ],
        )

    def _result_file_descriptor(
        self, run_id: str, item, run_sequence: int
    ) -> pb.ResultFileDescriptorV2:
        descriptor = pb.ResultFileDescriptorV2(
            artifact_ref=self._artifact_ref(run_id, item, run_sequence),
        )
        self._set_file_metadata(descriptor, item)
        return descriptor

    def ListRunOutputArtifacts(self, request, context):  # noqa: N802
        payload = self._result_payload(request.run_id, context)
        run_sequence = self._run_sequence(request.run_id)
        token = self._continuation_token(request.continuation, context)
        offset = 0
        if token:
            if not token.startswith("artifacts:"):
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT, "Invalid artifacts continuation"
                )
            try:
                offset = int(token[len("artifacts:") :])
            except ValueError:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT, "Invalid artifacts continuation"
                )
        size = _bounded_page_size(request.page_size)
        page = payload.files[offset : offset + size]
        next_offset = offset + len(page)
        stream = f"artifacts:{request.run_id}"
        message = pb.RunOutputArtifactPageV2(
            cursor=self._cursor(stream, run_sequence),
            run_id=request.run_id,
            scope_ref=self._scope(),
            artifacts=[
                self._artifact_descriptor(request.run_id, item, run_sequence)
                for item in page
            ],
        )
        if next_offset < len(payload.files):
            message.next_page.CopyFrom(
                self._continuation(stream, f"artifacts:{next_offset}", run_sequence)
            )
        return message

    def _artifact_descriptor(
        self, run_id: str, item, run_sequence: int
    ) -> pb.RunOutputArtifactDescriptorV2:
        descriptor = pb.RunOutputArtifactDescriptorV2(
            artifact_ref=self._artifact_ref(run_id, item, run_sequence),
        )
        self._set_file_metadata(descriptor, item)
        return descriptor

    def ReadRunOutputArtifact(self, request, context):  # noqa: N802
        artifact_ref = request.artifact_ref
        self._validate_scope(artifact_ref.scope_ref, context)
        uri = artifact_ref.object_uri
        prefix = f"{RESULT_URI_SCHEME}{artifact_ref.run_id}/"
        if not uri.startswith(prefix) or uri[len(prefix) :] != artifact_ref.artifact_id:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "Artifact reference does not match its URI"
            )
        payload = self._result_payload(artifact_ref.run_id, context)
        for item in payload.files:
            if item.attachment_id == artifact_ref.artifact_id:
                data = item.content
                if sha256_hex(data) != artifact_ref.sha256:
                    context.abort(grpc.StatusCode.DATA_LOSS, "Artifact content digest mismatch")
                yield from _stream_chunks(data, pb.RunOutputArtifactChunkV2)
                return
        context.abort(
            grpc.StatusCode.NOT_FOUND,
            f"Artifact {artifact_ref.artifact_id} not found for run {artifact_ref.run_id}",
        )

    # ── Watch ─────────────────────────────────────────────

    def WatchRunStatus(self, request, context):  # noqa: N802
        after_cursor = request.after_cursor
        foreign_generation = (
            after_cursor.stream_generation
            and after_cursor.stream_generation != self._generation
        )
        if foreign_generation:
            envelope = pb.RunStatusEnvelopeV2(
                reset_required=pb.ResetRequiredV2(
                    history_floor=self._cursor(_EVENTS_STREAM, 0),
                    latest_cursor=self._cursor(_EVENTS_STREAM, self._op.current_sequence),
                )
            )
            envelope.source_sequence = self._op.current_sequence
            envelope.cursor.CopyFrom(self._cursor(_EVENTS_STREAM, self._op.current_sequence))
            envelope.scope_ref.CopyFrom(self._scope())
            yield envelope
            return
        after_sequence = (
            request.after_cursor.source_sequence
            if request.HasField("after_cursor")
            else self._op.current_sequence
        )
        subscription = self._op.subscribe_operator_updates(
            self._op.operator_instance_id, after_sequence
        )
        try:
            context.send_initial_metadata(())
            while context.is_active():
                try:
                    envelope = subscription.get(timeout=1.0)
                except queue.Empty:
                    continue
                yield update_envelope_to_v2(
                    envelope,
                    scope_ref=self._scope(),
                    cursor_for=lambda sequence: self._cursor(_EVENTS_STREAM, sequence),
                )
                if envelope.reset_required is not None:
                    return
        finally:
            self._op.unsubscribe_operator_updates(subscription)


def _stream_chunks(data: bytes, chunk_type):
    if not data:
        yield chunk_type(chunk_index=0, data=b"", eof=True)
        return
    for chunk_index, offset in enumerate(range(0, len(data), TRACE_CHUNK_BYTES)):
        chunk = data[offset : offset + TRACE_CHUNK_BYTES]
        yield chunk_type(
            chunk_index=chunk_index,
            data=chunk,
            eof=offset + len(chunk) == len(data),
        )


def _decode_input_payload_v2(request) -> dict[str, Any] | None:
    payload = _decode_json_object(request.input_json, "input_json") or {}
    for attachment in request.input_files:
        if not attachment.field_name:
            raise ValueError("input file attachment is missing field_name")
        if attachment.object_uri or attachment.object_key:
            raise ValueError(
                "staged object attachments are not supported by the local loopback operator"
            )
        if attachment.field_name in payload:
            raise ValueError(f"Duplicate input field '{attachment.field_name}'")
        payload[attachment.field_name] = File(
            name=attachment.name or None,
            content=bytes(attachment.inline_bytes),
            content_type=attachment.media_type or None,
            sha256=attachment.sha256 or None,
        )
    return payload or None
