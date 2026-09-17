"""Optional real-worker classifier serialization and process-local evidence regression."""

from __future__ import annotations

import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import avalanche as ava
from avalanche.classifier import capture_classifier_evidence

pytestmark = pytest.mark.ray


@pytest.fixture(scope="module")
def ray_runtime():
    ray = pytest.importorskip("ray")
    owns_runtime = not ray.is_initialized()
    if owns_runtime:
        ray.init(num_cpus=2, include_dashboard=False)
    yield ray
    if owns_runtime:
        ray.shutdown()


@pytest.fixture
def typesafe_http_service():
    requests = queue.Queue()
    response = json.dumps(
        {
            "model": "jev-worker-resolved",
            "answers": {"urgent": {"type": "noul", "noul": 0.93}},
            "usage": {"input_tokens": 8, "output_tokens": 2},
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler method
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.put((self.path, self.headers.get("Authorization"), body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_workflow_bound_callable_uses_worker_credentials_and_capture(
    ray_runtime, typesafe_http_service, monkeypatch
):
    from ray import cloudpickle

    monkeypatch.setenv("TYPESAFE_API_KEY", "driver-must-not-be-used")

    @ava.classifier_step(
        questions={
            "urgent": {"type": "noul", "instructions": "Does this require action today?"},
        }
    )
    async def classify(ticket, *, classifier: ava.Classifier):
        first = await classifier(state=ticket)
        second = await classifier(state={"followup": ticket})
        return first, second

    bound = classify.fn.__classifier_step__.with_workflow_defaults(
        classify.fn, {"model": "worker-request-model", "timeout": 5.0}
    )
    parent_records = []
    lock = threading.Lock()

    def parent_listener(record):
        with lock:
            parent_records.append(record)

    @ray_runtime.remote
    def execute(serialized_callable, base_url):
        import asyncio
        import os

        from ray import cloudpickle

        from avalanche.classifier import capture_classifier_evidence

        previous = {
            name: os.environ.get(name) for name in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL")
        }
        os.environ["TYPESAFE_API_KEY"] = "worker-only-key"
        os.environ["TYPESAFE_BASE_URL"] = base_url
        records = []
        try:
            restored = cloudpickle.loads(serialized_callable)
            with capture_classifier_evidence(records.append):
                results = asyncio.run(restored("private-worker-ticket"))
            return (
                os.getpid(),
                [result.model_dump(mode="json") for result in results],
                [record.model_dump(mode="json") for record in records],
            )
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    base_url, requests = typesafe_http_service
    with capture_classifier_evidence(parent_listener):
        serialized = cloudpickle.dumps(bound)
        worker_pid, results, records = ray_runtime.get(
            execute.remote(serialized, base_url), timeout=30
        )

    assert worker_pid != os.getpid()
    assert parent_records == []
    assert [result["answers"]["urgent"]["noul"] for result in results] == [0.93, 0.93]
    assert [record["status"] for record in records] == [
        "running",
        "success",
        "running",
        "success",
    ]
    assert records[0]["invocation_id"] == records[1]["invocation_id"]
    assert records[2]["invocation_id"] == records[3]["invocation_id"]
    assert records[0]["invocation_id"] != records[2]["invocation_id"]
    assert [records[0]["invocation_index"], records[2]["invocation_index"]] == [0, 1]
    assert records[1]["result"] == results[0]
    assert records[3]["result"] == results[1]
    evidence = json.dumps(records)
    assert "worker-only-key" not in evidence
    assert "driver-must-not-be-used" not in evidence
    for index, expected_input in enumerate(
        ("private-worker-ticket", {"followup": "private-worker-ticket"})
    ):
        path, authorization, body = requests.get(timeout=5)
        assert path == "/v1/systemone"
        assert authorization == "Bearer worker-only-key"
        assert body["model"] == "worker-request-model"
        assert body["state"] == expected_input
        assert records[2 * index]["input"] == records[2 * index + 1]["input"] == body["state"]
    assert requests.empty()
