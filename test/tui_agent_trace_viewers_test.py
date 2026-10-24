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
