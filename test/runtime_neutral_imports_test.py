"""The operator, TUI and ``ava.agent.Skill`` work without any agent runtime installed."""

from __future__ import annotations

import subprocess
import sys

_CHECK = """
import sys

# Any import of PredictRLM now raises ImportError.
sys.modules["predict_rlm"] = None
import runtime.operator.client
import runtime.operator.operator
import runtime.operator.run_worker
import runtime.operator.server
import runtime.operator.server_tui
import runtime.operator.tui_extensions
import tui.app
import tui.ui_store
import tui.widgets.agent_trace

import avalanche as ava

# The shared Agent Skills type is runtime-neutral; PredictRLM's skills are not.
assert ava.agent.skills.Skill is ava.agent.Skill

loaded = sorted(name for name in sys.modules if name.startswith("avalanche.runtimes."))
assert not loaded, loaded
"""


def test_operator_tui_and_agent_skills_import_without_predict_rlm():
    # A fresh interpreter: this test process has already imported PredictRLM.
    subprocess.run([sys.executable, "-c", _CHECK], check=True, timeout=120)
