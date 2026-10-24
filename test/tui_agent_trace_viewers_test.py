"""Every agent trace format the operator can send has a TUI viewer."""

from __future__ import annotations

from typing import get_args

from avalanche.agent.native import NativeEvent
from avalanche.agent.trace import NativeTrace
from tui.agent_trace_viewers import VIEWERS


def _formats(union: object) -> set[str]:
    members = get_args(get_args(union)[0]) if get_args(union) else (union,)
    return {member.model_fields["format"].default for member in members}


def test_every_trace_format_has_a_viewer():
    assert set(VIEWERS) == _formats(NativeEvent) | _formats(NativeTrace)


def test_the_predict_rlm_viewer_labels_every_native_kind():
    from avalanche.agent.native import predict_rlm as native
    from tui.agent_trace_viewers.predict_rlm import _LABELS

    members = get_args(get_args(native.PredictRLMNativeEvent)[0])
    assert set(_LABELS) == {kind for member in members for kind in member.common_kinds}
