"""Public agent backends resolve lazily in a fresh interpreter."""

import subprocess
import sys


def test_direct_python_backend_export_preserves_lazy_import() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import avalanche
assert not any(name == 'predict_rlm' or name.startswith('predict_rlm.') for name in sys.modules)
from avalanche import agent
assert 'DirectPythonBackend' in agent.__all__
assert 'DirectPythonBackend' not in vars(agent)
from avalanche.agent import DirectPythonBackend
from predict_rlm import DirectPythonBackend as implementation
assert DirectPythonBackend is implementation
assert agent.DirectPythonBackend is DirectPythonBackend
""",
        ],
        check=True,
    )
