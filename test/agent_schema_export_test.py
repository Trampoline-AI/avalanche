"""The checked-in agent schema file matches the Python trace models."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "export_agent_schemas.py"


def test_checked_in_agent_schemas_match_the_models():
    spec = importlib.util.spec_from_file_location("export_agent_schemas", _SCRIPT)
    assert spec is not None and spec.loader is not None
    export = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(export)

    assert (
        export.OUTPUT.read_text() == export.agent_schemas_json()
    ), "agent schemas are stale; run `make web-proto`"
