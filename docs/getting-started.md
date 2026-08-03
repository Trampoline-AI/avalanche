# Getting Started

This guide is for early users trying Avalanche locally from a fresh clone. It
assumes Python 3.11 through 3.13, `uv`, and a local terminal.

The current team release candidate is `0.1.0-rc2`.

## What Is Supported

- Python flow authoring with `@ava.source`, `@ava.step`, `@ava.dest`, and
  `@ava.workflow`.
- Agent-backed workflow steps with the optional `agent` extra.
- Local flow execution with `ava.LocalExecutor`.
- Iceberg-backed and Lance-backed storage helpers.
- Local Iceberg examples backed by PyIceberg's SQL SQLite catalog.
- Run-scoped stream reads by default (`ava.Stream(table)`), with opt-in
  backlog/incremental processing via `ava.Stream(..., key=..., mode="append_scan")`.
- Cursor-based manual checkpoints for advanced incremental flows.
- A local operator that discovers flow files or directories.
- A Textual TUI in mock mode or connected to a local operator.
- Optional Ray-backed execution when a local Ray environment is available.

## What Is Not Supported Yet

- Production auth, authorization, TLS, or multitenancy.
- One-click cloud deploy.
- A schema migration CLI.
- A public `avalanche` console command.
- An `avalanche init` project generator.
- Durable operator replay/recovery beyond the current implementation.

## Setup

From the repository root:

```bash
uv sync
```

Run the full test suite:

```bash
make test
```

Some tests are skipped when optional local services or terminal features are not
available. Before handing off changes, run the full pre-commit gate:

```bash
make precommit-check
```

The pre-commit gate runs Ruff over source and tests, then runs the full test
suite.

For a shorter end-to-end confidence check, run:

```bash
make smoke-test
```

## Local Development Modes

Avalanche supports two local development modes. Both use the same workflow
runtime and can execute through `LocalExecutor` or `RayExecutor`; they differ in
where the run is started and managed.

### Embedded Mode

Run the Python program that declares the flow. The program builds the DAG and
calls `Workflow.run()` directly. The call immediately returns an awaitable
`ava.RunHandle` with a stable `run_id`; call `.result()` for a synchronous wait
or `await` the handle from asynchronous code:

```python
run = workflow.run(executor=ava.LocalExecutor())
print(run.run_id)
result = run.result()
```

The handle and its cached terminal result or failure exist only in this process.
Its driver uses one named non-daemon thread, so the process remains alive until
execution finishes. `run.cancel()` is cooperative and is observed between node
submissions; it does not interrupt an active Python thread or Ray task. No
operator or gRPC connection is required.

Start with the simplest local DAG example:

```bash
uv run python examples/complex_dag_pattern.py
```

Then try the incremental examples:

```bash
uv run python examples/stream_pattern.py
uv run python examples/cursor_pattern.py
```

Examples that need storage write under `.avalanche/examples/` by default. To keep
artifacts isolated for a test run, set `AVALANCHE_EXAMPLE_ROOT`:

```bash
AVALANCHE_EXAMPLE_ROOT=/tmp/avalanche-example uv run python examples/stream_pattern.py
```

### Operator-Managed Mode

Run the operator as the local control plane. It discovers flows from configured
Python files or directories, executes them in background runs, and owns run
state, logs, cancellation, schedules, and file watching. The CLI and connected
TUI send run-control requests to the operator over gRPC; the TUI does not execute
flows directly.

The shortest interactive path starts the operator and connected TUI together:

```bash
uv run ava dev --flows examples
```

To run them separately, start the operator in one terminal:

```bash
uv run ava operator --flows examples --port 7433
```

Operator terminal logging defaults to `WARNING`. Add `--log-level INFO` to see
service and source-watcher startup, each hot-reload attempt, successful revision
transitions, unchanged rescans, reload failures, and watcher shutdown. The flag
also works with the `python -m avalanche.operator` entry point.

Start a discovered flow from another terminal with the CLI:

```bash
uv run ava run operator_demo_workflow --connect localhost:7433
```

Alternatively, connect the TUI and start runs interactively:

```bash
uv run ava tui --connect localhost:7433
```

Or enable the operator's browser interface:

```bash
uv run ava operator --flows examples --web
```

Open `http://127.0.0.1:7435`. The browser receives the same ordered operator
updates as the TUI, including live catalog replacement when watched workflow
sources change. Current definitions update in place; historical run canvases
retain the topology and agent declarations captured for that run. The browser
listener defaults to loopback and has no built-in authentication. Use
`--web-trusted-proxy` with a non-loopback `--web-host` only behind a trusted,
authenticated proxy.


The TUI gets flows from the operator over gRPC. It does not import files from the
examples directory directly.

Module entry points are available if you need to bypass the `ava` wrapper:

```bash
uv run python -m avalanche.operator --flows examples --port 7433
uv run python -m avalanche.tui --connect localhost:7433
```

For UI-only exploration, use mock mode:

```bash
uv run python -m avalanche.tui
```

#### POST Body Intake

Operator-managed workflows can declare a loopback HTTP webhook. Declare the
workflow input as an `ava.BaseInput` model so Avalanche validates and injects the
decoded request body as a typed value:

```python
import avalanche as ava


class ReportRequest(ava.BaseInput):
    message: str
    priority: int = 0


@ava.source
def receive(payload: ReportRequest):
    print(payload.message, payload.priority)


@ava.workflow(input=ReportRequest, webhook=True)
def report_webhook():
    receive()
```

Start the operator with that flow file, then ask the operator for the generated
loopback URL:

```bash
uv run ava operator --flows path/to/report_flow.py --webhook-port 7434
uv run ava webhooks list --connect localhost:7433
```

Post a JSON object to the listed URL:

```bash
curl -i -X POST '<url-from-ava-webhooks-list>' \
  -H 'Content-Type: application/json' \
  --data '{"message":"daily report","priority":2}'
```

The endpoint accepts only `POST` requests with `Content-Type: application/json`,
a valid `Content-Length`, and a top-level JSON object no larger than 1 MiB. It
decodes the object and starts the discovered workflow with
`input=<decoded-object>` and `triggered_by="webhook"`. Avalanche then constructs
`ReportRequest` with Pydantic validation and injects it into matching node
parameters such as `payload` above.

A successfully started run returns `202` with `{"run_id": "..."}`. The HTTP
boundary rejects an unknown route (`404`), unsupported method (`405`), malformed
JSON or a non-object body (`400`), an oversized body (`413`), and a non-JSON
content type (`415`). Typed input validation happens during workflow execution,
after the run is accepted, so a schema mismatch is reported as a failed run
rather than a synchronous HTTP validation response.

## Optional Components

`uv sync` installs the development dependency group used by the test suite.
Package consumers can choose narrower extras:

| Extra | Use |
| --- | --- |
| `runtime` | operator gRPC server/client, file watching, and scheduling |
| `tui` | Textual terminal UI |
| `ray` | Ray executor support |
| `lance` | Lance storage backend support |
| `agent` | agent-backed workflow steps through [PredictRLM](https://github.com/Trampoline-AI/predict-rlm) |
| `all` | all optional runtime components |

For development, include extras with `uv sync --extra <name>`, for example:

```bash
uv sync --extra runtime --extra tui
```

## Troubleshooting

- If operator discovery imports too many files, replace `--flows .` with a
  specific flow file or a clean flow-only directory.
- If the connected TUI shows no flows, confirm the operator was started with the
  same port used by `ava tui --connect`.
- If Ray execution fails, first verify a local Ray head is available and set
  `RAY_ADDRESS` when needed.
- If example artifacts affect a repeat run, remove `.avalanche/examples/` or set
  `AVALANCHE_EXAMPLE_ROOT` to a fresh temp directory.

## Next References

- [examples/README.md](../examples/README.md) for canonical example details.
- [docs/dag-api.md](dag-api.md) for workflow DAG primitives.
- [docs/agent-steps.md](agent-steps.md) for agent-backed workflow steps.
- [docs/data-model-api.md](data-model-api.md) for table and namespace APIs.
- [ARCHITECTURE.md](../ARCHITECTURE.md) for component boundaries.
- [CONTRIBUTING.md](../CONTRIBUTING.md) for local development expectations.
- [CHANGELOG.md](../CHANGELOG.md) for release notes.
- [docs/releases/internal-alpha-checklist.md](releases/internal-alpha-checklist.md)
  for the team handoff checklist.
