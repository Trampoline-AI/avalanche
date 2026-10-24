"""Closed sets of names shared by agent traces, live events and native payloads."""

from __future__ import annotations

from typing import Literal

# Every agent runtime Avalanche knows; a new runtime adds its kind here.
RuntimeKind = Literal["predict_rlm", "claude_agent"]

# Kinds of the runtime-neutral live events every runtime can send.
CommonEventKind = Literal[
    "turn.finished",
    "model.started",
    "model.finished",
    "tool.started",
    "tool.finished",
    "code.generated",
]
