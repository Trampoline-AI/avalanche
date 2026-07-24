"""Loopback HTTP ingress for locally discovered workflow webhooks."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

from .models import WorkflowDescriptor

if TYPE_CHECKING:
    from .operator import Operator

DEFAULT_WEBHOOK_PORT = 7434
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


@dataclass(frozen=True)
class WebhookRoute:
    workflow_id: str
    path: str


def routes_for(descriptors: tuple[WorkflowDescriptor, ...]) -> dict[str, WebhookRoute]:
    """Build a complete route table or reject it before it can be published."""
    routes: dict[str, WebhookRoute] = {}
    for descriptor in descriptors:
        if not descriptor.webhook_enabled:
            continue
        path = descriptor.webhook_path or default_path(descriptor)
        if path in routes:
            raise ValueError(
                f"Webhook route collision for {path}: {routes[path].workflow_id} and "
                f"{descriptor.workflow_id}"
            )
        routes[path] = WebhookRoute(descriptor.workflow_id, path)
    return dict(sorted(routes.items()))


def default_path(descriptor: WorkflowDescriptor) -> str:
    relative_parts = descriptor.locator.relative_file.removesuffix(".py").split("/")
    parts = [descriptor.locator.root_alias, *relative_parts, descriptor.locator.builder_symbol]
    return "/webhooks/" + "/".join(quote(part, safe="") for part in parts)


class WebhookServer:
    """One small HTTP server whose route snapshot is owned by the Operator."""

    def __init__(self, operator: Operator, port: int) -> None:
        self._operator = operator
        self._port = port
        self._lock = threading.RLock()
        self._routes: dict[str, WebhookRoute] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        with self._lock:
            return self._server is not None

    @property
    def port(self) -> int | None:
        with self._lock:
            return self._server.server_address[1] if self._server is not None else None

    def url_for(self, path: str) -> str | None:
        port = self.port
        return f"http://127.0.0.1:{port}{path}" if port is not None else None

    def reconcile(self, routes: dict[str, WebhookRoute]) -> None:
        with self._lock:
            self._routes = dict(routes)
            if routes and self._server is None:
                self._start_locked()

    def close(self) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
            self._routes = {}
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _start_locked(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                owner._handle(self)

            def do_GET(self) -> None:  # noqa: N802
                owner._method_not_allowed(self)

            def do_PUT(self) -> None:  # noqa: N802
                owner._method_not_allowed(self)

            def do_PATCH(self) -> None:  # noqa: N802
                owner._method_not_allowed(self)

            def do_DELETE(self) -> None:  # noqa: N802
                owner._method_not_allowed(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="avalanche-webhooks",
            daemon=True,
        )
        self._thread.start()

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        request_path = urlsplit(handler.path).path
        with self._lock:
            route = self._routes.get(request_path)
        if route is None:
            self._reply(handler, HTTPStatus.NOT_FOUND, {"error": "unknown webhook route"})
            return
        content_type = handler.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._reply(
                handler,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "Content-Type must be application/json"},
            )
            return
        try:
            length = int(handler.headers.get("Content-Length", ""))
        except ValueError:
            self._reply(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
            return
        if length < 0 or length > MAX_WEBHOOK_BODY_BYTES:
            self._reply(
                handler,
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": "request body too large"},
            )
            return
        try:
            body: Any = json.loads(handler.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply(
                handler,
                HTTPStatus.BAD_REQUEST,
                {"error": "request body must be valid JSON"},
            )
            return
        if not isinstance(body, dict):
            self._reply(
                handler,
                HTTPStatus.BAD_REQUEST,
                {"error": "request body must be a JSON object"},
            )
            return
        try:
            run_id = self._operator.start_run(
                route.workflow_id, input=body, triggered_by="webhook"
            )
        except Exception:
            self._reply(
                handler,
                HTTPStatus.CONFLICT,
                {"error": "workflow could not be started"},
            )
            return
        self._reply(handler, HTTPStatus.ACCEPTED, {"run_id": run_id})

    def _method_not_allowed(self, handler: BaseHTTPRequestHandler) -> None:
        self._reply(handler, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "POST is required"})

    @staticmethod
    def _reply(
        handler: BaseHTTPRequestHandler, status: HTTPStatus, body: dict[str, str]
    ) -> None:
        encoded = json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(encoded)))
        handler.end_headers()
        handler.wfile.write(encoded)
