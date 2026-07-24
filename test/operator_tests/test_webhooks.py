from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from avalanche import Webhook, Workflow, workflow
from runtime.operator.models import WorkflowDescriptor, WorkflowLocator
from runtime.operator.webhooks import WebhookServer, routes_for


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

    with pytest.raises(ValueError, match="Webhook route collision"):
        routes_for(
            (
                _descriptor(path="/same"),
                _descriptor(workflow_id="other.py::flow", path="/same"),
            )
        )


def test_declaration_normalizes_bool_and_validates_configured_paths():
    explicit = Webhook(path="/stripe/events")

    def declared(value: Webhook | bool | None) -> Workflow:
        decorator = cast(
            Callable[[Callable[[], None]], Callable[[], Workflow]],
            workflow(webhook=value),
        )

        def definition() -> None:
            return None

        return decorator(definition)()

    assert declared(True).webhook == Webhook()
    assert declared(False).webhook is None
    assert declared(explicit).webhook is explicit
    for invalid in ("relative", "//double", "/trailing/", "/../escape", "/a//b"):
        with pytest.raises(ValueError):
            Webhook(path=invalid)


def test_loopback_post_accepts_json_object_and_rejects_invalid_requests():
    class FakeOperator:
        calls = []

        def start_run(self, selector, *, input, triggered_by):
            self.calls.append((selector, input, triggered_by))
            return "run_webhook"

    operator = FakeOperator()
    server = WebhookServer(operator, 0)
    routes = routes_for((_descriptor(),))
    server.reconcile(routes)
    try:
        url = server.url_for("/webhooks/root/reports/daily/shared")
        assert url is not None and url.startswith("http://127.0.0.1:")
        request = Request(
            f"{url}?source=test",
            data=json.dumps({"message": "hello"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:  # noqa: S310 - loopback test server
            assert response.status == 202
            assert json.loads(response.read()) == {"run_id": "run_webhook"}
        assert operator.calls == [
            ("root/reports/daily.py::shared", {"message": "hello"}, "webhook")
        ]

        server.reconcile({})
        with pytest.raises(HTTPError) as removed_error:
            urlopen(request)  # noqa: S310 - loopback test server
        assert removed_error.value.code == 404
        server.reconcile(routes)

        with pytest.raises(HTTPError) as error:
            urlopen(url)  # noqa: S310 - loopback test server
        assert error.value.code == 405
    finally:
        server.close()
    assert not server.active
