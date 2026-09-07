"""Real webhook ingress, route ownership, and HTTP rejection boundaries."""

import http.client
import json
import time
from urllib.parse import urlsplit

import pytest

from runtime.operator import Operator
from runtime.operator.models import RunStatus, WorkflowDescriptor, WorkflowLocator
from runtime.operator.webhooks import MAX_WEBHOOK_BODY_BYTES, routes_for


def _descriptor(
    *, workflow_id: str = "root/reports/daily.py::shared", path: str | None = None
) -> WorkflowDescriptor:
    return WorkflowDescriptor(
        workflow_id=workflow_id,
        display_name="shared",
        locator=WorkflowLocator("root", "reports/daily.py", "shared"),
        node_ids=(),
        graph=(),
        node_types=(),
        display_names=(),
        webhook_enabled=True,
        webhook_path=path,
    )


def test_default_routes_are_hierarchical_and_collisions_reject_the_catalog():
    route = routes_for((_descriptor(),))
    assert list(route) == ["/webhooks/root/reports/daily/shared"]

    with pytest.raises(ValueError):
        routes_for(
            (
                _descriptor(path="/same"),
                _descriptor(workflow_id="other.py::flow", path="/same"),
            )
        )


def test_webhook_executes_json_input_and_rejects_invalid_or_removed_routes(tmp_path):
    workflow_path = tmp_path / "ingress.py"
    workflow_path.write_text(
        """import avalanche as ava

class Input(ava.BaseInput):
    message: str

@ava.source
def capture(payload: Input):
    return payload.message

@ava.workflow(input=Input, webhook=ava.Webhook(path="/ingest"))
def ingress():
    return capture()
"""
    )
    operator = Operator([str(workflow_path)], schedule=False, watch=False, webhook_port=0)
    try:
        flow = operator.list_workflows()[0]
        address = urlsplit(flow.webhook_url)
        assert address.hostname == "127.0.0.1"

        def request(
            method, body=b"", *, path="/ingest", content_type="application/json", length=None
        ):
            connection = http.client.HTTPConnection(address.hostname, address.port, timeout=5)
            try:
                headers = {"Content-Type": content_type}
                if length is not None:
                    headers["Content-Length"] = str(length)
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        status, body = request("POST", b'{"message":"delivered"}', path="/ingest?source=test")
        assert status == 202
        run_id = body["run_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            run = operator.get_run(run_id)
            if run.status in (RunStatus.SUCCESS, RunStatus.FAILED):
                break
            time.sleep(0.02)
        assert run.status is RunStatus.SUCCESS
        assert operator.get_run_result(run_id) == "delivered"
        assert run.triggered_by == "webhook"

        for method, payload, content_type, length, expected in (
            ("GET", b"", "application/json", None, 405),
            ("POST", b"{}", "text/plain", None, 415),
            ("POST", b"not json", "application/json", None, 400),
            ("POST", b"[]", "application/json", None, 400),
            ("POST", b"", "application/json", MAX_WEBHOOK_BODY_BYTES + 1, 413),
        ):
            status, _ = request(method, payload, content_type=content_type, length=length)
            assert status == expected
        assert [run.run_id for run in operator.list_runs(flow.selector)] == [run_id]

        operator._webhooks.reconcile({})
        assert request("POST", b'{"message":"removed"}')[0] == 404
        assert [run.run_id for run in operator.list_runs(flow.selector)] == [run_id]
    finally:
        operator.close()
