"""Browser-compatible transport and asset serving tests."""

from __future__ import annotations

import http.client
import socket
import struct
from pathlib import Path

import pytest

from runtime.operator.operator import Operator
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.server import serve as serve_operator
from runtime.operator.web import start_browser_server

_SERVICE = "/avalanche.operator.OperatorService/"
_CONTENT_TYPE = "application/grpc-web+proto"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _frame(message) -> bytes:
    payload = message.SerializeToString()
    return struct.pack(">BI", 0, len(payload)) + payload


def _frames(body: bytes) -> list[tuple[int, bytes]]:
    frames = []
    offset = 0
    while offset < len(body):
        flags, length = struct.unpack(">BI", body[offset : offset + 5])
        offset += 5
        frames.append((flags, body[offset : offset + length]))
        offset += length
    assert offset == len(body)
    return frames


def _post(server, method: str, request) -> tuple[int, str, bytes]:
    connection = http.client.HTTPConnection(server.host, server.port, timeout=5)
    body = _frame(request)
    connection.request(
        "POST",
        f"{_SERVICE}{method}",
        body=body,
        headers={"Content-Type": _CONTENT_TYPE},
    )
    response = connection.getresponse()
    result = response.status, response.getheader("Content-Type"), response.read()
    connection.close()
    return result


def test_browser_listener_proxies_unary_catalog_from_remote_operator(tmp_path: Path):
    (tmp_path / "index.html").write_text("<main>Avalanche</main>")
    operator = Operator([], watch=False, schedule=False)
    grpc_port = _free_port()
    grpc_server = serve_operator(operator, port=grpc_port, block=False)
    server = start_browser_server(f"127.0.0.1:{grpc_port}", port=0, asset_root=tmp_path)
    try:
        status, content_type, body = _post(server, "GetCatalog", pb.Empty())
        frames = _frames(body)
        catalog = pb.CatalogSnapshotMsg.FromString(frames[0][1])

        assert status == 200
        assert content_type == _CONTENT_TYPE
        assert catalog.operator_instance_id == operator.operator_instance_id
        assert catalog.as_of_sequence == operator.current_sequence
        assert frames[1][0] == 0x80
        assert b"grpc-status: 0" in frames[1][1]
    finally:
        server.close()
        grpc_server.stop(grace=0)
        operator.close()


def test_browser_listener_proxies_stream_reset_from_remote_operator(tmp_path: Path):
    (tmp_path / "index.html").write_text("<main>Avalanche</main>")
    operator = Operator([], watch=False, schedule=False)
    grpc_port = _free_port()
    grpc_server = serve_operator(operator, port=grpc_port, block=False)
    server = start_browser_server(f"127.0.0.1:{grpc_port}", port=0, asset_root=tmp_path)
    try:
        status, _, body = _post(
            server,
            "StreamOperatorUpdates",
            pb.StreamOperatorUpdatesRequest(
                operator_instance_id="stale-operator",
                after_sequence=42,
            ),
        )
        frames = _frames(body)
        envelope = pb.OperatorUpdateEnvelope.FromString(frames[0][1])

        assert status == 200
        assert envelope.operator_instance_id == operator.operator_instance_id
        assert envelope.HasField("reset_required")
        assert frames[-1][0] == 0x80
        assert b"grpc-status: 0" in frames[-1][1]
    finally:
        server.close()
        grpc_server.stop(grace=0)
        operator.close()


def test_browser_listener_serves_assets_without_a_running_operator(tmp_path: Path):
    (tmp_path / "index.html").write_text("<main>Avalanche</main>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('loaded')")
    server = start_browser_server("127.0.0.1:7433", port=0, asset_root=tmp_path)
    try:
        connection = http.client.HTTPConnection(server.host, server.port, timeout=5)
        connection.request("GET", "/runs/run-1")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b"<main>Avalanche</main>"

        connection.request("GET", "/assets/app.js")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/javascript"
        assert response.read() == b"console.log('loaded')"
        connection.close()
    finally:
        server.close()


def test_browser_listener_serves_packaged_operator_application():
    server = start_browser_server("127.0.0.1:7433", port=0)
    try:
        connection = http.client.HTTPConnection(server.host, server.port, timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read()

        assert response.status == 200
        assert response.getheader("Content-Type") == "text/html"
        assert b"<title>Avalanche Operator</title>" in body
        assert b'id="root"' in body
        connection.close()
    finally:
        server.close()


def test_browser_listener_rejects_non_loopback_without_trusted_proxy(tmp_path: Path):
    with pytest.raises(ValueError, match="trusted, authenticated boundary"):
        start_browser_server(
            "127.0.0.1:7433",
            host="0.0.0.0",
            port=0,
            asset_root=tmp_path,
        )
