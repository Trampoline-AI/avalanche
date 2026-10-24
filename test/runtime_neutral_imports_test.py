"""The operator and TUI work for every agent runtime without any runtime installed."""

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
import tui.app
import tui.ui_store
import tui.widgets.agent_trace

loaded = sorted(name for name in sys.modules if name.startswith("avalanche.runtimes."))
assert not loaded, loaded
"""


def test_operator_and_tui_import_without_predict_rlm():
    # A fresh interpreter: this test process has already imported PredictRLM.
    subprocess.run([sys.executable, "-c", _CHECK], check=True, timeout=120)
