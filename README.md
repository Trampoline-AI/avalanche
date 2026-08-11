

# Avalanche

Avalanche makes agents first-class steps in typed data pipelines. Compose  
adaptive agent work with deterministic Python transformations in one DAG, run  
it through the Avalanche operator, and inspect every run from the terminal UI.

> [!NOTE]  
> Avalanche is an early release candidate intended for local development and  
> experimentation. APIs and operational behavior may change before a stable  
> release.

## Requirements

- Python 3.11, 3.12, or 3.13.
- A LLM provider API key or Codex subscription for agent steps.
- uv (recommended, https://docs.astral.sh/uv/)

## Quickstart

Move into an empty directory, then run this command to initialize a starter
project with the Avalanche skill installed and an example workflow:

```bash
uvx avalanche-ai init
```

Follow the instructions to set up your LLM provider. Then finally, run the demo:

> [!WARNING]
> `ava dev` without `--flows` scans every eligible Python file below the current
> working directory. Run it only from a dedicated flow workspace; otherwise pass
> a specific flow file or flow-only directory with `--flows`.

```bash
uv run ava dev
```

This starts the operator and opens the browser UI at `http://127.0.0.1:7435`.

## Installation



### Starter project

We recommend starting with the default Avalanche project, which includes everything you need to get started quickly on your first workflow. Just create an empty directory, cd into it and run:

```bash
uvx avalanche-ai init
```

Follow the instruction to set up your LLM provider

When run from an interactive terminal, the bootstrapper offers provider setup immediately. To change
providers or credentials later in the starter project:

```bash
bash scripts/configure-provider.sh
```



### Existing project

Avalanche is also usable as a project dependency.

Add Avalanche to an existing project:

```bash
uv add avalanche-ai
```

Install the avalanche skill in the same project:

```bash
npx skills add Trampoline-AI/avalanche
```



### Local checkout dependencies

To develop Avalanche and PredictRLM alongside a new workspace, initialize an
empty directory with editable dependencies:

```bash
uvx avalanche-ai init --editable-deps
```

This clones both Trampoline AI projects into `.trampoline-ai/` and configures
them as local editable dependencies, so changes to either checkout are used
immediately by the workspace.

## Usage



### Creating a workflow

Avalanche workflows chain deterministic `@ava.step` and agent-backed
`@ava.agent_step` nodes inside an `@ava.workflow`.

```python
@ava.step
def step1() -> str:
    return "Hello world"
```

```python
@ava.agent_step(ava.Signature("text: str -> completion: str"))
async def step2(text: str, *, agent: ava.Agent) -> str:
    return (await agent(text=text)).completion
```

```python
@ava.workflow
def feedback_workflow():
    return step1() >> step2()
```

We recommend using the skill directly in order to have your agent align on a goal and build a workflow for you.

Open your coding agent in the same project where you installed avalanche, then:

```
/avalanche <Describe your wanted outcome here>
```

for codex:

```
$avalanche <Describe your wanted outcome here>
```

### Running the operator and Web UI

The operator scans your code for workflows, then loads and runs them:

```bash
uv run ava operator
```

The Web UI reflects the state of the oeprator:

```bash
uv run ava web
```

Start the operator and Web UI together:

```bash
uv run ava dev
```

You can pass `--flows` to `operator` or `dev` to point the operator scan to a certain file or directory:

```bash
uv run ava dev --flows ./flow.py
```

Otherwise, the scan defaults to the current working directory.


Similarily, you can pass `--connect` to the Web UI to change the operator url to connect to:

```bash
uv run ava web --connect localhost:7433
```

The operator defaults to `127.0.0.1:7433` and the Web UI to
`http://127.0.0.1:7435`.

### Running a workflow

Once you have the operator running, you can either start workflows directly in the web UI, or start runs from your command line in a different terminal:

```bash
uv run ava run <workflow_name>
```



### TUI

Avalanche also ships with a Terminal UI, that you can launch on the operator:

```bash
uv run ava tui --connect localhost:7433
```

The operator defaults to port 7433.

### Workflow inputs

Avalanche supports passing inputs to workflows using the BaseInput class. Learn more in
[the DAG API's input and context guide](docs/dag-api.md#input-and-context). You can pass
inputs directly in the Web UI using small JSON editor, or through the command line:

```bash
uv run ava run <workflow_name> --input '{"key": "value"}'
```



### Embedded workflows

You can run a workflow directly from Python. `.run()` returns an awaitable `RunHandle`; call `.result()` to wait synchronously:

```python
run = feedback_workflow().run(executor=ava.LocalExecutor())
print(run.run_id)
result = run.result()
```



## Quick Example

```python
import random
import avalanche as ava

@ava.source
def generate_binary() -> str:
    length = random.randint(128, 256)
    return "1" + "".join(random.choice("01") for _ in range(length - 1))

@ava.agent_step(
    ava.Signature(
        "binary: str -> decimal: str",
    ),
    lm="openai/gpt-5.6-terra",
)
async def convert_binary(binary: str, *, agent: ava.Agent) -> str:
    return (await agent(binary=binary)).decimal

@ava.dest
def print_result(result: str) -> str:
    print(result)
    return result


@ava.workflow
def binary_converter():
    return generate_binary() >> convert_binary() >> print_result()
```



## Examples

The [`examples/`](examples/) directory contains runnable workflows. Start with
the customer feedback review, a production-shaped agentic data-transformation
workflow; the rest are focused pattern demos.

| Example | Description |
| ------- | ----------- |
| [Customer feedback review](examples/customer_feedback_review/) | End-to-end agentic workflow: parallel theme/risk analysis of a feedback workbook, deterministic reconciliation, and published Excel + Word review pack. |
| [`complex_dag_pattern.py`](examples/complex_dag_pattern.py) | Local DAG API with explicit data passing, fan-out, and fan-in on `ava.LocalExecutor`. |
| [`stream_pattern.py`](examples/stream_pattern.py) | Stream-based incremental processing with local Iceberg tables. |
| [`cursor_pattern.py`](examples/cursor_pattern.py) | Manual checkpoint control with cursors for advanced incremental flows. |
| [`document_file_workflow.py`](examples/document_file_workflow.py) | Typed `ava.File` inputs and outputs through a `BaseInput` workflow. |
| [`operator_workflow.py`](examples/operator_workflow.py) | Flow file for the local operator and connected TUI path. |

See [`examples/README.md`](examples/README.md) for how to run each example.

## LLM providers and models

Avalanche sends agent-model requests through [LiteLLM](https://www.litellm.ai/).
Any provider and model supported by LiteLLM is therefore supported by Avalanche.
Configure the provider credentials as environment variables documented in
[LiteLLM's provider guide](https://docs.litellm.ai/docs/providers); the process
running the operator must have access to those variables.

We select models on each `@ava.agent_step` with LiteLLM's provider-qualified
model identifier. `lm` selects the main model and `sub_lm` selects the
sub-model:

```python
@ava.agent_step(
    ExtractThemes,
    lm="openai/gpt-5.6-terra",
    sub_lm="gemini/gemini-3.5-flash",
)
async def extract_themes(..., *, agent: ava.Agent) -> ThemeReport:
    ...
```

When a workflow's agent steps share models, we set them once with
`@ava.workflow(agent_defaults=...)`:

```python
@ava.workflow(
    agent_defaults={
        "lm": "openai/gpt-5.6-terra",
        "sub_lm": "gemini/gemini-3.5-flash",
    }
)
def feedback_workflow():
    return extract_themes()
```

An `lm` or `sub_lm` passed to an individual agent step overrides the same
workflow default. `agent_defaults` configures runtime options only; signatures, skills, and
tools remain defined on each agent step.

## Optional components


| Extra   | Purpose                       |
| ------- | ----------------------------- |
| `ray`   | Ray-backed workflow execution |
| `lance` | Lance storage backend         |


The remaining extras can be combined:

```bash
uv add "avalanche-ai[ray,lance]"
```



## Documentation

- [DAG API](docs/dag-api.md)
- [Agent steps](docs/agent-steps.md)
- [Data model and storage API](docs/data-model-api.md)
- [Execution services](docs/execution-services.md)
- [Architecture](ARCHITECTURE.md)
- [Examples](examples/README.md)
- [Changelog](CHANGELOG.md)



## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for local setup,
quality gates, and pull request expectations.

## License

Avalanche is licensed under the [Apache License 2.0](LICENSE).