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

from avalanche.agent.declaration import AgentDeclaration
from avalanche.agent.live import AgentLiveEvent, AgentTraceTerminal

OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "web"
    / "operator"
    / "src"
    / "generated"
    / "agentSchemas.json"
)


def agent_schemas_json() -> str:
    """One schema document whose ``$defs`` hold the bodies the operator serves.

    Live event bodies are ``AgentLiveEvent``; final trace bodies are
    ``AgentTraceTerminal``; declarations are ``AgentDeclaration``.
    """
    keyed, schema = TypeAdapter.json_schemas(
        [
            ("AgentLiveEvent", "serialization", TypeAdapter(AgentLiveEvent)),
            ("AgentTraceTerminal", "serialization", TypeAdapter(AgentTraceTerminal)),
            ("AgentDeclaration", "serialization", TypeAdapter(AgentDeclaration)),
        ],
        ref_template="#/$defs/{model}",
    )
    definitions = schema["$defs"]
    # The terminal union has no class of its own, so it is inlined; name it.
    definitions["AgentTraceTerminal"] = keyed[("AgentTraceTerminal", "serialization")]
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> int:
    OUTPUT.write_text(agent_schemas_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
