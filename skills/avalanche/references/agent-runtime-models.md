# Agent runtime models

Every data model of the agent runtime layer, grouped by its four top-level
models: `AgentRequest`, `AgentResult`, `AgentTrace` and `AgentLiveEvent`. For
what they mean and examples, see [Agent runtimes](agent-runtime-contract.md).

All models are frozen, strict Pydantic models that reject unknown fields.
`SignatureSpec`, `ToolSpec`, `AgentRequest`, `PythonOutputs`, `AgentRunOutcome`
and `AgentResult` hold live Python objects and are never serialized.

```python
RuntimeKind = Literal["predict_rlm", "claude_agent"]
```

## Request

`avalanche.agent.contract`, `avalanche.agent.skills`

```python
class AgentRequest:
    invocation_id: str
    step_name: str
    signature: SignatureSpec
    inputs: BaseModel                    # an instance of signature.input_model
    tools: tuple[ToolSpec, ...]
    skills: tuple[Skill, ...]            # names are unique


class SignatureSpec:
    name: str
    instructions: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    dspy_signature: type[dspy.Signature]

    @property
    def inputs(self) -> tuple[FieldSpec, ...]: ...
    @property
    def outputs(self) -> tuple[FieldSpec, ...]: ...


class FieldSpec:
    name: str
    description: str | None
    json_schema: dict[str, JsonValue]


class ToolSpec:
    name: str
    description: str | None
    function: Callable[..., object]


SkillName = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
]


class Skill:                             # ava.agent.Skill
    name: SkillName
    description: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    instructions: str
    files: Mapping[str, Path] = {}
    license: str | None = None
    compatibility: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None
    metadata: Mapping[str, str] = {}
    allowed_tools: str | None = None
```

## Result

`avalanche.agent.contract`

```python
class AgentResult:
    outputs: BaseModel                   # an instance of signature.output_model
    trace: AgentTrace


class AgentRunOutcome:
    outputs: AgentOutputs
    trace: AgentTrace


AgentOutputs = Annotated[PythonOutputs | JsonOutputs, Field(discriminator="kind")]


class PythonOutputs:
    kind: Literal["python"] = "python"
    values: Mapping[str, object]


class JsonOutputs:
    kind: Literal["json"] = "json"
    text: str
```

## Trace

`avalanche.agent.trace`, `avalanche.agent.native`

```python
class AgentTrace:
    schema_version: Literal[2] = 2
    header: AgentTraceHeader
    turns: tuple[Turn, ...]              # () when turns_streamed
    turns_streamed: bool


AgentTraceStatus = Literal["succeeded", "failed", "cancelled"]


class AgentTraceHeader:
    runtime_kind: RuntimeKind
    trace_format: str                    # equals native.format
    status: AgentTraceStatus
    stop_reason: str | None
    model: str | None
    duration_ms: NonNegativeInt
    usage: TokenUsage | None
    turn_count: NonNegativeInt
    tool_call_count: NonNegativeInt
    external_run_id: str | None
    session_id: str | None
    native: TraceHeaderNative            # at most 64 KiB as JSON


class Turn:
    index: PositiveInt
    message: str | None
    reasoning: str | None
    tool_calls: tuple[ToolCall, ...]
    duration_ms: NonNegativeInt | None
    usage: TokenUsage | None
    native: TurnNative

    @property
    def failed(self) -> bool: ...


class ToolCall:
    call_id: str | None
    caller: Literal["model", "code"]
    name: str
    arguments: JsonValue
    result: JsonValue
    error: str | None
    duration_ms: NonNegativeInt | None


class TokenUsage:
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cache_read_input_tokens: NonNegativeInt | None = None
    cache_creation_input_tokens: NonNegativeInt | None = None
    cost_usd: NonNegativeFloat | None = None
```

### File and workspace values

How an `ava.File` or `ava.Workspace` appears in a trace or live event's JSON.

```python
FileInputEvidenceValue = Annotated[
    FileInputEvidence | WorkspaceInputEvidence | WorkspaceReferenceInputEvidence,
    Field(discriminator="kind"),
]


class FileInputEvidence:
    kind: Literal["file"] = "file"
    workspace: str
    path: str
    name: str | None
    content_type: str | None
    size_bytes: NonNegativeInt
    sha256: str


class WorkspaceInputEvidence:
    kind: Literal["workspace"] = "workspace"
    entry_count: NonNegativeInt
    total_size_bytes: NonNegativeInt


class WorkspaceReferenceInputEvidence:
    kind: Literal["workspace_reference"] = "workspace_reference"
    reference: str
```

### Native parts

```python
TraceHeaderNative = Annotated[
    PredictRLMTraceHeader | ClaudeAgentTraceHeader, Field(discriminator="format")
]
TurnNative = Annotated[PredictRLMTurn | ClaudeAgentTurn, Field(discriminator="format")]
```

PredictRLM, `avalanche.agent.native.predict_rlm`:

```python
class PredictRLMTraceHeader:
    format: Literal["predict_rlm.run/v1"] = "predict_rlm.run/v1"
    sub_model: str | None
    max_iterations: PositiveInt
    usage: PredictRLMUsage
    telemetry_ref: dict[str, JsonValue] | None


class PredictRLMTurn:
    format: Literal["predict_rlm.run/v1"] = "predict_rlm.run/v1"
    code: str
    output: str
    untruncated_output: str
    error: bool
    finish_reason: str | None
    usage: PredictRLMUsage
    predict_calls: tuple[PredictRLMPredictCall, ...]  # the turn's last tool calls, in order


class PredictRLMPredictCall:
    signature: str
    instructions: str | None
    model: str
    usage: PredictRLMTokenUsage
    finish_reason: str | None


class PredictRLMUsage:
    main: PredictRLMTokenUsage
    sub: PredictRLMTokenUsage


class PredictRLMTokenUsage:
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cost: NonNegativeFloat
    cache_hits: NonNegativeInt
```

ClaudeAgent, `avalanche.agent.native.claude_agent`:

```python
class ClaudeAgentTraceHeader:
    format: Literal["claude_agent.messages/v1"] = "claude_agent.messages/v1"
    result_subtype: str | None
    num_turns: NonNegativeInt
    duration_api_ms: NonNegativeInt | None
    permission_denials: NonNegativeInt
    errors: tuple[str, ...]              # at most 8, each at most 1024 characters


class ClaudeAgentTurn:
    format: Literal["claude_agent.messages/v1"] = "claude_agent.messages/v1"
    message_id: str | None
    model: str
    stop_reason: str | None
    api_error: str | None
```

## Live events

`avalanche.agent.live`, `avalanche.agent.native`

```python
AgentEvidence = Annotated[
    AgentLiveEvent | AgentTraceFinished | AgentTraceUnavailable,
    Field(discriminator="kind"),
]


class AgentLiveEvent:
    kind: Literal["event"] = "event"
    schema_version: Literal[2] = 2
    invocation_id: str                   # min_length=1
    sequence: PositiveInt
    timestamp_ns: NonNegativeInt
    runtime_kind: RuntimeKind
    common: AgentEvent | None
    native: NativeEvent


AgentEvent = Annotated[TurnFinished | ToolStarted | ToolFinished, Field(discriminator="kind")]
CommonEventKind = Literal["turn.finished", "tool.started", "tool.finished"]


class TurnFinished:
    kind: Literal["turn.finished"] = "turn.finished"
    turn: Turn
    omitted_tool_calls: NonNegativeInt


class ToolStarted:
    kind: Literal["tool.started"] = "tool.started"
    call_id: str
    name: str


class ToolFinished:
    kind: Literal["tool.finished"] = "tool.finished"
    call_id: str
    name: str
    error: str | None
```

### Final events

```python
AgentTraceTerminal = Annotated[
    AgentTraceFinished | AgentTraceUnavailable, Field(discriminator="kind")
]


class AgentTraceFinished:
    kind: Literal["trace_finished"] = "trace_finished"
    schema_version: Literal[2] = 2
    invocation_id: str                   # min_length=1
    record: AgentTrace                   # turns_streamed is True
    complete: bool


class AgentTraceUnavailable:
    kind: Literal["trace_unavailable"] = "trace_unavailable"
    schema_version: Literal[2] = 2
    invocation_id: str                   # min_length=1
    runtime_kind: RuntimeKind
    status: AgentTraceStatus
    error: str
    external_run_id: str | None
    complete: bool
```

### Native events

```python
NativeEvent = Annotated[
    PredictRLMNativeEvent | ClaudeAgentNativeEvent, Field(discriminator="format")
]
```

PredictRLM, `avalanche.agent.native.predict_rlm`. Every class also has
`format: Literal["predict_rlm.run/v1"] = "predict_rlm.run/v1"`; the comment is
the `common` event it travels with.

```python
PredictRLMNativeEvent = Annotated[
    RunStarted | Lifecycle | IterationRecorded | CodeGenerated | PredictStarted
    | PredictFinished | Correlated | CodeExecuted | RunSucceeded | RunEnded,
    Field(discriminator="kind"),
]


class RunStarted:                        # None
    kind: Literal["run.started"] = "run.started"
    input_fields: tuple[str, ...]
    inputs: JsonValue


class IterationRecorded:                 # turn.finished
    kind: Literal["iteration.recorded"] = "iteration.recorded"
    omissions: PredictRLMOmissions | None


class PredictRLMOmissions:
    reason: str
    tool_count: NonNegativeInt
    predict_count: NonNegativeInt
    predict_group_count: NonNegativeInt


class CodeGenerated:                     # None
    kind: Literal["code.generated"] = "code.generated"
    iteration: NonNegativeInt
    code: str


class PredictStarted:                    # None
    kind: Literal["predict.started"] = "predict.started"
    call_id: str
    signature: str
    instructions: str | None
    model: str


class PredictFinished:                   # None
    kind: Literal["predict.finished"] = "predict.finished"
    call_id: str
    predict_error: str | None


class Correlated:                        # tool.started or tool.finished, same as kind
    kind: Literal["tool.started", "tool.finished"]


class CodeExecuted:                      # None
    kind: Literal["code.executed"] = "code.executed"
    iteration: NonNegativeInt
    output: str | None
    execution_error: str | None


class RunSucceeded:                      # None
    kind: Literal["run.succeeded"] = "run.succeeded"
    status: str
    outputs: JsonValue


class RunEnded:                          # None
    kind: Literal["run.failed", "run.cancelled"]
    error_type: str
    run_error: str


LifecycleKind = Literal[
    "input.prepared", "session.started", "packages.installed", "artifact.mounted",
    "output.reserved", "artifact.collected", "output.materialized",
    "session.finalized", "session.finalize_failed",
    "session.released", "session.release_failed",
]


class Lifecycle:                         # None
    kind: LifecycleKind
```

ClaudeAgent, `avalanche.agent.native.claude_agent`. Every class also has
`format: Literal["claude_agent.messages/v1"] = "claude_agent.messages/v1"`; the
comment is the `common` event it travels with.

```python
ClaudeAgentNativeEvent = Annotated[
    Started | SessionInitialized | ToolUsed | ToolReturned | MessageFinished
    | RateLimited | Finished,
    Field(discriminator="kind"),
]


class Started:                           # None
    kind: Literal["started"] = "started"
    model: str | None
    inputs: JsonValue


class SessionInitialized:                # None
    kind: Literal["session.initialized"] = "session.initialized"
    session_id: str | None
    model: str | None
    tools: tuple[str, ...]


class ToolUsed:                          # tool.started
    kind: Literal["tool.used"] = "tool.used"
    turn_index: NonNegativeInt


class ToolReturned:                      # tool.finished
    kind: Literal["tool.returned"] = "tool.returned"
    turn_index: NonNegativeInt


class MessageFinished:                   # turn.finished
    kind: Literal["message.finished"] = "message.finished"


class RateLimited:                       # None
    kind: Literal["rate_limit"] = "rate_limit"
    status: str
    limit_type: str | None


class Finished:                          # None
    kind: Literal["finished"] = "finished"
    subtype: str
    is_error: bool
    num_turns: NonNegativeInt
    structured_output: JsonValue
    output_omitted: bool
    workspace_outputs: tuple[str, ...]
    errors: tuple[str, ...]
```
