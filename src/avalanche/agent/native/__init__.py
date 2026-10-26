"""Runtime-specific (native) turn parts and live-event payloads, one module per format.

They are defined in Avalanche without runtime SDK imports, so the operator,
client and TUI validate every format without the runtime extras installed.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from .claude_agent import ClaudeAgentNativeEvent, ClaudeAgentTurn
from .predict_rlm import PredictRLMNativeEvent, PredictRLMTurn

NativeEvent = Annotated[
    PredictRLMNativeEvent | ClaudeAgentNativeEvent, Field(discriminator="format")
]
# A turn's runtime-specific part, discriminated by its trace format.
TurnNative = Annotated[PredictRLMTurn | ClaudeAgentTurn, Field(discriminator="format")]

__all__ = ["NativeEvent", "TurnNative"]
