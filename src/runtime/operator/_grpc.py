"""Shared private gRPC transport configuration."""

from __future__ import annotations

_UNLIMITED_MESSAGE_OPTIONS = (
    ("grpc.max_send_message_length", -1),
    ("grpc.max_receive_message_length", -1),
)
