# Agent runtimes

An agent step can run on different engines, called runtimes. Avalanche supports
three:

| Runtime | Page |
| --- | --- |
| `ava.runtimes.PredictRLM(...)` (default) | [PredictRLM](runtimes/predict-rlm.md) |
| `ava.runtimes.ClaudeAgent(...)` | [ClaudeAgent](runtimes/claude-agent.md) |
| `ava.runtimes.OpenAIAgent(...)` | [OpenAIAgent](runtimes/openai-agent.md) |

Your step, its result and its trace look the same whatever runtime runs it. This
page shows that shared data. Each runtime page shows the runtime's settings, the
data only it has, and how it fills the shared data. Every model, without
explanations, is listed in [Agent runtime models](agent-runtime-models.md).

## Calling an agent step

```python
import avalanche as ava


class Summarize(ava.Signature):
    """Summarize the document in one sentence."""

    document: str = ava.InputField(desc="The text to summarize.")
    summary: str = ava.OutputField(desc="One sentence.")
    word_count: int = ava.OutputField(desc="Words in the document.", ge=0)


def lookup_term(term: str) -> str:
    """Return the glossary definition of a term."""
    return "A data-flow graph."


@ava.agent_step(
    Summarize,
    runtime=ava.runtimes.ClaudeAgent(),
    tools=[lookup_term],
    skills=[ava.agent.Skill.from_dir("skills/plain-summaries")],
)
async def summarize(document: str, *, agent: ava.Agent) -> str:
    result = await agent(document=document)
    return result.outputs.summary
```

`skills/plain-summaries/` is an [Agent Skills](https://agentskills.io/specification)
folder: a `SKILL.md` file with the skill's name, description and instructions,
and any files they refer to.

To run the step on PredictRLM, change `runtime=`. PredictRLM does not run
`ava.agent.Skill` yet, so drop `skills=` too:

```python
@ava.agent_step(Summarize, runtime=ava.runtimes.PredictRLM(), tools=[lookup_term])
```

Or set one runtime for every step of a workflow:

```python
@ava.workflow(agent_defaults={"runtime": ava.runtimes.ClaudeAgent(model="claude-haiku-5-5")})
def summaries():
    return load_document() >> summarize()
```

The examples below all come from this step, called with
`document="Avalanche runs DAGs."`.

## What happens in one call

1. Avalanche validates your inputs and builds an **`AgentRequest`**: the
   signature, the inputs, the tools, the skills.
2. The runtime runs it on its engine. While it runs, it sends **live events**:
   the call started, a tool started, a turn finished.
3. Once it has the outputs, the runtime sends the **final event**: the trace
   header and the outputs. It then returns an **`AgentRunOutcome`**: the raw
   outputs and the **trace**, both already in Avalanche's shape.
4. Avalanche validates the outputs against the signature and gives your step an
   **`AgentResult`**.

The runtime never decides whether outputs are valid. Avalanche does, the same way
for every runtime.

## `AgentResult`

What `await agent(...)` returns.

```python
class AgentResult:
    outputs: BaseModel   # your signature's outputs, validated
    trace: AgentTrace    # what happened during the call
```

Outputs are never coerced. If the runtime returns `"3"` for `word_count`, the
call fails with `ava.agent.AgentOutputError`.

## `AgentTrace`

The same shape for every runtime. It is the trace your process holds, as
`result.trace`, and it always has every turn. The operator, TUI and web UI never
receive it whole: each turn arrives as a `turn.finished` [live event](#live-events),
and the [final event](#the-final-event) carries the header.

```python
class AgentTrace:
    header: AgentTraceHeader
    turns: tuple[Turn, ...]
```

As JSON, for the Claude call above:

```json
{
  "header": {
    "status": "succeeded",
    "stop_reason": "success",
    "model": "claude-haiku-5-5",
    "duration_ms": 5400,
    "usage": {
      "input_tokens": 1200,
      "output_tokens": 80,
      "cache_read_input_tokens": 900,
      "cache_creation_input_tokens": 0,
      "cost_usd": 0.0042
    },
    "turn_count": 2,
    "tool_call_count": 2,
    "external_run_id": "9f2c-session",
    "session_id": "9f2c-session",
    "native": {
      "format": "claude_agent.messages/v1",
      "result_subtype": "success",
      "num_turns": 3,
      "duration_api_ms": 4100,
      "permission_denials": 0,
      "errors": []
    }
  },
  "turns": [
    {
      "index": 1,
      "message": "Let me check the glossary.",
      "reasoning": null,
      "tool_calls": [
        {
          "call_id": "toolu_01",
          "caller": "model",
          "name": "mcp__avalanche__lookup_term",
          "arguments": {"term": "DAG"},
          "result": [{"type": "text", "text": "\"A data-flow graph.\""}],
          "error": null,
          "duration_ms": null
        }
      ],
      "duration_ms": null,
      "usage": {
        "input_tokens": 600,
        "output_tokens": 40,
        "cache_read_input_tokens": 450,
        "cache_creation_input_tokens": 0,
        "cost_usd": null
      },
      "native": {
        "format": "claude_agent.messages/v1",
        "message_id": "msg_01",
        "model": "claude-haiku-5-5",
        "stop_reason": "tool_use",
        "api_error": null
      }
    },
    {
      "index": 2,
      "message": null,
      "reasoning": null,
      "tool_calls": [
        {
          "call_id": "toolu_02",
          "caller": "model",
          "name": "StructuredOutput",
          "arguments": {"summary": "Avalanche runs DAGs.", "word_count": 3},
          "result": "Structured output provided successfully",
          "error": null,
          "duration_ms": null
        }
      ],
      "duration_ms": null,
      "usage": {
        "input_tokens": 600,
        "output_tokens": 40,
        "cache_read_input_tokens": 450,
        "cache_creation_input_tokens": 0,
        "cost_usd": null
      },
      "native": {
        "format": "claude_agent.messages/v1",
        "message_id": "msg_02",
        "model": "claude-haiku-5-5",
        "stop_reason": "tool_use",
        "api_error": null
      }
    }
  ]
}
```

The same call on PredictRLM is on the [PredictRLM page](runtimes/predict-rlm.md#trace-example).

### `AgentTraceHeader`

```python
class AgentTraceHeader:
    status: Literal["succeeded", "failed", "cancelled"]
    stop_reason: str | None       # why it ended, in the runtime's words
    model: str | None             # the main model
    duration_ms: int
    usage: TokenUsage | None      # the whole call
    turn_count: int
    tool_call_count: int          # across all turns
    external_run_id: str | None   # the runtime's own ID for this run
    session_id: str | None        # the runtime's session ID, when it has one
    native: PredictRLMTraceHeader | ClaudeAgentTraceHeader | OpenAIAgentTraceHeader
                                  # see each runtime page

    @property
    def runtime_kind(self) -> Literal["predict_rlm", "claude_agent", "openai_agent"]: ...
                                  # from native
    @property
    def trace_format(self) -> str: ...  # native.format, the shape of every native part
```

`runtime_kind` and `trace_format` are read from the `native` part, so they are not
in the JSON.

The header travels with every run snapshot, so its `native` part is kept under
64 KiB. Anything larger belongs in the turns.

### `Turn`

One model response and the tool calls that followed it.

```python
class Turn:
    index: int                    # from 1
    message: str | None           # what the model wrote
    reasoning: str | None         # the model's own reasoning, when the runtime exposes it
    tool_calls: tuple[ToolCall, ...]  # in order
    duration_ms: int | None
    usage: TokenUsage | None      # this turn, every model it called added up
    native: PredictRLMTurn | ClaudeAgentTurn | OpenAIAgentTurn  # see each runtime page
```

A turn has failed when its runtime says so: PredictRLM's code failed, Claude
got an API error, or an OpenAI model refused.

### `ToolCall`

`caller` is `"model"` when the model asked for the call, and `"code"` when code
the agent ran made it. `name` is the tool's name as the engine knows it, so
ClaudeAgent records `lookup_term` as `mcp__avalanche__lookup_term`. `result` is
what the engine gave the model; each runtime page says how it records it.

```python
class ToolCall:
    call_id: str | None
    caller: Literal["model", "code"]
    name: str
    arguments: JsonValue
    result: JsonValue
    error: str | None
    duration_ms: int | None
```

```json
{
  "call_id": "toolu_01",
  "caller": "model",
  "name": "mcp__avalanche__lookup_term",
  "arguments": {"term": "DAG"},
  "result": [{"type": "text", "text": "\"A data-flow graph.\""}],
  "error": null,
  "duration_ms": null
}
```

### `TokenUsage`

`None` means the runtime doesn't report that figure.

```python
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None      # read from the prompt cache
    cache_creation_input_tokens: int | None  # written to the prompt cache
    cost_usd: float | None
```

### File and workspace values

A file or workspace in the call's inputs or outputs is recorded by its reference,
never its bytes or host path (see [file references](file-references.md)), in the
`run.started` event and the final event alike, whatever the runtime. Tool values
depend on the runtime: PredictRLM records files in tool arguments and results the
same way. ClaudeAgent records a tool's result as the JSON Claude got, so an
`ava.File` a tool returns appears in its JSON form, and an `ava.Workspace` holding
its files appears with their contents. OpenAIAgent records a tool's result as the
JSON text the model got, with the same forms.

```python
class FileInputEvidence:            # an ava.File
    kind: Literal["file"]
    workspace: str                  # the workspace reference
    path: str                       # relative to the workspace
    name: str | None
    content_type: str | None
    size_bytes: int
    sha256: str


class WorkspaceInputEvidence:       # an ava.Workspace holding its files
    kind: Literal["workspace"]
    entry_count: int
    total_size_bytes: int


class WorkspaceReferenceInputEvidence:  # an ava.Workspace pointing to a folder
    kind: Literal["workspace_reference"]
    reference: str
```

```json
{
  "kind": "file",
  "workspace": "delta-ws:v1:snapshot-id",
  "path": ".inputs/document/report.pdf",
  "name": "report.pdf",
  "content_type": "application/pdf",
  "size_bytes": 48213,
  "sha256": "9b74c9897bac770ffc029102a200c5de..."
}
```

## Live events

Sent while the call runs. The TUI and web UI show them as the run goes, and the
operator stores them with the run. Each event has a shared part, `common`, the
runtime's own part, `native`, or both.

```python
class AgentLiveEvent:
    kind: Literal["event"]
    schema_version: Literal[2]
    invocation_id: str
    sequence: int                 # from 1, in order within the call
    timestamp_ns: int             # when Avalanche received it
    common: AgentEvent | None     # None when the event means nothing outside its runtime
    native: PredictRLMNativeEvent | ClaudeAgentNativeEvent | OpenAIAgentNativeEvent | None
                                  # see each runtime page;
                                  # None when common says everything

    @property
    def runtime_kind(self) -> Literal["predict_rlm", "claude_agent", "openai_agent"] | None: ...
```

At least one of `common` and `native` is set. `runtime_kind` is read from the
native part, or from the turn of a `turn.finished` event, so it is not in the
JSON; it is `None` for an event with only a `run.started`, `tool.started` or
`tool.finished` part.

```json
{
  "kind": "event",
  "schema_version": 2,
  "invocation_id": "3f9a0c7e",
  "sequence": 3,
  "timestamp_ns": 1761480000123000000,
  "common": {
    "kind": "tool.started",
    "call_id": "toolu_01",
    "name": "mcp__avalanche__lookup_term",
    "turn": 1
  },
  "native": null
}
```

### `AgentEvent`

The shared events. Events only one runtime has, such as PredictRLM's
`predict()` calls, have `common: None` and are on the runtime's page.

```python
AgentEvent = RunStarted | TurnFinished | ToolStarted | ToolFinished


class RunStarted:                 # the first event of every call
    kind: Literal["run.started"]
    model: str | None             # the model the runtime asked for; None: the engine's default
    inputs: dict[str, JsonValue]  # the step's inputs, files and workspaces by reference
    omitted_inputs: tuple[str, ...]  # inputs left out because they are too large


class TurnFinished:
    kind: Literal["turn.finished"]
    turn: Turn
    omitted_tool_calls: int       # tool calls dropped to fit a turn into one event


class ToolStarted:
    kind: Literal["tool.started"]
    call_id: str
    name: str
    turn: int | None              # the turn that called the tool, when the runtime knows it


class ToolFinished:
    kind: Literal["tool.finished"]
    call_id: str
    name: str
    turn: int | None
    error: str | None
```

The inputs together are kept up to 4 MiB as JSON. Past that, the largest are left
out, one by one, and named in `omitted_inputs`.

The call starting, as sent:

```json
{
  "kind": "event",
  "schema_version": 2,
  "invocation_id": "3f9a0c7e",
  "sequence": 1,
  "timestamp_ns": 1761480000001000000,
  "common": {
    "kind": "run.started",
    "model": "claude-haiku-5-5",
    "inputs": {"document": "Avalanche runs DAGs."},
    "omitted_inputs": []
  },
  "native": null
}
```

A finished turn, as sent:

```json
{
  "kind": "event",
  "schema_version": 2,
  "invocation_id": "3f9a0c7e",
  "sequence": 5,
  "timestamp_ns": 1761480002100000000,
  "common": {
    "kind": "turn.finished",
    "turn": {
      "index": 1,
      "message": "Let me check the glossary.",
      "reasoning": null,
      "tool_calls": [
        {
          "call_id": "toolu_01",
          "caller": "model",
          "name": "mcp__avalanche__lookup_term",
          "arguments": {"term": "DAG"},
          "result": [{"type": "text", "text": "\"A data-flow graph.\""}],
          "error": null,
          "duration_ms": null
        }
      ],
      "duration_ms": null,
      "usage": {
        "input_tokens": 600,
        "output_tokens": 40,
        "cache_read_input_tokens": 450,
        "cache_creation_input_tokens": 0,
        "cost_usd": null
      },
      "native": {
        "format": "claude_agent.messages/v1",
        "message_id": "msg_01",
        "model": "claude-haiku-5-5",
        "stop_reason": "tool_use",
        "api_error": null
      }
    },
    "omitted_tool_calls": 0
  },
  "native": null
}
```

### The final event

Every call the runtime starts ends with exactly one final event, also when it
fails or is cancelled. A call rejected before it starts, for example for invalid
inputs or a field type the runtime does not support, sends none. `complete` says
whether every live event of the call was delivered.

```python
class AgentTraceFinished:         # the call has a trace
    kind: Literal["trace_finished"]
    schema_version: Literal[2]
    invocation_id: str
    header: AgentTraceHeader      # the turns went out as turn.finished events
    outputs: dict[str, JsonValue] | None  # None when the run returned no outputs
    omitted_outputs: tuple[str, ...]      # outputs left out because they are too large
    complete: bool


class AgentTraceUnavailable:      # the call ended without a trace
    kind: Literal["trace_unavailable"]
    schema_version: Literal[2]
    invocation_id: str
    runtime_kind: Literal["predict_rlm", "claude_agent", "openai_agent"]
    status: Literal["succeeded", "failed", "cancelled"]
    error: str
    external_run_id: str | None
    complete: bool
```

The runtime sends the final event once it has the outputs. `outputs` are the raw
outputs as the runtime returned them, before Avalanche validates them, with files
and workspaces by reference. As with inputs, they are kept up to 4 MiB together,
and the largest beyond that are named in `omitted_outputs`. `outputs` is `None`
when the run ended without outputs, for example when it failed, or when turning
the engine's answer into outputs failed; the step then raises after the final
event.

```json
{
  "kind": "trace_finished",
  "schema_version": 2,
  "invocation_id": "3f9a0c7e",
  "header": { "...": "as in the trace above" },
  "outputs": {"summary": "Avalanche runs DAGs.", "word_count": 3},
  "omitted_outputs": [],
  "complete": true
}
```

```json
{
  "kind": "trace_unavailable",
  "schema_version": 2,
  "invocation_id": "3f9a0c7e",
  "runtime_kind": "claude_agent",
  "status": "cancelled",
  "error": "the agent call was cancelled",
  "external_run_id": "9f2c-session",
  "complete": true
}
```

## `AgentRequest`

What the runtime receives.

```python
class AgentRequest:
    invocation_id: str            # new for each call
    step_name: str
    signature: SignatureSpec
    inputs: BaseModel             # an instance of signature.input_model, validated
    tools: tuple[ToolSpec, ...]
    skills: tuple[Skill, ...]     # names are unique


class SignatureSpec:
    name: str
    instructions: str
    input_model: type[BaseModel]  # one required field per input
    output_model: type[BaseModel] # one required field per output
    dspy_signature: type[dspy.Signature]  # the step's signature, for DSPy-based runtimes
    inputs: tuple[FieldSpec, ...]   # read-only, from input_model
    outputs: tuple[FieldSpec, ...]  # read-only, from output_model


class FieldSpec:
    name: str
    description: str | None
    json_schema: dict[str, JsonValue]  # the field's type, constraints included


class ToolSpec:
    name: str
    description: str | None       # the function's docstring
    function: Callable[..., object]


class Skill:                      # ava.agent.Skill, in the Agent Skills format
    name: str                     # lowercase letters, digits and hyphens, at most 64
    description: str              # what it does and when to use it; the agent sees this first
    instructions: str             # the SKILL.md body, read when the agent uses the skill
    files: Mapping[str, Path] = {}  # path inside the skill folder -> host file
    license: str | None = None
    compatibility: str | None = None
    metadata: Mapping[str, str] = {}
    allowed_tools: str | None = None
```

For the call above: `step_name="summarize"`, a `Summarize` signature,
`inputs=SummarizeInputs(document="Avalanche runs DAGs.")`, one tool,
`lookup_term`, and one skill, `plain-summaries`.

A runtime that cannot offer skills to its agent rejects a request that has some.

`ava.File` and `ava.Workspace` inputs are passed as they are. Each runtime page
says how its engine gets them.

## `AgentRunOutcome`

What the runtime returns. Avalanche validates `outputs` into `result.outputs`.

```python
class AgentRunOutcome:
    outputs: PythonOutputs | JsonOutputs   # raw, not yet validated
    trace: AgentTrace


class PythonOutputs:              # Python values, validated in strict Python mode
    kind: Literal["python"]
    values: Mapping[str, object]


class JsonOutputs:                # a JSON document, validated in strict JSON mode
    kind: Literal["json"]
    text: str
```

PredictRLM returns `PythonOutputs`. ClaudeAgent returns `JsonOutputs`, or
`PythonOutputs` when the signature has `ava.File` or `ava.Workspace` outputs.
OpenAIAgent returns `JsonOutputs`, or `PythonOutputs` when the signature has
`ava.File` or `ava.Workspace` outputs.

```python
PythonOutputs(values={"summary": "Avalanche runs DAGs.", "word_count": 3})
JsonOutputs(text='{"summary": "Avalanche runs DAGs.", "word_count": 3}')
```
