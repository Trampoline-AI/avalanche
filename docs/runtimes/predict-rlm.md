# PredictRLM runtime

[PredictRLM](https://github.com/trampoline-ai/predict-rlm) runs an agent that
writes Python and runs it in a sandbox, one iteration at a time. Its code can call
your tools and `predict()`, which asks a second, smaller model for a structured
answer. The agent ends by calling `SUBMIT(...)` with its outputs.

It is the default runtime and is included with Avalanche.

## Using it

```python
import avalanche as ava


@ava.agent_step(
    Summarize,
    runtime=ava.runtimes.PredictRLM(
        lm="openai/gpt-5",
        sub_lm="openai/gpt-5-mini",
        max_iterations=10,
    ),
    tools=[lookup_term],
)
async def summarize(document: str, *, agent: ava.Agent) -> str:
    result = await agent(document=document)
    return result.outputs.summary
```

`Summarize` and `lookup_term` are defined on the
[agent runtimes page](../agent-runtime-contract.md#calling-an-agent-step). A
step without `runtime=` uses the workflow's `agent_defaults` runtime, else
`ava.runtimes.PredictRLM()`.

## Settings

Each setting is passed to PredictRLM under the same name. The signature and tools
come from the step.

```python
class PredictRLM:
    lm: dspy.BaseLM | str | None = None         # main model, writes the code; None: DSPy's current model
    sub_lm: dspy.BaseLM | str | None = None     # model behind predict(); None: DSPy's current model
    max_iterations: int = 30
    max_llm_calls: int = 50
    max_output_chars: int = 50_000              # code output shown to the model
    verbose: bool = False                       # PredictRLM's own default is True
    skills: Sequence[Skill] | None = None       # PredictRLM's own skills, see below
    allowed_domains: Sequence[str] | None = None  # sandbox network access; None: no network
    sandbox_backend: str | None = None          # "jspi" (default) or "sbx" for Docker Sandboxes
    sbx_config: SbxConfig | None = None
    sbx_pool: SbxPool | None = None
    interpreter: CodeInterpreter | SupervisorClient | None = None  # e.g. DirectPythonBackend
    execution: ExecutionBackend | None = None
    output_dir: str | Path | None = None        # not allowed on steps with ava.File or ava.Workspace fields
    debug: bool = False
    model_execution_timeout: bool = False       # let the model time-limit its own code
    submit_confirmation: Callable[[SubmitConfirmationContext], str | None] | None = None
    trace_export_path: str | Path | None = None   # trace file updated while running
    run_export_root: str | Path | None = None   # <root>/<run_id>/trace.json and evidence.json
    telemetry_context: TelemetryContext | None = None
    runtime_hooks: Sequence[RuntimeHook] | None = None
    on_runtime_hook_event: Callable[[RuntimeHookEvent], object] | None = None
    adapter: dspy.Adapter | None = None         # formats model requests and parses replies
    adapters: Sequence[InputAdapter | OutputAdapter] = ()
    modules: Sequence[RuntimeModule | RuntimeContribution] = ()
    events: Sequence[EventSink] = ()
```

PredictRLM's own skills are not `ava.agent.Skill`. Each one is always in the
agent's prompt and can bring sandbox packages, Python modules and tools. They
live in `ava.runtimes.predict_rlm.skills`, with the built-in `pdf`, `docx` and
`spreadsheet`:

```python
ava.runtimes.PredictRLM(skills=[ava.runtimes.predict_rlm.skills.pdf])
```

## Trace example

The `Summarize` call above, on PredictRLM, as `result.trace`. The agent counted
the words, asked the sub-model for a summary, then submitted.

```json
{
  "header": {
    "status": "succeeded",
    "stop_reason": "submit",
    "model": "openai/gpt-5",
    "duration_ms": 4300,
    "usage": {
      "input_tokens": 5300,
      "output_tokens": 390,
      "cache_read_input_tokens": null,
      "cache_creation_input_tokens": null,
      "cost_usd": 0.0121
    },
    "turn_count": 2,
    "tool_call_count": 1,
    "external_run_id": "run_7c1e",
    "session_id": null,
    "native": {
      "format": "predict_rlm.run/v1",
      "sub_model": "openai/gpt-5-mini",
      "max_iterations": 10,
      "usage": {
        "main": {"input_tokens": 5000, "output_tokens": 370, "cost": 0.012, "cache_hits": 0},
        "sub": {"input_tokens": 300, "output_tokens": 20, "cost": 0.0001, "cache_hits": 0}
      },
      "telemetry_ref": null
    }
  },
  "turns": [
    {
      "index": 1,
      "message": "Count the words, then summarize with predict().",
      "reasoning": null,
      "tool_calls": [
        {
          "call_id": null,
          "caller": "code",
          "name": "predict",
          "arguments": {"text": "Avalanche runs DAGs."},
          "result": {"summary": "Avalanche runs DAGs."},
          "error": null,
          "duration_ms": 900
        }
      ],
      "duration_ms": 3100,
      "usage": {
        "input_tokens": 2700,
        "output_tokens": 330,
        "cache_read_input_tokens": null,
        "cache_creation_input_tokens": null,
        "cost_usd": 0.0062
      },
      "native": {
        "format": "predict_rlm.run/v1",
        "code": "n = len(document.split())\ns = predict(\"text -> summary\", text=document)\nprint(n, s.summary)",
        "output": "3 Avalanche runs DAGs.\n",
        "untruncated_output": "3 Avalanche runs DAGs.\n",
        "error": false,
        "finish_reason": "stop",
        "usage": {
          "main": {"input_tokens": 2400, "output_tokens": 310, "cost": 0.0061, "cache_hits": 0},
          "sub": {"input_tokens": 300, "output_tokens": 20, "cost": 0.0001, "cache_hits": 0}
        },
        "predict_calls": [
          {
            "signature": "text -> summary",
            "instructions": null,
            "model": "openai/gpt-5-mini",
            "usage": {"input_tokens": 300, "output_tokens": 20, "cost": 0.0001, "cache_hits": 0},
            "finish_reason": "stop"
          }
        ]
      }
    },
    {
      "index": 2,
      "message": "Submit.",
      "reasoning": null,
      "tool_calls": [],
      "duration_ms": 1200,
      "usage": {
        "input_tokens": 2600,
        "output_tokens": 60,
        "cache_read_input_tokens": null,
        "cache_creation_input_tokens": null,
        "cost_usd": 0.0059
      },
      "native": {
        "format": "predict_rlm.run/v1",
        "code": "SUBMIT(summary=s.summary, word_count=n)",
        "output": "",
        "untruncated_output": "",
        "error": false,
        "finish_reason": "stop",
        "usage": {
          "main": {"input_tokens": 2600, "output_tokens": 60, "cost": 0.0059, "cache_hits": 0},
          "sub": {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "cache_hits": 0}
        },
        "predict_calls": []
      }
    }
  ]
}
```

## Native trace header

The trace header's `native` part.

```python
class PredictRLMTraceHeader:
    format: Literal["predict_rlm.run/v1"]
    sub_model: str | None          # None when it is the main model
    max_iterations: int
    usage: PredictRLMUsage         # main and sub kept apart; the header adds them
    telemetry_ref: dict[str, JsonValue] | None


class PredictRLMUsage:
    main: PredictRLMTokenUsage
    sub: PredictRLMTokenUsage


class PredictRLMTokenUsage:
    input_tokens: int
    output_tokens: int
    cost: float
    cache_hits: int                # calls answered from cache
```

## Native turn

Each turn's `native` part: the iteration's code and what it printed.

```python
class PredictRLMTurn:
    format: Literal["predict_rlm.run/v1"]
    code: str
    output: str                    # as shown to the model
    untruncated_output: str        # in full
    error: bool                    # the code failed; the turn then counts as failed
    finish_reason: str | None
    usage: PredictRLMUsage         # main and sub kept apart
    predict_calls: tuple[PredictRLMPredictCall, ...]


class PredictRLMPredictCall:       # one predict() call
    signature: str
    instructions: str | None
    model: str
    usage: PredictRLMTokenUsage
    finish_reason: str | None
```

Each `predict()` call is also a `predict` tool call in the turn's `tool_calls`,
which hold its inputs, outputs, error and duration. `predict_calls` matches the
last tool calls, in the same order.

## Native live events

The `native` part of a live event, for what only PredictRLM reports. Every class
also has `format: Literal["predict_rlm.run/v1"]`. The comment names the `common`
event it is sent with. The run starting and tool calls are shared events alone,
with `native: None`.

```python
PredictRLMNativeEvent = (
    IterationRecorded | CodeGenerated | PredictStarted | PredictFinished
    | CodeExecuted | RunEnded | Lifecycle
)


class IterationRecorded:           # common: turn.finished, which holds the turn
    kind: Literal["iteration.recorded"]
    omissions: PredictRLMOmissions # the calls dropped to fit the turn into one event


class CodeGenerated:               # common: None; code about to run
    kind: Literal["code.generated"]
    iteration: int
    code: str


class PredictStarted:              # common: None
    kind: Literal["predict.started"]
    call_id: str
    signature: str
    instructions: str | None
    model: str


class PredictFinished:             # common: None
    kind: Literal["predict.finished"]
    call_id: str
    predict_error: str | None


class CodeExecuted:                # common: None
    kind: Literal["code.executed"]
    iteration: int
    output: str | None
    execution_error: str | None


class RunEnded:                    # common: None
    kind: Literal["run.failed", "run.cancelled"]
    error_type: str
    run_error: str


class Lifecycle:                   # common: None; sandbox steps
    kind: Literal[
        "input.prepared", "session.started", "packages.installed", "artifact.mounted",
        "output.reserved", "artifact.collected", "output.materialized",
        "session.finalized", "session.finalize_failed",
        "session.released", "session.release_failed",
    ]


class PredictRLMOmissions:
    reason: str
    tool_count: int
    predict_count: int
    predict_group_count: int
```

A `turn.finished` event has an `iteration.recorded` part only when the turn was
too large for one event and some of its calls were left out; a turn sent whole
has `native: None`.

The agent calling `predict()`, which has no shared event:

```json
{
  "kind": "event",
  "schema_version": 2,
  "invocation_id": "b81d2f04",
  "sequence": 4,
  "timestamp_ns": 1761480001200000000,
  "common": null,
  "native": {
    "format": "predict_rlm.run/v1",
    "kind": "predict.started",
    "call_id": "pc_1",
    "signature": "text -> summary",
    "instructions": null,
    "model": "openai/gpt-5-mini"
  }
}
```

The code's output:

```json
{
  "kind": "event",
  "schema_version": 2,
  "invocation_id": "b81d2f04",
  "sequence": 6,
  "timestamp_ns": 1761480002900000000,
  "common": null,
  "native": {
    "format": "predict_rlm.run/v1",
    "kind": "code.executed",
    "iteration": 1,
    "output": "3 Avalanche runs DAGs.\n",
    "execution_error": null
  }
}
```

## How PredictRLM fills the shared data

### Request

| `AgentRequest` | PredictRLM |
| --- | --- |
| `signature` | The step's signature, as is. |
| `inputs` | Keyword arguments of the run. |
| `ava.File` inputs | Mounted in the sandbox from where they are, without copies. |
| `ava.Workspace` inputs | Mounted in the sandbox, read-only. |
| `tools` | Functions the agent's code can call by name. |
| `skills` | Not supported yet. A step with skills fails with `AgentStepError` before the agent starts. |
| `invocation_id`, `step_name` | Not sent. |

### Live events

| `common.kind` | PredictRLM event |
| --- | --- |
| `run.started` | `run.started`. `model` is the `lm` setting by name, `None` when the run uses DSPy's current model; `inputs` are the step's inputs, files and workspaces by reference, not the paths mounted in the sandbox. |
| `turn.finished` | `iteration.recorded` |
| `tool.started`, `tool.finished` | `tool.started`, `tool.finished`. `turn` is `None`: PredictRLM does not say which iteration's code called the tool. |
| `None` | `predict.started`, `predict.finished`, `code.generated`, `code.executed`, `run.failed`, `run.cancelled` and the sandbox events |

PredictRLM's `run.succeeded` is not sent: the final event says the run succeeded
and carries its outputs.

### Final event

Sent once the run has ended and its outputs, if any, are built.

| Final event | PredictRLM |
| --- | --- |
| `trace_finished` | PredictRLM has a trace of the run. `outputs` are the values from `SUBMIT(...)`, with `ava.File` outputs as the file references the step returns. `outputs` is `None` when the run raised, or when a file output is not usable; the step then raises. |
| `trace_unavailable` | The run raised without a trace. |

The final event for a step with a `report: ava.File` output:

```json
{
  "kind": "trace_finished",
  "schema_version": 2,
  "invocation_id": "b81d2f04",
  "header": { "...": "as in the trace example" },
  "outputs": {
    "report": {
      "kind": "file",
      "workspace": "delta-ws:v1:snapshot-id",
      "path": ".outputs/summarize/1/5e0c2a9b7d4f4c1e8a63f0b2d91c7a48/report.md",
      "name": "report.md",
      "content_type": "text/markdown",
      "size_bytes": 412,
      "sha256": "3a7bd3e2360a3d29eea436fcfb7e44c7..."
    }
  },
  "omitted_outputs": [],
  "complete": true
}
```

### Trace header

| Header field | PredictRLM |
| --- | --- |
| `status` | `"succeeded"`, `"failed"` or `"cancelled"`, from how the run ended. |
| `stop_reason` | `"submit"` when the agent submitted, `"max_iterations"` when it ran out, else `null`. |
| `model` | The main model. A Codex model shows as `codex/<model>`. |
| `usage` | Main model plus sub-model. |
| `turn_count` | Number of iterations. |
| `tool_call_count` | Tool calls across all iterations, `predict()` calls included. |
| `external_run_id` | PredictRLM's run ID. |
| `session_id` | `null` |

### Turns

One turn per iteration.

| Turn field | PredictRLM iteration |
| --- | --- |
| `message` | What the model wrote before its code (PredictRLM calls it reasoning). |
| `reasoning` | `null`. The model provider's own reasoning is not captured. |
| `tool_calls` | Each tool call its code made, then each `predict()` call as a `predict` tool call. |
| `duration_ms` | The iteration's duration. |
| `usage` | Main model plus sub-model, for the iteration. |
| `native` | A `PredictRLMTurn`: the code, its output, and the `predict()` details. |

Tool calls have `caller: "code"` and `call_id: null`. A tool call's `arguments` is
`{"args": [...], "kwargs": {...}}`; a `predict` call's `arguments` and `result`
are the call's inputs and outputs.

### TokenUsage

| Field | PredictRLM |
| --- | --- |
| `input_tokens`, `output_tokens` | Main plus sub. |
| `cost_usd` | Main plus sub. |
| `cache_read_input_tokens`, `cache_creation_input_tokens` | `null`. PredictRLM counts cached calls instead, as `cache_hits` in the header's `native` part. |

### Outputs

Python values from `SUBMIT(...)`.

`ava.File` outputs are written to a fresh folder in the run workspace,
`.outputs/<step>/<attempt>/<call>/`, and returned as `ava.File` references.
