"""Runtime-specific (native) header, turn and live-event parts, one module per format.

They are defined in Avalanche without runtime SDK imports, so the operator,
client and TUI validate every format without the runtime extras installed.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from .claude_agent import ClaudeAgentNativeEvent, ClaudeAgentTraceHeader, ClaudeAgentTurn
from .predict_rlm import PredictRLMNativeEvent, PredictRLMTraceHeader, PredictRLMTurn

NativeEvent = Annotated[
    PredictRLMNativeEvent | ClaudeAgentNativeEvent, Field(discriminator="format")
]
# A turn's runtime-specific part, discriminated by its trace format.
TurnNative = Annotated[PredictRLMTurn | ClaudeAgentTurn, Field(discriminator="format")]

# A trace header's runtime-specific part, discriminated by its trace format.
TraceHeaderNative = Annotated[
    PredictRLMTraceHeader | ClaudeAgentTraceHeader, Field(discriminator="format")
]

__all__ = ["NativeEvent", "TraceHeaderNative", "TurnNative"]
