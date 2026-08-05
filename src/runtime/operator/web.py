"""In-process gRPC-Web and static asset listener for the local operator UI."""

from __future__ import annotations

import logging
import mimetypes
import select
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

import grpc
from google.protobuf import message_factory
from google.protobuf.message import DecodeError, Message

from .operator import Operator
from .proto import operator_pb2 as pb
from .server import OperatorServicer

logger = logging.getLogger(__name__)

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 7435
_GRPC_WEB_CONTENT_TYPE = "application/grpc-web+proto"
_GRPC_SERVICE_PATH = "/avalanche.operator.OperatorService/"
_FRAME_HEADER_BYTES = 5
_MAX_REQUEST_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class _RpcMethod:
    request_type: type[Message]
    response_type: type[Message]
    server_streaming: bool = False


def _rpc_methods() -> MappingProxyType[str, _RpcMethod]:
    service = pb.DESCRIPTOR.services_by_name["OperatorService"]
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


class _WebRpcAbortError(Exception):
    def __init__(self, code: grpc.StatusCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _WebRpcContext:
    def __init__(self, active: Callable[[], bool]) -> None:
        self._active = active

    def abort(self, code: grpc.StatusCode, detail: str) -> None:
        raise _WebRpcAbortError(code, detail)

    def is_active(self) -> bool:
        return self._active()

    def send_initial_metadata(self, metadata: tuple[()]) -> None:
        del metadata


class _BrowserHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        operator: Operator,
        asset_root: Path,
    ) -> None:
        self.operator_servicer = OperatorServicer(operator)
        self.asset_root = asset_root
        self.stopping = threading.Event()
        super().__init__(server_address, _BrowserRequestHandler)


class _BrowserRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _BrowserHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        self._serve_asset()

    def do_POST(self) -> None:  # noqa: N802
        method_name = urlsplit(self.path).path.removeprefix(_GRPC_SERVICE_PATH)
        method = _RPC_METHODS.get(method_name)
        if method is None or urlsplit(self.path).path != f"{_GRPC_SERVICE_PATH}{method_name}":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = self.headers.get_content_type()
        if content_type != _GRPC_WEB_CONTENT_TYPE:
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            request = _decode_request(self.rfile.read(self._request_content_length()), method)
        except (DecodeError, ValueError) as exc:
            self._send_grpc_error(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return
        context = _WebRpcContext(self._request_is_active)
        handler = getattr(self.server.operator_servicer, method_name)
        try:
            result = handler(request, context)
            if method.server_streaming:
                self._send_stream(iter(result))
            else:
                self._send_unary(result)
        except _WebRpcAbortError as exc:
            self._send_grpc_error(exc.code, exc.detail)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            logger.exception("Unhandled gRPC-Web method failure: %s", method_name)
            self._send_grpc_error(grpc.StatusCode.INTERNAL, "internal operator error")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _request_content_length(self) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise ValueError("Content-Length is required")
        try:
            length = int(raw)
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if not 0 <= length <= _MAX_REQUEST_BYTES:
            raise ValueError(f"request body exceeds {_MAX_REQUEST_BYTES} byte limit")
        return length

    def _request_is_active(self) -> bool:
        if self.server.stopping.is_set():
            return False
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return True
            return bool(self.connection.recv(1, socket.MSG_PEEK))
        except OSError:
            return False

    def _send_unary(self, response: Message) -> None:
        body = _data_frame(response) + _trailer_frame(grpc.StatusCode.OK, "")
        self.send_response(HTTPStatus.OK)
        self._send_grpc_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stream(self, responses: Iterator[Message]) -> None:
        self.send_response(HTTPStatus.OK)
        self._send_grpc_headers()
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            try:
                for response in responses:
                    self._write_chunk(_data_frame(response))
            except _WebRpcAbortError as exc:
                trailer = _trailer_frame(exc.code, exc.detail)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                logger.exception("Unhandled gRPC-Web stream failure")
                trailer = _trailer_frame(grpc.StatusCode.INTERNAL, "internal operator error")
            else:
                trailer = _trailer_frame(grpc.StatusCode.OK, "")
            try:
                self._write_chunk(trailer)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
        finally:
            close = getattr(responses, "close", None)
            if close is not None:
                close()

    def _send_grpc_error(self, code: grpc.StatusCode, detail: str) -> None:
        if self.wfile.closed:
            return
        body = _trailer_frame(code, detail)
        self.send_response(HTTPStatus.OK)
        self._send_grpc_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_grpc_headers(self) -> None:
        self.send_header("Content-Type", _GRPC_WEB_CONTENT_TYPE)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _serve_asset(self) -> None:
        request_path = unquote(urlsplit(self.path).path)
        relative = request_path.lstrip("/") or "index.html"
        candidate = (self.server.asset_root / relative).resolve()
        try:
            candidate.relative_to(self.server.asset_root)
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file() and "." not in Path(relative).name:
            candidate = self.server.asset_root / "index.html"
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if candidate.name == "index.html":
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("Browser listener: " + format, *args)


class BrowserServer:
    """Owned browser listener serving gRPC-Web and the compiled SPA."""

    def __init__(self, server: _BrowserHTTPServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def host(self) -> str:
        return str(self._server.server_address[0])

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def endpoint(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def close(self) -> None:
        if self._server.stopping.is_set():
            return
        self._server.stopping.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


def start_browser_server(
    operator: Operator,
    *,
    host: str = DEFAULT_WEB_HOST,
    port: int = DEFAULT_WEB_PORT,
    asset_root: Path | None = None,
    trust_non_loopback: bool = False,
) -> BrowserServer:
    """Start the loopback-default browser listener for one shared operator."""
    if not _is_loopback_host(host) and not trust_non_loopback:
        raise ValueError(
            "Non-loopback web UI exposure requires --web-trusted-proxy and an external "
            "trusted, authenticated boundary"
        )
    root = (asset_root or Path(__file__).with_name("web_assets")).resolve()
    server = _BrowserHTTPServer((host, port), operator, root)
    thread = threading.Thread(
        target=server.serve_forever,
        name="avalanche-browser-listener",
        daemon=True,
    )
    thread.start()
    logger.info("Operator web UI listening on %s", BrowserServer(server, thread).endpoint)
    return BrowserServer(server, thread)


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
    if normalized.lower() == "localhost":
        return True
    try:
        return socket.gethostbyname(normalized).startswith("127.") or normalized == "::1"
    except OSError:
        return False


__all__ = [
    "BrowserServer",
    "DEFAULT_WEB_HOST",
    "DEFAULT_WEB_PORT",
    "start_browser_server",
]
