"""Runtime-specific (native) live-event payloads, one module per trace format.

They are defined in Avalanche without runtime SDK imports, so the operator,
client and TUI validate every format without the runtime extras installed.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from .claude_agent import ClaudeAgentNativeEvent
from .predict_rlm import PredictRLMNativeEvent

NativeEvent = Annotated[
    PredictRLMNativeEvent | ClaudeAgentNativeEvent, Field(discriminator="format")
]

__all__ = ["NativeEvent"]
