"""Runtime-specific (native) live-event payloads, one module per trace format.

They are defined in Avalanche without runtime SDK imports, so the operator,
client and TUI validate every format without the runtime extras installed.
"""

from __future__ import annotations

from .predict_rlm import PredictRLMNativeEvent

# A discriminated union on ``format`` once a second runtime adds its format.
NativeEvent = PredictRLMNativeEvent

__all__ = ["NativeEvent"]
