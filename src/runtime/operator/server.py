"""gRPC server wrapping the Operator."""

from __future__ import annotations

import logging
from concurrent import futures

import grpc

from .convert import run_state_to_proto, workflow_info_to_proto
from .operator import Operator
from .proto import operator_pb2 as pb
from .proto import operator_pb2_grpc as pb_grpc

logger = logging.getLogger(__name__)

DEFAULT_PORT = 7433


class OperatorServicer(pb_grpc.OperatorServiceServicer):
    """gRPC servicer that delegates to an Operator instance."""

    def __init__(self, operator: Operator) -> None:
        self._op = operator

    def ListFlows(self, request, context):  # noqa: N802
        workflows = self._op.list_workflows()
        return pb.FlowList(
            flows=[workflow_info_to_proto(p) for p in workflows]
        )

    def StartRun(self, request, context):  # noqa: N802
        try:
            run_id = self._op.start_run(request.flow_name)
            return pb.StartRunResponse(run_id=run_id)
        except KeyError as e:
            context.abort(grpc.StatusCode.NOT_FOUND, str(e))

    def CancelRun(self, request, context):  # noqa: N802
        self._op.cancel_run(request.run_id)
        return pb.Empty()

    def ListRuns(self, request, context):  # noqa: N802
        runs = self._op.list_runs(request.flow_name)
        return pb.RunList(runs=[run_state_to_proto(r) for r in runs])

    def GetRun(self, request, context):  # noqa: N802
        run = self._op.get_run(request.run_id)
        if run is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Run {request.run_id} not found")
        return run_state_to_proto(run)

    def StreamUpdates(self, request, context):  # noqa: N802
        """Server-streaming RPC: yields RunUpdate messages as state changes."""
        q = self._op.subscribe()
        try:
            while context.is_active():
                try:
                    seq, run = q.get(timeout=1.0)
                    if seq <= request.since_sequence:
                        continue
                    yield pb.RunUpdate(
                        sequence=seq,
                        run=run_state_to_proto(run),
                    )
                except Exception:
                    # Queue.get timeout — just loop and check context.is_active()
                    continue
        finally:
            self._op.unsubscribe(q)


def serve(operator: Operator, port: int = DEFAULT_PORT, block: bool = True) -> grpc.Server:
    """Start the gRPC server.

    Args:
        operator: The Operator instance to serve.
        port: Port to listen on.
        block: If True, blocks until server is terminated.

    Returns:
        The gRPC server (useful for testing when block=False).
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb_grpc.add_OperatorServiceServicer_to_server(
        OperatorServicer(operator), server
    )
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    logger.info(f"Operator gRPC server listening on port {port}")

    if block:
        import os
        import subprocess
        import sys

        # Ray and gRPC intercept SIGINT at C level, making it impossible
        # to catch Ctrl+C via Python signal handlers. Solution: spawn a
        # tiny child process that waits for SIGINT (which it CAN receive)
        # and then kills our process group.
        pid = os.getpid()
        sentinel_script = (
            "import signal, os, sys; "
            f"signal.signal(signal.SIGINT, lambda *_: "
            f"(os.kill({pid}, signal.SIGKILL), sys.exit())); "
            f"signal.signal(signal.SIGTERM, lambda *_: "
            f"(os.kill({pid}, signal.SIGKILL), sys.exit())); "
            "signal.pause()"
        )
        sentinel = subprocess.Popen([sys.executable, "-c", sentinel_script])

        try:
            server.wait_for_termination()
        except KeyboardInterrupt:
            pass
        finally:
            sentinel.kill()
            server.stop(grace=1)

    return server
