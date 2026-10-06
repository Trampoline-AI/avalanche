# Agents

Avalanche 0.7.0's workflow-author agent API. Dependency behavior below describes the locked PredictRLM 0.8.0 and DSPy 3.2.1 versions. See [workflows](workflows.md) for node calls, composition, and workflow default declarations; [inputs and context](inputs-context.md) for ordinary step injection.

## Agent step

```python
import avalanche as ava

# Author-facing call form; omitted runtime options inherit configuration.
@ava.agent_step(
    signature,
    lm=...,
    sub_lm=...,
    max_iterations=...,
    skills=...,
    tools=...,
    output_dir=...,
    **predictor_kwargs,
)
async def analyze(document: str, *, agent: ava.Agent) -> str:
    prediction = await agent(document=document)
    return prediction.summary
```

The example assumes a signature with input `document` and output `summary`. Ellipses denote optional arguments to omit or replace, not values to pass. `output_dir` is an upstream option passed through `**predictor_kwargs`, not a separately declared decorator parameter.

| Parameter | Accepted value and default | Meaning |
| --- | --- | --- |
| `signature` | Required DSPy signature class | An Avalanche `Signature` subclass, the class returned by `Signature(...)`, or a native DSPy signature class. Must declare at least one output. Bare strings are not accepted. |
| `lm` | `dspy.LM \| str \| None`; omitted inherits | Main code-writing model. Dependency default `None` uses DSPy context configuration. |
| `sub_lm` | `dspy.LM \| str \| None`; omitted inherits | Model for the agent's structured prediction tool. Dependency default `None` uses DSPy context configuration. |
| `max_iterations` | `int`; omitted inherits, dependency default `30` | Maximum agent REPL iterations. |
| `skills` | Sequence of `ava.agent.Skill`; omitted gives none | Capability bundles, configured on this step only. Avalanche checks that the value is a sequence, not each member's type. |
| `tools` | Sequence of callables; omitted gives none | Additional tools; every member must be callable. Neither `tools=None` nor `skills=None` means an empty sequence. |
| `output_dir` | `str \| pathlib.Path \| None`; omitted inherits, dependency default `None` | Collect generated agent `File` outputs under `<output_dir>/<field>/`, otherwise in a temporary directory. Does not redirect scalar outputs or logs. |
| `**predictor_kwargs` | Additional PredictRLM constructor keyword arguments | Forwarded to the installed dependency; see its constructor documentation for options and compatibility constraints. Avalanche does not validate this entire upstream surface. |

The decorator returns a single-output workflow step. The body must declare exactly `*, agent: ava.Agent`, with no default; Avalanche injects it and removes it from the step's public call signature. The remaining Python parameters and return annotation describe the workflow step, independently of the model signature. Synchronous and asynchronous bodies are accepted; an awaitable body result is awaited. Use an asynchronous body to await model calls.

There are no ordinary node-decorator options here: for example, `slug=` would be forwarded to PredictRLM, not interpreted as a node identifier. The body chooses which prediction fields to return and whether to write files or tables; the decorator does not persist the body result to an application table.

### Workflow defaults

Declare `agent_defaults={...}` on [the workflow decorator](workflows.md). Resolution is per runtime key:

1. Explicit agent-step value, including explicit `None`.
2. Workflow `agent_defaults`.
3. Avalanche's `verbose=False` default.
4. PredictRLM's constructor default.

`agent_defaults` must be a mapping. `signature`, `skills`, and `tools` are forbidden there; declare them on individual agent steps. Other keys are forwarded to PredictRLM. Model credentials and accepted model configurations follow the installed dependency, not Avalanche's classifier configuration.

## Signature declarations

```python
class Summarize(ava.Signature):
    """Summarize the supplied document."""

    document: str = ava.InputField(desc="Complete document text.")
    summary: str = ava.OutputField(desc="Concise factual summary.")
```

The class docstring supplies instructions, type annotations describe model inputs and outputs, and the field helpers mark their direction. Authors choose the field names and types, including structured Pydantic output models and [agent files](#agent-files). Skills and tools are decorator configuration, not signature fields.

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

The field helpers accept keyword arguments only. They do not add an Avalanche value-validation layer. Malformed declarations or incompatible field options raise DSPy/Pydantic errors, including `ValueError` and `TypeError`. Signature classes expose `instructions`, `input_fields`, and `output_fields` for the declared contract; inherited dependency editing methods are outside this reference.

## Injected Agent call

```python
prediction = await agent(**inputs)
summary = prediction.summary  # the output name declared by the signature
```

`inputs` must contain **exactly** the signature's input names, even for fields with descriptor defaults. Positional input arguments, extra names, and missing names are not supported. Runtime configuration cannot be overridden through this call. Avalanche checks input names, not their value types; PredictRLM/DSPy processes the values.

The return is the dependency's raw DSPy prediction, with outputs accessible by their declared field names. There is no fixed Avalanche result-field schema. For a structured output, callers can explicitly validate the field with their application model, for example `Report.model_validate(prediction.report)`.

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

Built-in skill objects are `ava.agent.skills.pdf`, `ava.agent.skills.docx`, and `ava.agent.skills.spreadsheet`. Pass the objects in the decorator's `skills=[...]`; their instructions and package requirements belong to PredictRLM.

## Agent files

```python
ava.agent.File(*, path: str | None = None)
ava.agent.File.from_dir(path: str) -> list[ava.agent.File]
```

This is PredictRLM's file-reference model, **not** the workflow runtime [`ava.File`](files-workspaces.md). It has one field, `path`: an optional host-path string, default `None`. Construction returns a reference; it does not read or verify the file. Invalid field types raise dependency Pydantic validation errors.

As a signature input, `File` names a host file to mount in the agent sandbox. As a signature output, PredictRLM fills `path` with the generated host-file path. Both `File` and `list[File]` are supported signature shapes. Read generated paths through `prediction.<field>.path`, or validate a mapping-shaped output with `ava.agent.File.model_validate(prediction.<field>)` before reading `path`.

`from_dir` recursively collects file references without reading their content. It sorts filenames within each visited directory, not the entire traversal. A nonexistent directory produces an empty list. Agent `File` does not expose workflow file read/write or publication methods; use [files and workspaces](files-workspaces.md) for those APIs.

## Errors

Import agent errors from `avalanche.agent`. `AgentStepError` and `AgentStepExecutionError` are separate direct `RuntimeError` subclasses.

| Error | Caller-visible condition |
| --- | --- |
| `AgentStepError` | Invalid injected `agent` parameter, invalid resolved input-field mapping, or missing/extra model-call input names. |
| `TypeError` | Missing/invalid signature, outputless signature, invalid skills/tools declaration, or forbidden workflow-default keys. Signature validation may occur during workflow metadata resolution or the first agent call rather than decoration. |
| Dependency construction errors | Invalid upstream options or incompatible skill/tool configuration. Predictor construction occurs before the call's execution-error wrapper, so these propagate directly. |
| `AgentStepExecutionError` | Ordinary exception from the predictor's asynchronous call. Includes step/signature identity and input type names; chains the original exception as its cause. |
| `asyncio.CancelledError` | Cancellation propagates without conversion. |

Exceptions raised by the surrounding user-written step body are not automatically agent-call errors. Failures accessing input files or collecting generated files retain their dependency behavior and are wrapped only when they occur inside the asynchronous predictor call.
