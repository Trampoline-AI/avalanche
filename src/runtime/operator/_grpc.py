"""Shared private gRPC transport configuration."""

from __future__ import annotations

MAX_GRPC_MESSAGE_BYTES = 48 * 1024 * 1024

_BOUNDED_MESSAGE_OPTIONS = (
    ("grpc.max_send_message_length", MAX_GRPC_MESSAGE_BYTES),
    ("grpc.max_receive_message_length", MAX_GRPC_MESSAGE_BYTES),
)
