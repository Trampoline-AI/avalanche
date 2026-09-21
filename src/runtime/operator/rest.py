"""Local JSON HTTP routes backed by the operator's existing unary RPCs."""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

import grpc
from google.protobuf.json_format import MessageToJson, ParseDict, ParseError
from google.protobuf.message import Message
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from .proto import operator_pb2 as pb
from .proto import operator_pb2_grpc as pb_grpc

_MAX_BODY_BYTES = 4 * 1024 * 1024
_RPC_TIMEOUT_SECONDS = 30.0
_RUN_PATH = re.compile(r"/api/v1/runs/([^/]+)(?:/(cancel|output|activity))?")
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_HTTP_ERRORS = {
    grpc.StatusCode.INVALID_ARGUMENT: HTTPStatus.BAD_REQUEST,
    grpc.StatusCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
    grpc.StatusCode.ALREADY_EXISTS: HTTPStatus.CONFLICT,
    grpc.StatusCode.FAILED_PRECONDITION: HTTPStatus.CONFLICT,
    grpc.StatusCode.ABORTED: HTTPStatus.CONFLICT,
    grpc.StatusCode.OUT_OF_RANGE: HTTPStatus.BAD_REQUEST,
    grpc.StatusCode.UNAUTHENTICATED: HTTPStatus.UNAUTHORIZED,
    grpc.StatusCode.PERMISSION_DENIED: HTTPStatus.FORBIDDEN,
    grpc.StatusCode.RESOURCE_EXHAUSTED: HTTPStatus.TOO_MANY_REQUESTS,
    grpc.StatusCode.UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED: HTTPStatus.GATEWAY_TIMEOUT,
    grpc.StatusCode.UNIMPLEMENTED: HTTPStatus.NOT_IMPLEMENTED,
}


class _CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    workflow_selector: str = Field(min_length=1)
    run_id: str = Field(default_factory=lambda: f"run_{uuid4().hex}", min_length=1)
    input_json: dict[str, JsonValue] = Field(default_factory=dict)
    context_json: dict[str, JsonValue] = Field(default_factory=dict)


class _Query(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PageQuery(_Query):
    page_size: int = Field(default=100, ge=1, le=2**32 - 1)
    continuation: str | None = None

    def continuation_message(self) -> pb.ContinuationRefV2 | None:
        if self.continuation is None:
            return None
        value = _JSON_OBJECT.validate_json(self.continuation, strict=True)
        return ParseDict(value, pb.ContinuationRefV2())


class _RunsQuery(_PageQuery):
    workflow_selector: str = ""


class _ActivityQuery(_PageQuery):
    node_id: str = ""
    order: str = Field(default="forward", pattern=r"^(forward|newest_first)$")


class _HttpError(Exception):
    def __init__(self, status: HTTPStatus, message: str, *, allow: str | None = None) -> None:
        self.status = status
        self.allow = allow
        super().__init__(message)


def _create_request(handler: BaseHTTPRequestHandler) -> pb.StartRunRequestV2:
    lengths = handler.headers.get_all("Content-Length", [])
    if handler.headers.get("Transfer-Encoding") is not None:
        raise _HttpError(HTTPStatus.BAD_REQUEST, "Transfer-Encoding is not supported")
    if not lengths:
        raise _HttpError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
        raise _HttpError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
    length = int(lengths[0])
    if length > _MAX_BODY_BYTES:
        raise _HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body exceeds 4 MiB")
    body = handler.rfile.read(length)
    if len(body) != length:
        raise _HttpError(HTTPStatus.BAD_REQUEST, "Incomplete request body")
    payload = _CreateRun.model_validate_json(body)
    return pb.StartRunRequestV2(
        run_id=payload.run_id,
        workflow_selector=payload.workflow_selector,
        input_json=json.dumps(payload.input_json, allow_nan=False),
        context_json=json.dumps(payload.context_json, allow_nan=False),
    )


def _dispatch(
    handler: BaseHTTPRequestHandler, channel: grpc.Channel
) -> tuple[HTTPStatus, Message]:
    url = urlsplit(handler.path)
    query = parse_qs(url.query, keep_blank_values=True, max_num_fields=16)
    if any(len(values) != 1 for values in query.values()):
        raise _HttpError(HTTPStatus.BAD_REQUEST, "Duplicate query parameter")
    parameters = {key: values[0] for key, values in query.items()}
    stub = pb_grpc.OperatorServiceV2Stub(channel)
    path = url.path
    match = _RUN_PATH.fullmatch(path)
    if path == "/api/v1/flows":
        allowed = ("GET",)
    elif path == "/api/v1/runs":
        allowed = ("GET", "POST")
    elif match is not None:
        allowed = ("POST",) if match[2] == "cancel" else ("GET",)
    else:
        raise _HttpError(HTTPStatus.NOT_FOUND, "Unknown API route")
    if handler.command not in allowed:
        raise _HttpError(
            HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed", allow=", ".join(allowed)
        )
    # Cross-origin JSON mutations require a CORS preflight; ordinary forms do not.
    if handler.command == "POST" and handler.headers.get_content_type() != "application/json":
        raise _HttpError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Expected application/json")

    if path == "/api/v1/flows":
        page = _PageQuery.model_validate_strings(parameters)
        return HTTPStatus.OK, stub.DiscoverFlows(
            pb.DiscoverFlowsRequestV2(
                page_size=page.page_size, continuation=page.continuation_message()
            ),
            timeout=_RPC_TIMEOUT_SECONDS,
        )
    if path == "/api/v1/runs":
        if handler.command == "POST":
            _Query.model_validate_strings(parameters)
            return HTTPStatus.ACCEPTED, stub.StartRun(
                _create_request(handler), timeout=_RPC_TIMEOUT_SECONDS
            )
        runs = _RunsQuery.model_validate_strings(parameters)
        return HTTPStatus.OK, stub.ListRunSummaries(
            pb.ListRunSummariesRequestV2(
                workflow_selector=runs.workflow_selector,
                page_size=runs.page_size,
                continuation=runs.continuation_message(),
            ),
            timeout=_RPC_TIMEOUT_SECONDS,
        )
    assert match is not None
    run_id = unquote(match[1])
    action = match[2]
    if action == "activity":
        activity = _ActivityQuery.model_validate_strings(parameters)
        return HTTPStatus.OK, stub.ListRunActivity(
            pb.ListRunActivityRequestV2(
                run_id=run_id,
                page_size=activity.page_size,
                continuation=activity.continuation_message(),
                node_id=activity.node_id,
                order=(
                    pb.PAGE_ORDER_V2_NEWEST_FIRST
                    if activity.order == "newest_first"
                    else pb.PAGE_ORDER_V2_FORWARD
                ),
            ),
            timeout=_RPC_TIMEOUT_SECONDS,
        )
    _Query.model_validate_strings(parameters)
    if action == "cancel":
        return HTTPStatus.OK, stub.CancelRun(
            pb.CancelRunRequestV2(run_id=run_id), timeout=_RPC_TIMEOUT_SECONDS
        )
    if action == "output":
        return HTTPStatus.OK, stub.GetRunResult(
            pb.GetRunResultRequestV2(run_id=run_id), timeout=_RPC_TIMEOUT_SECONDS
        )
    return HTTPStatus.OK, stub.GetRunSnapshot(
        pb.GetRunSnapshotRequestV2(run_id=run_id), timeout=_RPC_TIMEOUT_SECONDS
    )


def serve_rest(handler: BaseHTTPRequestHandler, channel: grpc.Channel) -> None:
    """Serve one bounded JSON request without changing operator execution semantics."""
    # Do not reuse a connection with an unread body after rejecting a request.
    handler.close_connection = True
    allow = None
    try:
        status, response = _dispatch(handler, channel)
        body = MessageToJson(
            response,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        ).encode("utf-8")
    except _HttpError as exc:
        status = exc.status
        allow = exc.allow
        body = _error_body(status.name, str(exc))
    except (ValidationError, ParseError, ValueError) as exc:
        status = HTTPStatus.BAD_REQUEST
        body = _error_body("INVALID_ARGUMENT", str(exc))
    except grpc.RpcError as exc:
        status = _HTTP_ERRORS.get(exc.code(), HTTPStatus.INTERNAL_SERVER_ERROR)
        body = _error_body(exc.code().name, exc.details())
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Connection", "close")
    if allow is not None:
        handler.send_header("Allow", allow)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def _error_body(code: str, message: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": message}}).encode("utf-8")
