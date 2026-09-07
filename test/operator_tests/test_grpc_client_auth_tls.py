"""Exercise certificate trust and bearer authorization over an actual TLS channel."""

import threading
import time
from concurrent import futures
from datetime import datetime, timedelta, timezone

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from runtime.operator.client import GrpcStateProvider, OperatorCallError, StreamState
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc


class AuthenticatedOperatorService(pb_grpc.OperatorServiceV2Servicer):
    def __init__(self):
        self.watching = threading.Event()

    def _authorize(self, context):
        if dict(context.invocation_metadata()).get("authorization") != "Bearer secret":
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "bearer token required")

    def DiscoverFlows(self, request, context):  # noqa: N802
        self._authorize(context)
        return pb.FlowListV2(
            scope_ref=pb.ScopeReferenceV2(reference="operator-auth"),
            cursor=pb.LifecycleCursorV2(
                stream="operator-events",
                topology_fingerprint="operator-events-topology",
                stream_generation=1,
                retained_floor_event_ulid="00000000000000000000000000",
                event_ulid="00000000000000000000000000",
            ),
            flows=[pb.FlowInfoV2(workflow_selector="demo", display_name="demo")],
        )

    def WatchRunStatus(self, request, context):  # noqa: N802
        self._authorize(context)
        context.send_initial_metadata(())
        self.watching.set()
        closed = threading.Event()
        context.add_callback(closed.set)
        closed.wait(10)
        return iter(())


def test_tls_trust_and_bearer_auth_gate_unary_and_stream_calls():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )
    private_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    service = AuthenticatedOperatorService()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    pb_grpc.add_OperatorServiceV2Servicer_to_server(service, server)
    credentials = grpc.ssl_server_credentials(((private_key, certificate),))
    port = server.add_secure_port("localhost:0", credentials)
    server.start()
    try:
        untrusted = GrpcStateProvider(
            f"localhost:{port}", tls=True, token="secret", unary_timeout=1
        )
        try:
            with pytest.raises(OperatorCallError) as error:
                untrusted.list_workflows()
            assert error.value.status is grpc.StatusCode.UNAVAILABLE
        finally:
            untrusted.close()

        for token in (None, "wrong"):
            denied = GrpcStateProvider(
                f"localhost:{port}", tls=True, root_certificates=certificate, token=token
            )
            try:
                with pytest.raises(OperatorCallError) as error:
                    denied.list_workflows()
                assert error.value.status is grpc.StatusCode.UNAUTHENTICATED
            finally:
                denied.close()

        client = GrpcStateProvider(
            f"localhost:{port}", tls=True, root_certificates=certificate, token="secret"
        )
        try:
            assert [flow.selector for flow in client.list_workflows()] == ["demo"]
            client.start_stream()
            assert service.watching.wait(5)
            deadline = time.monotonic() + 5
            while client.stream_state is not StreamState.LIVE and time.monotonic() < deadline:
                time.sleep(0.01)
            assert client.stream_state is StreamState.LIVE
        finally:
            client.close()
    finally:
        server.stop(grace=0).wait()
