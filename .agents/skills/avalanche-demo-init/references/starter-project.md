# Starter workspace

The initializer creates one UV workspace named `avalanche-workflows` in the
current directory. It is the home for a collection of workflows, not one
project per workflow.

```text
avalanche-workflows/
├── pyproject.toml
├── uv.lock
├── AGENTS.md
├── .trampoline-ai/
│   ├── avalanche/
│   └── predict-rlm/
└── src/
    └── binary_converter/
        └── flow.py
```

The only starter asset is `assets/binary_converter/flow.py`. It is the README
quick example:

```text
generate_binary → convert_binary → print_result
```

The initializer writes the selected CodexLM, OpenAI, or LiteLLM-compatible
backend into `flow.py` without executing it. Workflows are run through the
Avalanche operator, never through a per-workflow runner.


## Adding another workflow

Create each subsequent workflow directly under `src/`:

```text
src/
├── binary_converter/
├── research_assistant/
└── document_reviewer/
```

Each workflow starts with only `flow.py`; add `schema.py`, `util.py`, or
substantial agent-contract directories only when the workflow actually needs
them. Do not create a `src/avalanche_workflows/` wrapper package, a nested
`pyproject.toml`, per-workflow local framework clones, or embedded runners.

The shared workspace installs `avalanche-ai[all]`. To start a flow from the
CLI, first start an operator, then invoke the flow through it:

```bash
uv run ava operator --flows src/binary_converter/ --port 7433
uv run ava run binary_converter --connect localhost:7433
```
