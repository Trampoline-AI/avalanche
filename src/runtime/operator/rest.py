"""FastAPI JSON routes backed by the operator's existing unary RPCs."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from http import HTTPStatus
from typing import Annotated, Literal
from urllib.parse import unquote
from uuid import uuid4

import grpc
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from google.protobuf.json_format import MessageToJson, ParseDict, ParseError
from google.protobuf.message import Message
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.routing import Match
from starlette.types import Scope

from .proto import operator_pb2 as pb
from .proto import operator_pb2_grpc as pb_grpc

_MAX_BODY_BYTES = 4 * 1024 * 1024
_RPC_TIMEOUT_SECONDS = 30.0
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    # A rejected request or bodyless mutation can leave unread bytes behind.
    "Connection": "close",
}
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

    workflow_selector: str = Field(min_length=1, description="Workflow selector to execute.")
    run_id: str = Field(
        default_factory=lambda: f"run_{uuid4().hex}",
        min_length=1,
        description="Idempotency key; generated when omitted.",
    )
    input_json: dict[str, JsonValue] = Field(default_factory=dict)
    context_json: dict[str, JsonValue] = Field(default_factory=dict)


class _Query(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PageQuery(_Query):
    page_size: int = Field(default=100, ge=1, le=2**32 - 1)
    continuation: str | None = Field(
        default=None,
        description="JSON-encoded next_page reference from the preceding response, unchanged.",
    )

    def continuation_message(self) -> pb.ContinuationRefV2 | None:
        if self.continuation is None:
            return None
        value = _JSON_OBJECT.validate_json(self.continuation, strict=True)
        return ParseDict(value, pb.ContinuationRefV2())


class _RunsQuery(_PageQuery):
    workflow_selector: str = ""


class _ActivityQuery(_PageQuery):
    node_id: str = ""
    order: Literal["forward", "newest_first"] = "forward"


class _ErrorDetail(BaseModel):
    code: str
    message: str


class _ErrorResponse(BaseModel):
    error: _ErrorDetail


def _body_length(request: Request, *, required: bool) -> int | None:
    # Cross-origin JSON mutations require a preflight; ordinary forms do not.
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Expected application/json")
    if "transfer-encoding" in request.headers:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Transfer-Encoding is not supported")
    lengths = request.headers.getlist("content-length")
    if not lengths:
        if required:
            raise HTTPException(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
        return None
    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
    length = int(lengths[0])
    if length > _MAX_BODY_BYTES:
        raise HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body exceeds 4 MiB")
    return length


class _BoundedRequest(Request):
    def __init__(self, request: Request, length: int) -> None:
        super().__init__(request.scope, request.receive)
        self._length = length

    async def stream(self) -> AsyncIterator[bytes]:
        received = 0
        try:
            async for chunk in super().stream():
                received += len(chunk)
                if received > self._length:
                    raise HTTPException(HTTPStatus.BAD_REQUEST, "Body exceeds Content-Length")
                yield chunk
        except ClientDisconnect as exc:
            raise HTTPException(HTTPStatus.BAD_REQUEST, "Incomplete request body") from exc
        if received != self._length:
            raise HTTPException(HTTPStatus.BAD_REQUEST, "Incomplete request body")


class _RestRoute(APIRoute):
    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] != "http":
            return super().matches(scope)
        # Match URL segments before decoding so an escaped slash inside a run ID
        # cannot become a route separator. Decode the captured identifier once.
        match, child = super().matches({**scope, "path": scope["raw_path"].decode("latin-1")})
        if match != Match.NONE:
            child["path_params"] = {
                name: unquote(value) for name, value in child["path_params"].items()
            }
        return match, child

    def get_route_handler(self) -> Callable[[Request], Coroutine[None, None, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            # Keep duplicate detection separate from Pydantic's single-value query extraction.
            query = request.query_params.multi_items()
            if len(query) > 16:
                raise ValueError("Max number of fields exceeded")
            if len({key for key, _ in query}) != len(query):
                raise HTTPException(HTTPStatus.BAD_REQUEST, "Duplicate query parameter")
            if request.method == "POST":
                length = _body_length(request, required=self.body_field is not None)
                if length is not None:
                    request = _BoundedRequest(request, length)
            return await handler(request)

        return guarded


def _protobuf_response(message: Message, status: HTTPStatus = HTTPStatus.OK) -> Response:
    return Response(
        MessageToJson(
            message,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        ),
        status_code=status,
        media_type="application/json",
        headers=_RESPONSE_HEADERS,
    )


def _error_response(
    status: int, code: str, message: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status,
        headers={**_RESPONSE_HEADERS, **(headers or {})},
    )


def create_rest_app(channel: grpc.Channel) -> FastAPI:
    """Build the /api subapplication; the browser listener owns the gRPC channel."""
    app = FastAPI(
        title="Avalanche operator REST API",
        version="1",
        description=(
            "Local operator control backed by the gRPC API. Responses use protobuf JSON: "
            "field names are snake_case and 64-bit integers are decimal strings."
        ),
        redirect_slashes=False,
        redoc_url=None,
    )
    router = APIRouter(
        route_class=_RestRoute,
        responses={
            400: {"model": _ErrorResponse, "description": "Invalid request"},
            "default": {"model": _ErrorResponse, "description": "HTTP or operator error"},
        },
    )
    stub = pb_grpc.OperatorServiceV2Stub(channel)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        status = HTTPStatus(exc.status_code)
        if not isinstance(exc.detail, str):
            raise TypeError("HTTP error details must be text")
        message = exc.detail
        headers = dict(exc.headers or {})
        if status == HTTPStatus.NOT_FOUND:
            message = "Unknown API route"
        elif status == HTTPStatus.METHOD_NOT_ALLOWED:
            message = "Method not allowed"
            # Starlette reports the first matching route, not every method for its path.
            allowed: set[str] = set()
            for route in router.routes:
                if (
                    isinstance(route, APIRoute)
                    and route.matches(request.scope)[0] == Match.PARTIAL
                ):
                    allowed.update(route.methods)
            if allowed:
                headers["Allow"] = ", ".join(sorted(allowed))
        return _error_response(status, status.name, message, headers)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    @app.exception_handler(ParseError)
    @app.exception_handler(ValueError)
    async def invalid_request(
        request: Request,
        exc: RequestValidationError | ValidationError | ParseError | ValueError,
    ) -> JSONResponse:
        return _error_response(HTTPStatus.BAD_REQUEST, "INVALID_ARGUMENT", str(exc))

    @app.exception_handler(grpc.RpcError)
    async def rpc_error(request: Request, exc: grpc.RpcError) -> JSONResponse:
        code = exc.code()
        status = _HTTP_ERRORS.get(code, HTTPStatus.INTERNAL_SERVER_ERROR)
        return _error_response(status, code.name, exc.details())

    @router.get("/v1/flows", summary="Discover workflows")
    def discover_flows(query: Annotated[_PageQuery, Query()]) -> Response:
        return _protobuf_response(
            stub.DiscoverFlows(
                pb.DiscoverFlowsRequestV2(
                    page_size=query.page_size, continuation=query.continuation_message()
                ),
                timeout=_RPC_TIMEOUT_SECONDS,
            )
        )

    @router.get("/v1/runs", summary="List run summaries")
    def list_runs(query: Annotated[_RunsQuery, Query()]) -> Response:
        return _protobuf_response(
            stub.ListRunSummaries(
                pb.ListRunSummariesRequestV2(
                    workflow_selector=query.workflow_selector,
                    page_size=query.page_size,
                    continuation=query.continuation_message(),
                ),
                timeout=_RPC_TIMEOUT_SECONDS,
            )
        )

    @router.post("/v1/runs", status_code=HTTPStatus.ACCEPTED, summary="Start a workflow run")
    def create_run(payload: _CreateRun, query: Annotated[_Query, Query()]) -> Response:
        return _protobuf_response(
            stub.StartRun(
                pb.StartRunRequestV2(
                    run_id=payload.run_id,
                    workflow_selector=payload.workflow_selector,
                    input_json=json.dumps(payload.input_json, allow_nan=False),
                    context_json=json.dumps(payload.context_json, allow_nan=False),
                ),
                timeout=_RPC_TIMEOUT_SECONDS,
            ),
            HTTPStatus.ACCEPTED,
        )

    @router.get("/v1/runs/{run_id}", summary="Get a run snapshot")
    def get_run(run_id: str, query: Annotated[_Query, Query()]) -> Response:
        return _protobuf_response(
            stub.GetRunSnapshot(
                pb.GetRunSnapshotRequestV2(run_id=run_id), timeout=_RPC_TIMEOUT_SECONDS
            )
        )

    @router.post("/v1/runs/{run_id}/cancel", summary="Cancel a workflow run")
    def cancel_run(run_id: str, query: Annotated[_Query, Query()]) -> Response:
        return _protobuf_response(
            stub.CancelRun(pb.CancelRunRequestV2(run_id=run_id), timeout=_RPC_TIMEOUT_SECONDS)
        )

    @router.get("/v1/runs/{run_id}/output", summary="Get a completed run's output")
    def get_output(run_id: str, query: Annotated[_Query, Query()]) -> Response:
        return _protobuf_response(
            stub.GetRunResult(
                pb.GetRunResultRequestV2(run_id=run_id), timeout=_RPC_TIMEOUT_SECONDS
            )
        )

    @router.get("/v1/runs/{run_id}/activity", summary="List run activity")
    def list_activity(run_id: str, query: Annotated[_ActivityQuery, Query()]) -> Response:
        return _protobuf_response(
            stub.ListRunActivity(
                pb.ListRunActivityRequestV2(
                    run_id=run_id,
                    page_size=query.page_size,
                    continuation=query.continuation_message(),
                    node_id=query.node_id,
                    order=(
                        pb.PAGE_ORDER_V2_NEWEST_FIRST
                        if query.order == "newest_first"
                        else pb.PAGE_ORDER_V2_FORWARD
                    ),
                ),
                timeout=_RPC_TIMEOUT_SECONDS,
            )
        )

    app.include_router(router)
    return app
