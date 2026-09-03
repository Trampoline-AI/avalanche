# Avalanche usage

This reference mirrors the README's Usage section and adds the exact local CLI
interfaces an agent may use. Run commands with `uv run ava ...` from a UV
project. Do not start long-lived services unless the user asks to run, inspect,
or verify a workflow through the operator.

## Choose an execution surface

- **Embedded execution:** application code constructs a workflow and calls
  `.run()` directly.
- **Browser UI:** the normal interactive local surface. Use it with the local
  operator when the user asks for an interactive run, inspection, or browser
  verification.
- **TUI:** an optional terminal surface. Do not run it from the agent; give the
  user the command to run in a terminal they control.

For the browser UI, prefer `uv run ava dev` unless the user needs separate
process lifecycles or custom browser-listener settings. Do not automatically
launch an operator or UI merely because an embedded workflow was implemented.

## Define a workflow

Use deterministic `@ava.step` nodes for ordinary Python work, and
`@ava.agent_step` for model-backed work:

```python
import avalanche as ava


@ava.step
def step1() -> str:
    return "Hello world"


@ava.agent_step(ava.Signature("text: str -> completion: str"))
async def step2(text: str, *, agent: ava.Agent) -> str:
    return (await agent(text=text)).completion


@ava.workflow
def feedback_workflow():
    return step1() >> step2()
```

`@ava.workflow` decorates a builder function. Call the builder before running:

```python
run = feedback_workflow().run(executor=ava.LocalExecutor())
print(run.run_id)
result = run.result()
```

`Workflow.run()` returns an awaitable `ava.RunHandle`; `.result()` waits
synchronously. The declared `ava.Signature` output names are the prediction
attribute names—read `.completion` for the signature above, not `.summary`.

## Browser UI and operator

### Combined local path: `ava dev`

Use this when the user asks to start a local operator and browser UI together:

```bash
uv run ava dev path/to/flow.py
```

`ava dev` starts the operator on `127.0.0.1:7433` and a browser UI connected to
it at `http://127.0.0.1:7435` by default.

```text
uv run ava dev [FLOW [FLOW ...]] [--port PORT] [--web-port PORT] [--ray]
```

- `FLOW [FLOW ...]`: optional flow files or clean flow-only directories. When
  omitted, the command uses `[tool.avalanche].flow_targets` in the nearest
  `pyproject.toml`; relative paths resolve from that file.
- Explicit `FLOW` values replace configured targets. Without either source, the
  command fails before starting services; there is no current-directory default.
- `--port PORT`: operator gRPC port, default `7433`.
- `--web-port PORT`: browser UI HTTP port, default `7435`. It must differ from
  `--port`.
- `--ray`: use the Ray executor.

Use separate commands instead when the browser listener's host must change or
the services need independent lifecycles.

### Separate local processes: `ava operator` and `ava web`

Run these in separate terminals when the user asks for independent lifecycle or
custom browser-listener settings:

```bash
# terminal 1
uv run ava operator path/to/flow.py --port 7433

# terminal 2
uv run ava web --connect localhost:7433
```

`ava operator` does **not** accept `--web`. Use `ava dev` or start `ava web`
separately.

```text
uv run ava operator [FLOW [FLOW ...]] [--host HOST] [--port PORT]
                        [--webhook-port PORT] [--log-level LEVEL] [--ray]
```

- `FLOW [FLOW ...]`: optional flow discovery targets. Without them, the command
  uses `[tool.avalanche].flow_targets` from the nearest `pyproject.toml`; explicit
  targets replace it. Use narrow paths, never a mixed repository root.
- `--host HOST`: gRPC listen host, default `127.0.0.1`. Non-loopback exposure
  requires an external trusted, authenticated boundary.
- `--port PORT`: gRPC port, default `7433`.
- `--webhook-port PORT`: loopback webhook HTTP port, default `7434`.
- `--log-level LEVEL`: one of `DEBUG`, `INFO`, `WARNING`, or `ERROR`; default
  `WARNING`.
- `--ray`: use the Ray executor.

```text
uv run ava web [--connect HOST:PORT] [--host HOST] [--port PORT]
                   [--trusted-proxy]
```

- `--connect HOST:PORT`: operator address, default `localhost:7433`.
- `--host HOST`: browser UI listen host, default `127.0.0.1`.
- `--port PORT`: browser UI HTTP port, default `7435`.
- `--trusted-proxy`: required confirmation before serving non-loopback browser
  traffic behind a trusted, authenticated proxy. Keep the default loopback host
  otherwise.

`ava web` prints the browser endpoint and attempts to open it. When a browser
UI is started, report its actual local URL to the user.

## Run workflows and pass inputs

Start a discovered workflow through an operator:

```bash
uv run ava run <workflow-selector> --connect localhost:7433 \
  --input '{"key":"value"}'
```

```text
uv run ava run WORKFLOW_SELECTOR [--connect HOST:PORT] [--input JSON]
                                  [--context JSON] [--file FIELD=PATH] ...
                                  [--workspace FIELD=DIR] ...
```

- `WORKFLOW_SELECTOR`: the discovered workflow name or selector.
- `--connect HOST:PORT`: operator address, default `localhost:7433`.
- `--input JSON`: JSON object for `ava.BaseInput` workflow fields.
- `--context JSON`: JSON object for runtime context.
- `--file FIELD=PATH`: repeat for each top-level `ava.File` input field.
- `--workspace FIELD=DIR`: repeat for each top-level `ava.Workspace` input
  field.

Declare workflow inputs with a Pydantic `ava.BaseInput` subclass. The CLI
validates the supplied JSON and attachments at the operator boundary.

Retrieve a completed result when the user asks for local result files:

```text
uv run ava result RUN_ID --output-dir PATH [--connect HOST:PORT] [--wait]
                         [--timeout SECONDS]
```

- `--output-dir PATH` is required and must name a destination that does not
  already exist.
- `--wait` waits for a nonterminal run.
- `--timeout SECONDS` bounds `--wait`; the default is `300` seconds.

Inspect locally exposed webhook routes when needed:

```text
uv run ava webhooks list [--connect HOST:PORT]
uv run ava webhooks get WORKFLOW_SELECTOR [--connect HOST:PORT]
```

## Optional TUI handoff

Do not launch `uv run ava tui` from the agent or a background process. Its
interactive terminal would not be available to the user. When the user asks for
the TUI, ensure they have an operator endpoint, then give them this command to
run themselves in another terminal:

```bash
uv run ava tui --connect localhost:7433
```

```text
uv run ava tui [FLOW[/NODE]] [--connect HOST:PORT] [--token TOKEN]
               [--tls | --insecure] [--tls-ca-cert PATH]
```

- `FLOW[/NODE]`: optional workflow or workflow/node deep link.
- `--connect HOST:PORT`: operator address. Omit it for mock-mode UI exploration.
- `--token TOKEN`: operator bearer token.
- `--tls`, `--insecure`, `--tls-ca-cert PATH`: gRPC connection security
  settings.

The TUI discovers and controls workflows through gRPC; it does not import or
execute a flow directly.

## Embedded execution

For application-owned execution, call the workflow builder and run the returned
`Workflow` in the same process:

```python
import avalanche as ava


@ava.workflow
def document_flow():
    return step1() >> step2()


run = document_flow().run(executor=ava.LocalExecutor())
result = run.result()
```

No operator or gRPC connection is involved. Use this path when the caller owns
the run and consumes its terminal result directly.
