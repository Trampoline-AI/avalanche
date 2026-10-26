# Agents

Workflow-author agent API, including inline agent nodes. Dependency behavior below describes PredictRLM 0.9.0 and DSPy 3.2.1. See [workflows](workflows.md) for node calls, composition, and workflow default declarations; [inputs and context](inputs-context.md) for ordinary step injection.

## Agent step

```python
import avalanche as ava

@ava.agent_step(
    signature,
    runtime=ava.runtimes.PredictRLM(lm=..., sub_lm=..., max_iterations=..., skills=...),
    tools=...,
    evaluations=...,
)
async def analyze(document: str, *, agent: ava.Agent) -> str:
    result = await agent(document=document)
    return result.outputs.summary
```

The example assumes a signature with input `document` and output `summary`. Ellipses denote optional arguments to omit or replace, not values to pass.

| Parameter | Accepted value and default | Meaning |
| --- | --- | --- |
| `signature` | Required DSPy signature class | An Avalanche `Signature` subclass, the class returned by `Signature(...)`, or a native DSPy signature class. Must declare at least one output. Bare strings are not accepted. |
| `runtime` | An agent runtime such as `ava.runtimes.PredictRLM(...)`; default `None` | The runtime that runs the agent and its settings. Omitted uses the workflow's `agent_defaults` runtime, else `ava.runtimes.PredictRLM()`. See [runtimes](#runtimes). Any other value raises `TypeError`. |
| `tools` | Sequence of callables; default none | Additional tools; every member must be callable. |
| `evaluations` | `ava.Evaluations \| None`; default `None` | Observation-only quality judgments of the first successful agent call, run by the operator. See [native evaluations](../agent-steps.md#native-evaluations). Any other value raises `TypeError`. |

Model, skill and other PredictRLM settings belong to the runtime. Passing `lm=`, `sub_lm=`, `max_iterations=`, `skills=` or another PredictRLM keyword to the step raises `TypeError` naming the `runtime=ava.runtimes.PredictRLM(...)` replacement.

The decorator returns a single-output workflow step. The body must declare exactly `*, agent: ava.Agent`, with no default; Avalanche injects it and removes it from the step's public call signature. The remaining Python parameters and return annotation describe the workflow step, independently of the model signature. Synchronous and asynchronous bodies are accepted; an awaitable body result is awaited. Use an asynchronous body to await model calls.

Declare decorated bodies outside workflow construction. Both `@ava.agent_step(...)` and `@ava.agent.step(...)` support this form; nested agent decorators raise `AgentStepError`.

Decorated agent functions do not provide ordinary node-decorator options such as a node slug. The body chooses which output fields to return and whether to write files or tables; the decorator does not persist the body result to an application table.

### Inline call

Inside a workflow body, `ava.agent.step(signature, inputs=None, slug=None, ...)`
declares an inline agent node and returns its deferred result. It accepts the
same signature, `runtime`, `tools`, and `evaluations` listed above;
evaluations judge the node's single agent call. It does not
execute a model during graph construction or discovery.

| Inline-only parameter | Default and contract |
| --- | --- |
| `inputs` | `None`; optionally a mapping from signature input names to values or deferred node outputs. Unknown names raise `AgentStepError`. With implicit `>>` binding, inputs follow signature declaration order. |
| `slug` | `None`; optionally the node's rerun-addressable identifier, following ordinary node slug rules. |

One output field becomes its validated value; multiple fields become an ordered
tuple and separate downstream arguments. A single list- or tuple-valued field
remains one argument under both Local and Ray execution, even when empty.
Outputs are validated strictly against their field annotations and constraints;
missing fields raise `AgentStepError`, and invalid values raise Pydantic
`ValidationError`, without coercion, fallback, or retry.

Outside a workflow, `ava.agent.step(...)` returns a decorator instead.
Non-`None` `inputs=` or `slug=` in that context raises `TypeError`; these are
inline node options, not model inputs or predictor settings.

### Workflow defaults

Declare `agent_defaults={"runtime": ava.runtimes.PredictRLM(...)}` (or an `ava.agent.AgentDefaults`) on [the workflow decorator](workflows.md). Each step's effective runtime is:

1. The step's `runtime` merged over the workflow runtime when both are the same kind: fields the step sets win, fields only the workflow sets are kept.
2. The step's `runtime` alone when the workflow runtime is another kind.
3. The workflow runtime when the step sets none.
4. `ava.runtimes.PredictRLM()` when neither is set.

Other `agent_defaults` keys raise `TypeError` naming the `{"runtime": ...}` form. Model credentials and accepted model configurations follow the installed dependency, not Avalanche's classifier configuration.

## Runtimes

A runtime is the agent engine that runs an agent step. The step's signature, tools, outputs, evaluations and trace views work the same whatever runtime runs it; only the `runtime=` value changes.

| Runtime | How the agent works | Install |
| --- | --- | --- |
| `ava.runtimes.PredictRLM(...)` (default) | Writes and runs Python in a sandbox, with a second model for structured sub-predictions. | Included. |
| `ava.runtimes.ClaudeAgent(...)` | Runs Claude through the Claude Code CLI, using your step tools and any Claude Code tools you enable. | `uv add 'avalanche-ai[claude]'` and the Claude Code CLI. |

Choose one per step with `runtime=`, or for every step with the workflow's `agent_defaults` (see [workflow defaults](#workflow-defaults)):

```python
@ava.agent_step(Summarize, runtime=ava.runtimes.ClaudeAgent(model="claude-haiku-5-5"))
async def summarize(document: str, *, agent: ava.Agent) -> str:
    result = await agent(document=document)
    return result.outputs.summary
```

A runtime value only holds settings. Creating one starts nothing and imports nothing optional, so it can be used at workflow definition time and sent to Ray workers.

### PredictRLM

```python
ava.runtimes.PredictRLM(lm="openai/gpt-5", sub_lm="openai/gpt-5-mini", max_iterations=20)
```

| Field | Accepted value and default | Meaning |
| --- | --- | --- |
| `lm` | `dspy.LM \| str \| None`; default `None` | Main model, which writes the code. `None` uses the DSPy context configuration. |
| `sub_lm` | `dspy.LM \| str \| None`; default `None` | Model for the agent's structured prediction tool. `None` uses the DSPy context configuration. |
| `max_iterations` | `int`; default `30` | Most code-writing iterations. |
| `skills` | Sequence of `ava.agent.Skill`; default none | Capability bundles. |
| `output_dir` | `str \| pathlib.Path \| None`; default `None` | For steps without `ava.File` or `ava.Workspace` fields: where PredictRLM `File` outputs are collected, as `<output_dir>/<field>/`; otherwise a temporary directory. Steps with file fields reserve their outputs in the run workspace (see [File references](../file-references.md)) and raise `AgentStepError` when `output_dir` is set. Scalar outputs and logs are not redirected. |
| `verbose` | `bool`; default `False` | PredictRLM console output. Avalanche runs agents quietly; PredictRLM's own default is `True`. |

Every other `predict_rlm.PredictRLM` constructor option (`max_llm_calls`, `max_output_chars`, `sandbox_backend`, `allowed_domains`, `runtime_hooks`, and so on) is accepted with the same values and defaults. The signature and tools are not fields: they come from the step. Any other keyword is passed unchanged to the PredictRLM constructor, so an option Avalanche has no field for still works, and an unknown one raises PredictRLM's `TypeError` when the agent first runs.

### ClaudeAgent

```python
ava.runtimes.ClaudeAgent(model="claude-haiku-5-5", max_turns=10, builtin_tools=("Read", "Grep"))
```

| Field | Accepted value and default | Meaning |
| --- | --- | --- |
| `model` | `str \| None`; default `None` | Claude model, such as `"claude-haiku-5-5"`. `None` lets the Claude CLI choose. |
| `max_turns` | positive `int \| None`; default `None` | Most model turns before the session stops. |
| `builtin_tools` | Sequence of Claude Code tool names; default none | Built-in tools the agent may use without asking, such as `"Read"` or `"Bash"`. The step's own `tools=` are always available. |
| `permission_mode` | `"default"`, `"acceptEdits"`, `"plan"`, `"bypassPermissions"` or `"dontAsk"`; default `"default"` | Claude Code permission mode. |
| `system_prompt_append` | `str \| None`; default `None` | Text added after the signature's instructions in the system prompt. |
| `max_budget_usd` | positive `float \| None`; default `None` | Spending limit for the session. |

**Setup.** Install the `claude` extra and the Claude Code CLI on every machine that runs the step, Ray workers included. A workflow that only declares Claude steps imports and publishes without them; running a step without the SDK raises `AgentStepExecutionError` naming the extra.

**Sign-in.** The CLI signs in as it does in a terminal, using the first of:

- an existing `claude` login;
- `CLAUDE_CODE_OAUTH_TOKEN`, created for a Claude subscription with `claude setup-token`;
- `ANTHROPIC_API_KEY`.

**Isolation.** Each call ignores your own Claude setup: user and project settings are not loaded, and MCP servers and claude.ai connectors from your account are not offered. The agent sees only the step's tools and the `builtin_tools` you list.

**Inputs and outputs.** The signature's instructions are the system prompt. Inputs are sent as JSON; outputs come back as Claude's structured output and are validated against the signature like every runtime's.

**Files and workspaces.**

| Field type | Input | Output |
| --- | --- | --- |
| `ava.File`, `list[ava.File]` | Written into the session's working directory; Claude gets the relative paths. | Not supported; return an `ava.Workspace` instead. |
| `ava.Workspace` (at most one input) | Becomes the working directory. A reference workspace runs in place, so the agent's writes land in your directory; a value workspace runs in its temporary copy. | A snapshot of the working directory. A reference stays bound to its directory. |
| No workspace input | The session runs in an empty temporary directory. | |

Any other shape that holds files or a workspace, such as `ava.Workspace | None`, `tuple[ava.File, ...]` or a model with an `ava.File` field, raises `AgentStepError` before Claude is called.

## Signature declarations

```python
class Summarize(ava.Signature):
    """Summarize the supplied document."""

    document: str = ava.InputField(desc="Complete document text.")
    summary: str = ava.OutputField(desc="Concise factual summary.")
```

The class docstring supplies instructions, type annotations describe model inputs and outputs, and the field helpers mark their direction. Authors choose the field names and types, including structured Pydantic output models and [agent files](#agent-files). Tools and the runtime are step configuration, not signature fields.

The dynamic form returns a **signature class**, not a prediction:

```python
ava.Signature(
    signature: str | dict[str, tuple[type, FieldInfo]],
    instructions: str | None = None,
    signature_name: str = "StringSignature",
    custom_types: dict[str, type] | None = None,
) -> type[dspy.Signature]

ava.InputField(**kwargs) -> FieldInfo
ava.OutputField(**kwargs) -> FieldInfo
```

`FieldInfo` is Pydantic's field descriptor type. These declarations delegate to DSPy.

| Argument | Behavior |
| --- | --- |
| `signature` | String such as `"document: str -> summary: str"`, or a mapping of field names to `(type, InputField()/OutputField())`. In strings, omitted types default to `str`; exactly one `->` and distinct field names are required. |
| `instructions` | Explicit instruction text; `None` lets DSPy generate basic input/output instructions. |
| `signature_name` | Generated class name; default `"StringSignature"`. |
| `custom_types` | Optional name-to-type mapping for non-builtin types used in string signatures. |
| Field `desc` | Model-facing field description. `description` supplies it when `desc` is omitted. |
| Field `default`, `default_factory`, constraints | Ordinary Pydantic field options, forwarded by DSPy. Without a default/factory the descriptor is required. |

The field helpers accept keyword arguments only. Field declarations alone do not validate values; every agent call strictly validates its outputs, for inline and decorated steps alike. Malformed declarations or incompatible field options raise DSPy/Pydantic errors, including `ValueError` and `TypeError`. Signature classes expose `instructions`, `input_fields`, and `output_fields` for the declared contract; inherited dependency editing methods are outside this reference.

## Injected Agent call

```python
result = await agent(**inputs)
summary = result.outputs.summary  # the output name declared by the signature
```

`inputs` must contain **exactly** the signature's input names, even for fields with descriptor defaults. Positional input arguments, extra names, and missing names are not supported. Runtime configuration cannot be overridden through this call. Avalanche checks the names and validates each value against its declared input type before the runtime starts; a mismatch raises `AgentStepError`.

The return is an `ava.agent.AgentResult`, the same for every runtime:

| Field | Meaning |
| --- | --- |
| `outputs` | A frozen Pydantic model generated from the signature, with one attribute per output field. Each value is already validated in strict mode against its declared type, so a structured field such as `result.outputs.report` is already a `Report`. |
| `trace` | The invocation's `ava.agent.AgentTrace`: `trace.header` (runtime kind, status, stop reason, model, duration, token usage, turn and tool-call counts) and `trace.turns`, each with the code, tool calls and model calls of that turn. Runtime-specific details sit in `trace.native`. |

Outputs that are missing or do not match their declared types raise `AgentOutputError` after the invocation's trace is recorded; values are never coerced or defaulted.

Each step-body execution receives a fresh injected agent. Its first call constructs the predictor lazily; subsequent calls in that body reuse it. The body can make zero, one, or multiple calls. Calling a decorated step during workflow construction follows [node semantics](workflows.md), not this model-call interface.

## Skills

```python
ava.agent.Skill(
    *,
    name: str,
    instructions: str = "",
    packages: list[str] = [],
    modules: dict[str, str] = {},
    tools: dict[str, Callable[..., Any]] = {},
)
```

All arguments are keyword-only. This returns a PredictRLM `Skill`; the displayed container defaults are fresh per instance. All five fields are readable on the result.

| Field | Meaning |
| --- | --- |
| `name` | Required short identifier. |
| `instructions` | Instructions included in the agent prompt; default empty string. |
| `packages` | Python package requirements for the sandbox; default empty list. |
| `modules` | Import name to host Python module-file path; default empty mapping. |
| `tools` | Tool name to synchronous or asynchronous callable; default empty mapping. |

Invalid field values raise dependency Pydantic validation errors. Combining skills with duplicate module or tool names raises dependency `ValueError` when the predictor is built. Avalanche does not provide its own skill loader or dependency installer API.

Built-in skill objects are `ava.agent.skills.pdf`, `ava.agent.skills.docx`, and `ava.agent.skills.spreadsheet`. Pass the objects in the runtime's `skills=[...]`, for example `runtime=ava.runtimes.PredictRLM(skills=[ava.agent.skills.pdf])`; their instructions and package requirements belong to PredictRLM.

## Agent files

Agent signatures declare files with the workflow types [`ava.File` and `ava.Workspace`](files-workspaces.md), as whole fields or inside other types such as `list[ava.File]` or `dict[str, ava.File]`. `ava.agent.File` is a deprecated alias of `ava.File`.

With PredictRLM, `ava.File` and `ava.Workspace` inputs are mounted in the agent sandbox where they are on the host, without copying; workspaces are mounted read-only (advisory with `DirectPythonBackend`). `ava.File` outputs are written to a folder reserved for the step, its attempt and the call, `.outputs/<step>/<attempt>/<invocation>/` inside the run workspace, and come back in `result.outputs` as `ava.File` references to that workspace; see [File references](../file-references.md). A file output without a host path, outside the reserved folder, or missing raises `AgentOutputError`. Steps with file fields own their output folder: setting the PredictRLM `output_dir` raises `AgentStepError`.

## Errors

Import agent errors from `avalanche.agent`. `AgentStepError` and `AgentStepExecutionError` are separate direct `RuntimeError` subclasses; `AgentOutputError` subclasses `AgentStepExecutionError`.

| Error | Caller-visible condition |
| --- | --- |
| `AgentStepError` | Invalid injected `agent` parameter, invalid resolved input-field mapping, or missing/extra model-call input names. |
| `TypeError` | Missing/invalid signature, outputless signature, invalid skills/tools declaration, or forbidden workflow-default keys. Signature validation may occur during workflow metadata resolution or the first agent call rather than decoration. |
| Dependency construction errors | Invalid upstream options or incompatible skill/tool configuration. Predictor construction occurs before the call's execution-error wrapper, so these propagate directly. |
| `AgentOutputError` | The runtime's outputs do not match the signature: a field is missing or has the wrong type, or a PredictRLM `ava.File` output cannot be read back. Raised after the invocation's trace is recorded; no evaluation is submitted. Signature mismatches chain the Pydantic `ValidationError` as their cause. |
| `AgentStepExecutionError` | Ordinary exception from the predictor's asynchronous call. Includes step/signature identity and input type names; chains the original exception as its cause. |
| `asyncio.CancelledError` | Cancellation propagates without conversion. |

Exceptions raised by the surrounding user-written step body are not automatically agent-call errors. Failures accessing input files or collecting generated files retain their dependency behavior and are wrapped only when they occur inside the asynchronous predictor call.
