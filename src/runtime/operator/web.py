"""In-process REST, gRPC-Web, and static asset listener for the local operator."""

from __future__ import annotations

import logging
import mimetypes
import re
import socket
import struct
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

import anyio
import grpc
import uvicorn
from google.protobuf import message_factory
from google.protobuf.message import DecodeError, Message
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from ._grpc import _BOUNDED_MESSAGE_OPTIONS
from .proto import operator_pb2 as pb
from .rest import create_rest_app

logger = logging.getLogger(__name__)

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 7435
_GRPC_WEB_CONTENT_TYPE = "application/grpc-web+proto"
_GRPC_SERVICE_PATH = "/avalanche.operator.OperatorServiceV2/"
_FRAME_HEADER_BYTES = 5
_MAX_REQUEST_BYTES = 4 * 1024 * 1024
_STARTUP_TIMEOUT_SECONDS = 10.0
_SHUTDOWN_TIMEOUT_SECONDS = 2.0
_HTTP_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"]
_GRPC_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
_OPERATOR_PORT_META = re.compile(
    rb'(<meta\b[^>]*\bcontent=")[^"]+("(?=[^>]*\bdata-avalanche-operator-port\b))'
)


@dataclass(frozen=True)
class _RpcMethod:
    request_type: type[Message]
    response_type: type[Message]
    server_streaming: bool = False


def _rpc_methods() -> MappingProxyType[str, _RpcMethod]:
    service = pb.DESCRIPTOR.services_by_name["OperatorServiceV2"]
    return MappingProxyType(
        {
            descriptor.name: _RpcMethod(
                request_type=message_factory.GetMessageClass(descriptor.input_type),
                response_type=message_factory.GetMessageClass(descriptor.output_type),
                server_streaming=descriptor.server_streaming,
            )
            for descriptor in service.methods
        }
    )


_RPC_METHODS = _rpc_methods()


class _GrpcWebStreamResponse(StreamingResponse):
    def __init__(self, responses: Iterator[Message], call: grpc.Call) -> None:
        self._call = call
        super().__init__(
            _stream_frames(responses), media_type=_GRPC_WEB_CONTENT_TYPE, headers=_GRPC_HEADERS
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # An idle gRPC iterator cannot observe a failed send. Always listen for the
        # ASGI disconnect, including on ASGI 2.4+, and cancel gRPC before waiting
        # for the thread running next() to exit.
        async with anyio.create_task_group() as tasks:

            async def send_stream() -> None:
                try:
                    await self.stream_response(send)
                except OSError:
                    pass
                finally:
                    self._call.cancel()
                    tasks.cancel_scope.cancel()

            tasks.start_soon(send_stream)
            try:
                await self.listen_for_disconnect(receive)
            finally:
                self._call.cancel()
                tasks.cancel_scope.cancel()


async def _stream_frames(responses: Iterator[Message]) -> AsyncIterator[bytes]:
    # A subscription occupies a worker while idle; do not consume the shared
    # thread limiter used by REST requests and asset responses.
    limiter = anyio.CapacityLimiter(1)
    try:
        while (
            response := await anyio.to_thread.run_sync(
                _next_response, responses, limiter=limiter
            )
        ) is not None:
            yield _data_frame(response)
    except grpc.RpcError as exc:
        trailer = _trailer_frame(exc.code(), exc.details())
    except Exception:
        logger.exception("Unhandled gRPC-Web stream proxy failure")
        trailer = _trailer_frame(grpc.StatusCode.INTERNAL, "internal proxy error")
    else:
        trailer = _trailer_frame(grpc.StatusCode.OK, "")
    yield trailer


def _next_response(responses: Iterator[Message]) -> Message | None:
    return next(responses, None)


async def _request_body(request: Request) -> bytes:
    if request.headers.get("transfer-encoding") is not None:
        raise ValueError("Transfer-Encoding is not supported")
    lengths = request.headers.getlist("content-length")
    if not lengths:
        raise ValueError("Content-Length is required")
    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
        raise ValueError("Content-Length must be an integer")
    length = int(lengths[0])
    if length > _MAX_REQUEST_BYTES:
        raise ValueError(f"request body exceeds {_MAX_REQUEST_BYTES} byte limit")
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > length:
            raise ValueError("request body exceeds its Content-Length")
        chunks.append(chunk)
    if received != length:
        raise ValueError("request body is truncated")
    return b"".join(chunks)


def _grpc_response(data: bytes, *, close: bool = False) -> Response:
    headers = {**_GRPC_HEADERS, "Connection": "close"} if close else _GRPC_HEADERS
    return Response(data, media_type=_GRPC_WEB_CONTENT_TYPE, headers=headers)


def _api_root(request: Request) -> Response:
    return JSONResponse(
        {"error": {"code": "NOT_FOUND", "message": "Unknown API route"}},
        status_code=HTTPStatus.NOT_FOUND,
        headers={**_GRPC_HEADERS, "Connection": "close"},
    )


def _options_response() -> Response:
    return Response(status_code=HTTPStatus.NO_CONTENT, headers={"Allow": "GET, POST, OPTIONS"})


class _OptionsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "OPTIONS":
            await _options_response()(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def _serve_asset(request: Request, asset_root: Path, operator_port: int) -> Response:
    if request.method != "GET":
        status = (
            HTTPStatus.NOT_FOUND if request.method == "POST" else HTTPStatus.METHOD_NOT_ALLOWED
        )
        return Response(status_code=status)
    # ASGI paths have already been percent-decoded by the HTTP server.
    relative = request.url.path.lstrip("/") or "index.html"
    candidate = (asset_root / relative).resolve()
    if not candidate.is_relative_to(asset_root):
        return Response(status_code=HTTPStatus.NOT_FOUND)
    if not candidate.is_file() and "." not in Path(relative).name:
        candidate = (asset_root / "index.html").resolve()
    if not candidate.is_relative_to(asset_root) or not candidate.is_file():
        return Response(status_code=HTTPStatus.NOT_FOUND)
    data = candidate.read_bytes()
    if candidate.name == "index.html":
        data = _OPERATOR_PORT_META.sub(
            rb"\g<1>" + str(operator_port).encode() + rb"\g<2>", data, count=1
        )
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    return Response(
        data,
        headers={
            "Content-Type": content_type,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": (
                "no-store"
                if candidate.name == "index.html"
                else "public, max-age=31536000, immutable"
            ),
        },
    )


def _create_browser_app(
    channel: grpc.Channel, asset_root: Path, operator_port: int
) -> Starlette:
    async def grpc_web(request: Request) -> Response:
        method_name = request.path_params["method_name"]
        method = _RPC_METHODS.get(method_name)
        if method is None:
            return Response(status_code=HTTPStatus.NOT_FOUND)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != _GRPC_WEB_CONTENT_TYPE
        ):
            return Response(
                status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE, headers={"Connection": "close"}
            )
        try:
            message = _decode_request(await _request_body(request), method)
        except (DecodeError, ValueError) as exc:
            return _grpc_response(
                _trailer_frame(grpc.StatusCode.INVALID_ARGUMENT, str(exc)), close=True
            )
        try:
            if method.server_streaming:
                stream = channel.unary_stream(
                    f"{_GRPC_SERVICE_PATH}{method_name}",
                    request_serializer=_serialize_message,
                    response_deserializer=method.response_type.FromString,
                )(message)
                if not isinstance(stream, grpc.Call):
                    raise TypeError("A gRPC response stream must support cancellation")
                return _GrpcWebStreamResponse(stream, stream)
            call = channel.unary_unary(
                f"{_GRPC_SERVICE_PATH}{method_name}",
                request_serializer=_serialize_message,
                response_deserializer=method.response_type.FromString,
            )
            response = await run_in_threadpool(call, message)
            return _grpc_response(
                _data_frame(response) + _trailer_frame(grpc.StatusCode.OK, "")
            )
        except grpc.RpcError as exc:
            return _grpc_response(_trailer_frame(exc.code(), exc.details()))
        except Exception:
            logger.exception("Unhandled gRPC-Web proxy failure: %s", method_name)
            return _grpc_response(
                _trailer_frame(grpc.StatusCode.INTERNAL, "internal proxy error")
            )

    def asset(request: Request) -> Response:
        return _serve_asset(request, asset_root, operator_port)

    app = Starlette(
        routes=[
            Route("/api", _api_root, methods=_HTTP_METHODS),
            Mount("/api", app=create_rest_app(channel)),
            Route(f"{_GRPC_SERVICE_PATH}{{method_name}}", grpc_web, methods=["POST"]),
            Route("/{path:path}", asset, methods=_HTTP_METHODS),
        ],
        middleware=[Middleware(_OptionsMiddleware)],
    )
    app.router.redirect_slashes = False
    return app


class _BrowserUvicornServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = threading.Event()
        self.failure: BaseException | None = None

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The owning CLI, not this background listener, owns process signals.
        yield

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        self.ready.set()


class BrowserServer:
    """Owned browser listener serving the SPA and proxying gRPC-Web to an operator."""

    def __init__(
        self, server: _BrowserUvicornServer, listener: socket.socket, channel: grpc.Channel
    ) -> None:
        self._server = server
        self._listener = listener
        self._channel = channel
        address = listener.getsockname()
        self._host: str = address[0]
        self._port: int = address[1]
        self._close_lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="avalanche-browser-listener", daemon=True
        )

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def endpoint(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def _run(self) -> None:
        try:
            self._server.run(sockets=[self._listener])
        except BaseException as exc:
            self._server.failure = exc
        finally:
            self._server.ready.set()

    def _start(self) -> None:
        self._thread.start()
        if not self._server.ready.wait(timeout=_STARTUP_TIMEOUT_SECONDS):
            raise TimeoutError("Browser listener did not become ready")
        if self._server.failure is not None:
            raise self._server.failure
        if not self._server.started:
            raise RuntimeError("Browser listener stopped before becoming ready")

    def wait(self) -> None:
        while self._thread.is_alive():
            self._thread.join(timeout=0.1)
        if self._server.failure is not None:
            raise self._server.failure

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._server.should_exit = True
        try:
            # Closing the shared channel cancels REST calls and idle subscriptions
            # before Uvicorn waits for their request tasks and worker threads.
            self._channel.close()
        finally:
            try:
                if self._thread.ident is not None:
                    self._thread.join(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
                    if self._thread.is_alive():
                        self._server.force_exit = True
                        self._thread.join(timeout=0.5)
            finally:
                self._listener.close()


def _bind_listener(host: str, port: int) -> socket.socket:
    normalized = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    family = socket.AF_INET6 if ":" in normalized else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((normalized, port))
        listener.listen(socket.SOMAXCONN)
    except BaseException as failure:
        try:
            listener.close()
        except BaseException as exc:
            failure.add_note(f"HTTP socket cleanup also failed: {exc}")
        raise
    return listener


def start_browser_server(
    operator_address: str,
    *,
    host: str = DEFAULT_WEB_HOST,
    port: int = DEFAULT_WEB_PORT,
    asset_root: Path | None = None,
    trust_non_loopback: bool = False,
) -> BrowserServer:
    """Start the loopback-default browser listener for a remote operator."""
    if not _is_loopback_host(host) and not trust_non_loopback:
        raise ValueError(
            "Non-loopback web UI exposure requires ava web --trusted-proxy and an "
            "external trusted, authenticated boundary"
        )
    root = (asset_root or Path(__file__).with_name("web_assets")).resolve()
    operator_port = _operator_port(operator_address)
    listener = _bind_listener(host, port)
    channel: grpc.Channel | None = None
    browser_server: BrowserServer | None = None
    try:
        channel = grpc.insecure_channel(operator_address, options=_BOUNDED_MESSAGE_OPTIONS)
        app = _create_browser_app(channel, root, operator_port)
        config = uvicorn.Config(
            app,
            loop="asyncio",
            http="h11",
            ws="none",
            lifespan="off",
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            timeout_graceful_shutdown=1.0,
        )
        browser_server = BrowserServer(_BrowserUvicornServer(config), listener, channel)
        browser_server._start()
    except BaseException as failure:
        try:
            if browser_server is not None:
                browser_server.close()
            else:
                try:
                    if channel is not None:
                        channel.close()
                finally:
                    listener.close()
        except BaseException as exc:
            failure.add_note(f"Browser listener cleanup also failed: {exc}")
        raise
    logger.info(
        "Browser UI listening on %s for operator %s",
        browser_server.endpoint,
        operator_address,
    )
    return browser_server


def _decode_request(data: bytes, method: _RpcMethod) -> Message:
    if len(data) < _FRAME_HEADER_BYTES:
        raise ValueError("gRPC-Web request frame is truncated")
    flags, length = struct.unpack(">BI", data[:_FRAME_HEADER_BYTES])
    if flags != 0:
        raise ValueError("compressed or trailer request frames are unsupported")
    if length != len(data) - _FRAME_HEADER_BYTES:
        raise ValueError("gRPC-Web request frame length does not match its payload")
    request = method.request_type()
    request.ParseFromString(data[_FRAME_HEADER_BYTES:])
    return request


def _serialize_message(message: Message) -> bytes:
    return message.SerializeToString()


def _data_frame(message: Message) -> bytes:
    payload = message.SerializeToString()
    return struct.pack(">BI", 0, len(payload)) + payload


def _trailer_frame(code: grpc.StatusCode, detail: str) -> bytes:
    status = code.value[0]
    safe_detail = detail.replace("\r", " ").replace("\n", " ")
    payload = f"grpc-status: {status}\r\ngrpc-message: {safe_detail}\r\n".encode()
    return struct.pack(">BI", 0x80, len(payload)) + payload


def _is_loopback_host(host: str) -> bool:
    normalized = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if normalized.lower() == "localhost" or normalized == "::1":
        return True
    try:
        return socket.gethostbyname(normalized).startswith("127.")
    except OSError:
        return False


def _operator_port(address: str) -> int:
    try:
        port = urlsplit(f"//{address}").port
    except ValueError as exc:
        raise ValueError("Operator address must use HOST:PORT") from exc
    if port is None:
        raise ValueError("Operator address must use HOST:PORT")
    return port


__all__ = [
    "BrowserServer",
    "DEFAULT_WEB_HOST",
    "DEFAULT_WEB_PORT",
    "start_browser_server",
]
