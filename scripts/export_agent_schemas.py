"""Write the JSON Schema of Avalanche's runtime-neutral agent models.

The web UI generates zod schemas and TypeScript types from this file
(``make web-proto``), so the browser and Python agree on one shape for agent traces and declarations.

    uv run python scripts/export_agent_schemas.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from avalanche.agent.contract import AgentEvent
from avalanche.agent.declaration import AgentDeclaration
from avalanche.agent.trace import AgentTraceRecord

OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "web"
    / "operator"
    / "src"
    / "generated"
    / "agentSchemas.json"
)


def agent_schemas_json() -> str:
    """One schema document whose ``$defs`` hold traces, live events and declarations."""
    keyed, schema = TypeAdapter.json_schemas(
        [
            ("AgentTraceRecord", "serialization", TypeAdapter(AgentTraceRecord)),
            ("AgentEvent", "serialization", TypeAdapter(AgentEvent)),
            ("AgentDeclaration", "serialization", TypeAdapter(AgentDeclaration)),
        ],
        ref_template="#/$defs/{model}",
    )
    definitions = schema["$defs"]
    # The live event union has no class of its own, so it is inlined; name it.
    definitions["AgentEvent"] = keyed[("AgentEvent", "serialization")]
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    OUTPUT.write_text(agent_schemas_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
