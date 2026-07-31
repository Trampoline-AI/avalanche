from __future__ import annotations

from concurrent import futures

import grpc

from avalanche.operator.client import GrpcStateProvider
from avalanche.tui import ConnectionAwareStateProvider
from runtime.operator import client as client_module
from runtime.operator._grpc import MAX_GRPC_MESSAGE_BYTES
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc


def test_grpc_state_provider_sends_bearer_metadata() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
    pb_grpc.add_OperatorServiceServicer_to_server(AuthenticatedOperatorService(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    provider = GrpcStateProvider(f"127.0.0.1:{port}", token="secret")
    try:
        workflows = provider.list_workflows()
    finally:
        provider.close()
        server.stop(grace=0)

    assert [workflow.name for workflow in workflows] == ["demo-flow"]


def test_grpc_state_provider_uses_secure_channel_when_tls_enabled(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeChannel:
        def close(self) -> None:
            calls["closed"] = True

    def fake_credentials(*, root_certificates=None, private_key=None, certificate_chain=None):
        calls["root_certificates"] = root_certificates
        calls["private_key"] = private_key
        calls["certificate_chain"] = certificate_chain
        return "credentials"

    def fake_secure_channel(address: str, credentials: object, *, options):
        calls["address"] = address
        calls["credentials"] = credentials
        calls["options"] = options
        return FakeChannel()

    monkeypatch.setattr(client_module.grpc, "ssl_channel_credentials", fake_credentials)
    monkeypatch.setattr(client_module.grpc, "secure_channel", fake_secure_channel)
    monkeypatch.setattr(client_module.pb_grpc, "OperatorServiceStub", lambda channel: object())

    provider = GrpcStateProvider("operator.example:443", tls=True, root_certificates=b"ca")
    assert isinstance(provider, ConnectionAwareStateProvider)
    assert provider.connection_label == "operator.example:443"
    provider.close()

    assert calls["address"] == "operator.example:443"
    assert calls["credentials"] == "credentials"
    assert calls["root_certificates"] == b"ca"
    assert dict(calls["options"]) == {
        "grpc.max_send_message_length": MAX_GRPC_MESSAGE_BYTES,
        "grpc.max_receive_message_length": MAX_GRPC_MESSAGE_BYTES,
    }
    assert calls["closed"] is True


def test_grpc_state_provider_uses_bounded_insecure_channel_options(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeChannel:
        def close(self) -> None:
            pass

    def fake_insecure_channel(address: str, *, options):
        calls["address"] = address
        calls["options"] = options
        return FakeChannel()

    monkeypatch.setattr(client_module.grpc, "insecure_channel", fake_insecure_channel)
    monkeypatch.setattr(client_module.pb_grpc, "OperatorServiceStub", lambda channel: object())

    provider = GrpcStateProvider("localhost:7433")
    provider.close()

    assert calls["address"] == "localhost:7433"
    assert dict(calls["options"]) == {
        "grpc.max_send_message_length": MAX_GRPC_MESSAGE_BYTES,
        "grpc.max_receive_message_length": MAX_GRPC_MESSAGE_BYTES,
    }


class AuthenticatedOperatorService(pb_grpc.OperatorServiceServicer):
    def GetCatalog(self, request, context):  # noqa: N802
        authorization = dict(context.invocation_metadata()).get("authorization")
        if authorization != "Bearer secret":
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing_bearer")
        return pb.CatalogSnapshotMsg(workflows=[pb.FlowInfoMsg(name="demo-flow")])
