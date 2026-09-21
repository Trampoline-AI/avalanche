"""Browser-compatible transport and asset serving tests."""

from __future__ import annotations

import http.client
import json
import socket
import struct
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import pytest

from runtime.operator.models import LogEntry, LogLevel, RunState, RunStatus, SequencedLogEntry
from runtime.operator.operator import Operator
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.results import EncodedWorkflowResult, decode_workflow_result
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


def _rest(server, method: str, path: str, body: bytes | None = None):
    connection = http.client.HTTPConnection(server.host, server.port, timeout=5)
    try:
        connection.request(
            method, path, body=body, headers={"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        assert response.getheader("Content-Type").split(";")[0] == "application/json"
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _assert_rest_error(response, status: int, code: str | None = None):
    actual_status, payload = response
    assert actual_status == status
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message"}
    assert isinstance(payload["error"]["message"], str)
    assert payload["error"]["message"]
    assert isinstance(payload["error"]["code"], str)
    assert payload["error"]["code"]
    if code is not None:
        assert payload["error"]["code"] == code


@pytest.mark.parametrize(
    "body",
    (
        b"{",
        b"[]",
        b"{}",
        b'{"workflow_selector":""}',
        b'{"workflow_selector":42}',
        b'{"workflow_selector":"flow","unexpected":true}',
        b'{"workflow_selector":"flow","run_id":null}',
        b'{"workflow_selector":"flow","input_json":[]}',
        b'{"workflow_selector":"flow","context_json":"{}"}',
    ),
)
def test_rest_rejects_invalid_create_documents(proxy, body):
    _, server = proxy
    _assert_rest_error(_rest(server, "POST", "/api/v1/runs", body), 400)
    status, page = _rest(server, "GET", "/api/v1/runs")
    assert status == 200
    assert page["runs"] == []


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/flows?page_size=0",
        "/api/v1/runs?page_size=1.5",
        "/api/v1/runs?page_size=1&page_size=2",
        "/api/v1/runs?unexpected=true",
        "/api/v1/runs?continuation=not-json",
        "/api/v1/runs?continuation=%5B%5D",
        "/api/v1/runs?continuation=%7B%22unknown%22%3A1%7D",
        "/api/v1/runs/missing/activity?order=backward",
        "/api/v1/runs/missing/activity?node_id=a&node_id=b",
    ),
)
def test_rest_rejects_invalid_or_ambiguous_queries(proxy, path):
    _, server = proxy
    _assert_rest_error(_rest(server, "GET", path), 400)


def test_rest_routes_never_fall_back_to_the_spa(proxy):
    _, server = proxy
    for path in ("/api", "/api/v1/unknown", "/api/v1/runs/missing/unknown"):
        _assert_rest_error(_rest(server, "GET", path), 404)
    for method, path in (
        ("POST", "/api/v1/flows"),
        ("DELETE", "/api/v1/runs"),
        ("GET", "/api/v1/runs/missing/cancel"),
        ("POST", "/api/v1/runs/missing/output"),
    ):
        _assert_rest_error(_rest(server, method, path, b"{}"), 405)
    _assert_rest_error(_rest(server, "GET", "/api/v1/runs/missing"), 404, "NOT_FOUND")
    status, page = _rest(server, "GET", "/api/v1/flows")
    assert status == 200
    assert page["flows"] == []
    assert isinstance(page["cursor"]["stream_generation"], str)


@pytest.mark.parametrize(
    ("headers", "status"),
    (
        ({"Content-Type": "text/plain", "Content-Length": "0"}, 415),
        ({"Content-Type": "application/json"}, 411),
        (
            {
                "Content-Type": "application/json",
                "Content-Length": str(4 * 1024 * 1024 + 1),
            },
            413,
        ),
    ),
)
def test_rest_rejects_invalid_body_framing_before_reading(proxy, headers, status):
    _, server = proxy
    connection = http.client.HTTPConnection(server.host, server.port, timeout=5)
    try:
        connection.putrequest("POST", "/api/v1/runs")
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        assert response.getheader("Content-Type").split(";")[0] == "application/json"
        _assert_rest_error((response.status, json.loads(response.read())), status)
    finally:
        connection.close()


def _seed_rest_run(operator: Operator, run_id: str, log_count: int = 0):
    run = RunState(
        run_id=run_id,
        flow_name="flow",
        status=RunStatus.SUCCESS,
        workflow_id="flow.py::flow",
        workflow_display_name="flow",
    )
    entries = [
        SequencedLogEntry(
            sequence=index,
            entry=LogEntry(
                timestamp=datetime(2026, 8, 17, 12, 0, index),
                level=LogLevel.INFO,
                node_id="node",
                message=f"log-{index}",
            ),
            size_bytes=5,
        )
        for index in range(1, log_count + 1)
    ]
    run.latest_log_sequence = log_count
    with operator._lock:
        operator._runs[run_id] = run
        operator._logs[run_id] = entries
    operator._notify_run(run)


def test_rest_run_pages_retain_snapshot_and_reject_changed_filter(proxy):
    operator, server = proxy
    for run_id in ("run-1", "run-2", "run-3"):
        _seed_rest_run(operator, run_id)
    status, first = _rest(server, "GET", "/api/v1/runs?page_size=2")
    assert status == 200
    assert [run["run_id"] for run in first["runs"]] == ["run-3", "run-2"]
    continuation = json.dumps(first["next_page"])
    _seed_rest_run(operator, "run-new")
    query = urlencode({"page_size": 2, "continuation": continuation})
    status, second = _rest(server, "GET", f"/api/v1/runs?{query}")
    assert status == 200
    assert [run["run_id"] for run in second["runs"]] == ["run-1"]
    assert not second.get("next_page", {}).get("continuation_id")
    changed = urlencode({"continuation": continuation, "workflow_selector": "other"})
    _assert_rest_error(
        _rest(server, "GET", f"/api/v1/runs?{changed}"), 409, "FAILED_PRECONDITION"
    )


def test_rest_activity_continuations_bind_run_and_node(proxy):
    operator, server = proxy
    _seed_rest_run(operator, "run-a", log_count=3)
    _seed_rest_run(operator, "run-b", log_count=1)
    status, first = _rest(server, "GET", "/api/v1/runs/run-a/activity?page_size=2")
    assert status == 200
    assert [item["run_sequence"] for item in first["activities"]] == ["1", "2"]
    query = urlencode({"page_size": 2, "continuation": json.dumps(first["next_page"])})
    status, second = _rest(server, "GET", f"/api/v1/runs/run-a/activity?{query}")
    assert status == 200
    assert [item["run_sequence"] for item in second["activities"]] == ["3"]
    assert not second.get("next_page", {}).get("continuation_id")
    for path in (
        f"/api/v1/runs/run-b/activity?{query}",
        f"/api/v1/runs/run-a/activity?{query}&node_id=node",
    ):
        _assert_rest_error(_rest(server, "GET", path), 409, "FAILED_PRECONDITION")
    status, reverse = _rest(
        server, "GET", "/api/v1/runs/run-a/activity?order=newest_first&page_size=2"
    )
    assert status == 200
    assert [item["run_sequence"] for item in reverse["activities"]] == ["3", "2"]


def _wait_for_rest_status(server, run_id: str, expected: str):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status, snapshot = _rest(server, "GET", f"/api/v1/runs/{run_id}")
        assert status == 200
        actual = snapshot["summary"]["status"]
        if actual == expected:
            return snapshot
        assert actual not in {"success", "failed", "cancelled"}, snapshot
        time.sleep(0.05)
    pytest.fail(f"Run {run_id} did not reach {expected}")


def test_rest_live_run_output_cancellation_and_conflicts(tmp_path):
    workflow = tmp_path / "rest_flow.py"
    workflow.write_text(
        "import time\nimport avalanche as ava\n"
        "@ava.source\ndef value():\n"
        "    return {'count': 3, 'ok': True, 'items': [None, 'value']}\n"
        "@ava.workflow\ndef result():\n    return value()\n"
        "@ava.source\ndef slow():\n    time.sleep(60)\n    return 'finished'\n"
        "@ava.workflow\ndef cancellable():\n    return slow()\n"
    )
    operator = Operator([str(workflow)], watch=False, schedule=False, cancel_grace=0.1)
    grpc_server = serve_operator(operator, port=(port := _free_port()), block=False)
    server = start_browser_server(f"127.0.0.1:{port}", port=0, asset_root=tmp_path)
    try:
        status, created = _rest(
            server, "POST", "/api/v1/runs", b'{"workflow_selector":"result"}'
        )
        assert status == 202
        run_id = created["run_id"]
        snapshot = _wait_for_rest_status(server, run_id, "success")
        assert isinstance(snapshot["summary"]["created_sequence"], str)
        status, result = _rest(server, "GET", f"/api/v1/runs/{run_id}/output")
        assert status == 200
        assert isinstance(result["value"]["value_json"], str)
        assert decode_workflow_result(
            EncodedWorkflowResult(value_json=result["value"]["value_json"])
        ) == {"count": 3, "ok": True, "items": [None, "value"]}
        assert result["files"] == []
        duplicate = json.dumps({"workflow_selector": "result", "run_id": run_id}).encode()
        _assert_rest_error(
            _rest(server, "POST", "/api/v1/runs", duplicate), 409, "ALREADY_EXISTS"
        )
        status, _ = _rest(
            server,
            "POST",
            "/api/v1/runs",
            b'{"workflow_selector":"cancellable","run_id":"run-cancel"}',
        )
        assert status == 202
        _assert_rest_error(
            _rest(server, "GET", "/api/v1/runs/run-cancel/output"),
            409,
            "FAILED_PRECONDITION",
        )
        _wait_for_rest_status(server, "run-cancel", "running")
        for content_type in (
            None,
            "text/plain",
            "application/x-www-form-urlencoded",
            "multipart/form-data; boundary=browser-form",
        ):
            headers = {"Origin": "https://unrelated.example"}
            if content_type is not None:
                headers["Content-Type"] = content_type
            connection = http.client.HTTPConnection(server.host, server.port, timeout=5)
            try:
                connection.request(
                    "POST", "/api/v1/runs/run-cancel/cancel", body=b"", headers=headers
                )
                response = connection.getresponse()
                _assert_rest_error((response.status, json.loads(response.read())), 415)
            finally:
                connection.close()
        _wait_for_rest_status(server, "run-cancel", "running")
        status, _ = _rest(server, "POST", "/api/v1/runs/run-cancel/cancel", b"{}")
        assert status == 200
        _wait_for_rest_status(server, "run-cancel", "cancelled")
        _assert_rest_error(
            _rest(server, "GET", "/api/v1/runs/run-cancel/output"),
            409,
            "FAILED_PRECONDITION",
        )
    finally:
        server.close()
        grpc_server.stop(grace=0).wait()
        operator.close()


def test_rest_maps_unavailable_grpc_operator_to_service_unavailable(tmp_path):
    with socket.socket() as upstream:
        upstream.bind(("127.0.0.1", 0))
        port = upstream.getsockname()[1]
        server = start_browser_server(f"127.0.0.1:{port}", port=0, asset_root=tmp_path)
        try:
            _assert_rest_error(_rest(server, "GET", "/api/v1/flows"), 503, "UNAVAILABLE")
        finally:
            server.close()
