"""Browser-compatible transport and asset serving tests."""

from __future__ import annotations

import http.client
import socket
import struct
import time
from pathlib import Path

import pytest

from runtime.operator.operator import Operator
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.server import serve as serve_operator
from runtime.operator.web import start_browser_server

_SERVICE = "/avalanche.operator.OperatorServiceV2/"
_CONTENT_TYPE = "application/grpc-web+proto"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _subscriber_count(operator: Operator) -> int:
    with operator._lock:
        return len(operator._update_subscribers)


def _wait_for_subscriber_count(operator: Operator, expected: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _subscriber_count(operator) == expected:
            return True
        time.sleep(0.01)
    return _subscriber_count(operator) == expected


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


@pytest.fixture
def proxy(tmp_path):
    (tmp_path / "index.html").write_text("<main>Avalanche</main>")
    operator = Operator([], watch=False, schedule=False)
    grpc_server = serve_operator(operator, port=(port := _free_port()), block=False)
    server = start_browser_server(f"127.0.0.1:{port}", port=0, asset_root=tmp_path)
    try:
        yield operator, server
    finally:
        server.close()
        grpc_server.stop(grace=0).wait()
        operator.close()


def test_browser_proxy_preserves_unary_errors_and_stream_reset(proxy):
    operator, server = proxy
    status, content_type, body = _post(server, "DiscoverFlows", pb.DiscoverFlowsRequestV2())
    frames = _frames(body)
    flow_list = pb.FlowListV2.FromString(frames[0][1])
    assert status == 200
    assert content_type == _CONTENT_TYPE
    assert flow_list.scope_ref.reference == operator.operator_instance_id
    assert frames[-1][0] == 0x80
    assert b"grpc-status: 0" in frames[-1][1]

    _, _, body = _post(server, "GetRunSnapshot", pb.GetRunSnapshotRequestV2(run_id="missing"))
    assert b"grpc-status: 5" in _frames(body)[-1][1]
    status, _, body = _post(
        server,
        "WatchRunStatus",
        pb.WatchRunStatusRequestV2(
            after_cursor=pb.LifecycleCursorV2(
                stream="operator-events",
                topology_fingerprint="foreign-topology",
                stream_generation=42,
                retained_floor_event_ulid="00000000000000000000000001",
                event_ulid="00000000000000000000000002",
            )
        ),
    )
    frames = _frames(body)
    assert status == 200
    assert pb.RunStatusEnvelopeV2.FromString(frames[0][1]).HasField("reset_required")
    assert b"grpc-status: 0" in frames[-1][1]


def test_browser_disconnect_cancels_idle_upstream_stream(proxy):
    operator, server = proxy
    connection = http.client.HTTPConnection(server.host, server.port, timeout=5)
    try:
        connection.request(
            "POST",
            f"{_SERVICE}WatchRunStatus",
            body=_frame(pb.WatchRunStatusRequestV2()),
            headers={"Content-Type": _CONTENT_TYPE},
        )
        assert connection.getresponse().status == 200
        assert _wait_for_subscriber_count(operator, 1)
    finally:
        connection.close()
    assert _wait_for_subscriber_count(operator, 0)


def test_asset_and_request_boundaries_reject_unsafe_http(tmp_path: Path):
    asset_root = tmp_path / "public"
    asset_root.mkdir()
    (asset_root / "index.html").write_text("<main>Application</main>")
    (asset_root / "app.js").write_text("export default 42")
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (asset_root / "escaped.txt").symlink_to(secret)
    with pytest.raises(ValueError):
        start_browser_server("127.0.0.1:7433", host="0.0.0.0", port=0, asset_root=asset_root)
    server = start_browser_server("127.0.0.1:1", port=0, asset_root=asset_root)
    try:
        for path, status, body in (
            ("/runs/run-1", 200, b"<main>Application</main>"),
            ("/app.js", 200, b"export default 42"),
            ("/%2e%2e/secret.txt", 404, None),
            ("/escaped.txt", 404, None),
        ):
            connection = http.client.HTTPConnection(server.host, server.port, timeout=5)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                assert response.status == status
                if body is not None:
                    assert response.read() == body
                else:
                    assert b"private" not in response.read()
            finally:
                connection.close()

        for content_type, body, length, status in (
            ("application/json", b"{}", "2", 415),
            (_CONTENT_TYPE, b"broken", "6", 200),
            (_CONTENT_TYPE, b"", str(4 * 1024 * 1024 + 1), 200),
        ):
            connection = http.client.HTTPConnection(server.host, server.port, timeout=5)
            try:
                connection.request(
                    "POST",
                    f"{_SERVICE}DiscoverFlows",
                    body=body,
                    headers={
                        "Content-Type": content_type,
                        "Content-Length": length,
                    },
                )
                response = connection.getresponse()
                assert response.status == status
                if status == 200:
                    assert b"grpc-status: 3" in _frames(response.read())[-1][1]
            finally:
                connection.close()
    finally:
        server.close()
