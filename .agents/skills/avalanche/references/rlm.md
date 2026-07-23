# Create a PredictRLM

> If the standalone RLM skill was installed through the Skills CLI, check it
> with `npx skills update rlm`. For this copy bundled inside Avalanche, run
> `npx skills update avalanche`. Replace `npx` with `pnpx`, `bunx`, or the
> equivalent package runner in use.

> Vendored from
> `https://github.com/Trampoline-AI/predict-rlm/blob/main/.agents/skills/rlm/SKILL.md`.
> Source SHA-256:
> `ccc1a74a252003c7d8d219666e715401014b0d1ced0e597031477eecf0d33618`.

## Upstream freshness

This is a bundled reference for the Avalanche skill, not a separately
discoverable skill. The Skills CLI update command above is the normal freshness
check. If the skill was not installed through that CLI, compare the latest
upstream `.agents/skills/rlm/SKILL.md` against the source checksum above and
synchronize this reference when it changed. If upstream is unreachable,
continue with this bundled copy; do not block the user's task.

Paths beginning with `docs/` below refer to files in the upstream PredictRLM
repository, not to files bundled with this Avalanche skill.

A PredictRLM is a DSPy module and runtime for a bounded workflow whose outcome
is known but whose execution path must adapt to the evidence it finds. It is
called like a typed function from application code; it is not an open-ended
chat agent or a fixed chain of prompts.

## Mental model

The caller supplies a DSPy signature. Its input and output fields are the
function contract, and its docstring is the operating procedure. On each call:

1. PredictRLM prepares the typed inputs and acquires a sandboxed execution
   session. The default backend is a stateful Python REPL running through
   Deno and Pyodide/WASM.
2. The **outer LM** reads the signature and strategy, then writes Python code
   to inspect inputs, preserve intermediate state, branch, retry, and verify.
   Python variables persist across iterations of that call.
3. Sandbox code can use `await predict(...)` for focused **sub-LM** perception
   or extraction. Each `predict()` call has its own context window and can use a
   typed DSPy signature, Pydantic schemas, and `dspy.Image`.
4. Sandbox code can also use packages and modules supplied by `Skill` objects,
   call host-side tools for capabilities outside WASM, and access explicitly
   allowed network domains.
5. When the work is complete, the outer LM calls `SUBMIT(...)`. PredictRLM
   validates the declared outputs, synchronizes file outputs to the host, and
   returns a `dspy.Prediction` with the output fields and a structured `trace`.

The separation is deliberate: the outer LM owns planning and code execution;
the sub-LM owns narrow understanding tasks; deterministic libraries and tools
do the work they are better suited for. Large context stays in files, sandbox
variables, and focused subcalls instead of accumulating in one prompt.

```text
application call
    -> DSPy signature: inputs + strategy + outputs
    -> outer LM <-> stateful sandbox REPL
                     |-> predict() -> sub-LM
                     |-> skill packages and modules
                     `-> host-side tools
    -> validated dspy.Prediction + trace
```

## Core API

A minimal typed RLM has a signature, a configured `PredictRLM`, and a normal
sync or async call:

```python
import dspy
from pydantic import BaseModel, Field

from predict_rlm import CtxStr, File, PredictRLM
from predict_rlm.skills import pdf


class Analysis(BaseModel):
    summary: str = Field(description="Grounded summary of the documents")
    risks: list[str] = Field(description="Material risks supported by the documents")


class AnalyzeDocuments(dspy.Signature):
    """Inspect the documents, apply the criteria, and return a grounded analysis.

    Survey the files first. Extract only relevant evidence with focused
    predict() calls, verify important claims, then submit the typed result.
    """

    documents: list[File] = dspy.InputField(desc="Documents to inspect")
    criteria: CtxStr = dspy.InputField(desc="Criteria the outer LM must see in full")
    analysis: Analysis = dspy.OutputField(desc="Evidence-grounded analysis")


rlm = PredictRLM(
    AnalyzeDocuments,
    lm="openai/gpt-5.4",
    sub_lm="openai/gpt-5.1",
    skills=[pdf],
    max_iterations=30,
)

result = await rlm.acall(
    documents=[File(path="report.pdf")],
    criteria="Cover obligations, deadlines, and material risks.",
)
print(result.analysis.summary)
```

Use `rlm(...)` for a synchronous call and `await rlm.acall(...)` for an
asynchronous call. Both return a `dspy.Prediction`; each declared output is
available as an attribute such as `result.analysis`.

The main constructor surface is:

- `signature` — a DSPy signature class or compact string signature;
- `lm` — the outer LM that writes code;
- `sub_lm` — the LM behind the built-in `predict()` tool;
- `skills` — reusable instructions, PyPI packages, sandbox modules, and tools;
- `tools` — sync or async host callables exposed inside the sandbox;
- `allowed_domains` — the sandbox network allowlist, empty by default;
- `max_iterations`, `max_llm_calls`, and `max_output_chars` — execution budgets;
- `output_dir` — host collection root for declared `File` outputs;
- `verbose` and `debug` — human-readable run output and lifecycle diagnostics.

Use `File` or `list[File]` for large file inputs and generated artifacts. Use
`CtxStr` for a direct string input, such as a rubric, whose full runtime value
must be added to the outer LM prompt as well as exposed as a Python variable.
Use a `Skill` when the sandbox needs reusable instructions, packages, modules,
or bundled tools. Use `tools=` directly for specific host-side actions such as
authenticated APIs, databases, native libraries, or host filesystem access.

## Runtime kernel and extension model

Most RLMs should use only signatures, `File`, `CtxStr`, `Workspace`, skills, and
host tools. Extend the kernel only when the workflow needs a new typed boundary,
resource lifecycle, execution substrate, or correctness-critical event stream.
Do not reach for a custom adapter merely because a signature uses a Pydantic
model.

At construction, PredictRLM resolves direct options and `RuntimeContribution`
module factories into one immutable `RuntimeSpec`: instructions, adapters,
tools, packages, exactly one execution backend, event sinks, validators, and
tool operations. Module factories run once at construction. Every invocation
then creates a fresh `RunContext` for mutable state, prepared inputs, bindings,
output reservations, cleanup callbacks, and evidence status.

### Kernel lifecycle and ownership

```text
construction
  direct options + RuntimeContribution modules
      -> immutable RuntimeSpec

invocation
  prepare typed inputs and output requirements
      -> compile path and artifact claims
      -> open adapter-owned resources
      -> validate requirements and acquire one ExecutionSession
      -> bind inputs and reserve outputs
      -> apply invocation-local prompt contributions
      -> run generated-code attempts
           -> after_execution durability hooks
      -> materialize submitted outputs while the session is active
      -> finalize input adapters in reverse order
      -> finalize and release the kernel-owned session
      -> run remaining LIFO RunContext cleanup
```

The ownership boundaries are strict:

- Adapters declare requirements and destinations before session acquisition.
- The `ExecutionBackend` creates the invocation-scoped `ExecutionSession`.
- Adapters may use supported backend or session capabilities, but never acquire,
  finalize, or release the session.
- Mutable provider clients, leases, baselines, and retry state belong in the
  invocation's `PreparedInput` or `RunContext`, never on a shared adapter.
- Sandbox destination overlaps fail before generated code runs.
- Adapter resources finalize on success, failure, setup failure, and
  cancellation. Framework-owned session finalization still runs after adapter
  errors.

### Choose the smallest extension point

Use the first boundary that can express the requirement:

1. **Signature docstring or `CtxStr`** — task strategy or invocation-specific
   criteria the outer LM must read.
2. **`Skill`** — reusable outer-LM instructions plus sandbox packages,
   importable Python modules, or bundled host tools.
3. **`tools=`** — a narrow sync or async host callable. Use this for native
   libraries, credentials, APIs, databases, and host filesystem access. Return
   plain JSON-like data or Pydantic values that can be normalized at the
   transport boundary.
4. **`File`, `Workspace`, or an existing adapter** — ordinary copied files,
   generated files, or a mutable directory. Prefer these built-ins over a new
   type.
5. **`InputAdapter` with `PreparedInput.path()`, `.paths()`, or `.glob()`** — a
   custom typed input can first be materialized as host paths. The kernel owns
   destination normalization, overlap checks, copying or explicit mounting,
   backend requirements, and the model-visible sandbox paths.
6. **Full `InputAdapter` lifecycle** — an external resource needs a provider
   lease, live handle, per-attempt synchronization, or a custom session
   capability that cannot be represented as host paths.
7. **`OutputAdapter`** — a custom output needs a reserved destination and
   provider-specific materialization while the session is still active.
8. **`RuntimeContribution` through `modules=`** — several host-side extensions
   belong together and should compose as one reusable construction-time unit.
9. **`EventSink` through `events=`** — a consumer needs ordered lifecycle events
   during the run. Use `prediction.trace` when only the completed trace is
   needed.
10. **`ExecutionBackend` through `execution=`** — only for a genuinely new
    sandbox or execution substrate. Use `sandbox_backend="jspi"` or `"sbx"` for
    maintained backends.

`interpreter=` is a compatibility bridge for older CodeInterpreter-shaped
integrations, not the root extension contract. New session-native integrations
must implement `ExecutionBackend` and use `execution=`.

### Input adapters

An `InputAdapter[T]` selects signature fields through `value_type` and turns the
caller's value into model-visible data and runtime requirements. Adapter
instances are construction-time objects and may serve concurrent calls, so
keep them immutable or concurrency-safe.

Use the current lifecycle names and responsibilities:

- `prepare(field, value, ctx) -> PreparedInput` runs before backend acquisition.
  It receives no backend or session. Return declarative paths, artifacts,
  sandbox-root reservations, metadata, requirements, and input instructions.
- `open(field, prepared, ctx, backend)` may acquire an adapter-owned client or
  lease after all inputs are prepared but before a session exists. Store the
  handle in `ctx.state` under an adapter-owned key.
- `bind(field, prepared, ctx, session) -> BoundInput` runs after acquisition.
  Use it only when the default artifact mounting is insufficient; return the
  final value and bindings visible to generated code.
- `append_prompt(prompt, field, prepared, ctx) -> str` may add invocation-local
  context to the outer action and forced-extraction prompts after binding. It
  must not mutate the canonical `PredictRLM.signature`.
- `after_execution(field, prepared, ctx, session, result, error)` runs after
  each completed generated-code attempt. Use it for durability or sync work,
  including changes made before that attempt raised an error.
- `finalize(field, prepared, ctx, session, error)` performs the final save and
  releases adapter-owned resources. Opened adapters finalize once in reverse
  order; `session` is `None` if acquisition failed.

For the common path case, implement only `prepare()`:

```python
class S3FileAdapter(InputAdapter[S3File]):
    name = "s3-file"
    value_type = S3File

    async def prepare(self, field, value, ctx) -> PreparedInput:
        local_path = await materialize_s3(value.uri, ctx=ctx)
        return PreparedInput.path(
            local_path,
            instructions=(f"{field.name} is available at its sandbox path.",),
        )
```

Register one-off adapters directly with
`PredictRLM(MySignature, adapters=[S3FileAdapter()])`, or contribute them from a
runtime module when they belong with tools, packages, events, or an execution
backend.

Copy is the portable default. Set `mode="mount"` only for a required live
directory view; unsupported backends fail rather than silently copying.
`.paths()` exposes an explicit list, and `.glob()` performs deterministic,
host-side selection with traversal and symlink-escape checks.

For a resource described completely by an ID, URI, or host path, return an
`Artifact` in `PreparedInput.artifacts` and let the default `bind()` call
`session.mount(artifact)`. For a provider-managed live handle, acquire it in
`open()`, keep it in `ctx.state`, and use a capability protocol implemented by
the selected session from `bind()`.

Do not use removed lifecycle names: input `prepare_session()` became `open()`,
input `mount()` became `bind()`, and `MountedInput` became `BoundInput`.
`prepare_session()` remains an output-adapter method. The reserved `ctx_str`
adapter name may only be replaced by a `CtxStrInputAdapter` subclass.

### Output adapters

An `OutputAdapter[T]` owns a custom typed output boundary:

- `prepare_session()` contributes pre-acquisition policy and requirements;
- `reserve()` claims a destination after session acquisition but before code;
- `materialize()` turns the submitted value into the caller-facing value while
  the session is active.

Output adapters have no input-style `finalize()` hook. Clean partial failures
locally, and register provider cleanup that can outlive the session with
`ctx.add_cleanup()`. If an output owns a sandbox path, place it in the
reservation artifact's `metadata["sandbox_path"]`; the kernel rejects overlaps
with inputs and other outputs before execution.

### Runtime modules

Use a zero-argument module factory to package related host-side contributions:

```python
from predict_rlm import CallableTool, RuntimeContribution


def document_runtime() -> RuntimeContribution:
    return RuntimeContribution(
        instructions=("Treat document IDs as opaque provider references.",),
        adapters=(DocumentReferenceAdapter(),),
        tools=(
            CallableTool(
                name="fetch_metadata",
                function=fetch_metadata,
                description="Fetch provider metadata for one document ID.",
                schema={
                    "type": "object",
                    "properties": {"document_id": {"type": "string"}},
                    "required": ["document_id"],
                },
            ),
        ),
        packages=("pure-python-package",),
        events=(DocumentAuditSink(),),
    )


rlm = PredictRLM(MySignature, modules=[document_runtime])
```

`PredictRLM(modules=...)` composes host-side runtime behavior.
`Skill.modules` is different: it copies Python files into the sandbox so
generated code can import them. Contribution packages are deduplicated;
duplicate adapter, tool, or tool-operation names are errors; and the resolved
configuration must select exactly one execution backend.

### Execution, events, hooks, and gates

- An `ExecutionBackend.start(spec, ctx)` returns an async context manager that
  yields one invocation-scoped `ExecutionSession`. The session implements code
  execution, package installation, artifact mounting and collection,
  cancellation, and finalization. Optional capability protocols add operations
  such as host-directory mounts or mutable-directory collection.
- `EventSink` implements async `emit`, `flush`, and `close` plus a `strict`
  flag. Use `strict=False` for monitoring whose failure must not fail the run.
  Use `strict=True` only when durable, complete evidence is part of correctness;
  a strict sink failure can prevent successful publication.
- DSPy callbacks report high-level RLM iteration progress. `EventSink` reports
  ordered kernel lifecycle evidence. They solve different problems.
- `RuntimeHook(target=..., phases={"before", "after", "error"})` instruments a
  dotted function inside the sandbox and emits sanitized `RuntimeHookEvent`
  values to `on_runtime_hook_event`. Runtime hooks require the SBX backend in
  PredictRLM v1; do not design a portable extension around them.
- `submit_confirmation` is a gate after a valid `SUBMIT`: return feedback to
  continue the RLM loop, or `None`/`""` to accept the submission.

Before implementing a kernel extension, read `docs/custom-path-inputs.md` for
path-backed inputs, `docs/custom-adapters.md` for lifecycle patterns,
`docs/api.md` for exact method signatures, and `docs/observability.md` for event
sink guarantees. Import public contracts from `predict_rlm`; do not use removed
shim modules such as `predict_rlm.adapters`, `predict_rlm.artifacts`,
`predict_rlm.events`, `predict_rlm.execution`, or `predict_rlm.kernel`.

Test extensions across success, generated-code failure, setup failure,
cancellation, and concurrent calls. If they own mutable external state, verify
LIFO cleanup and that no state leaks between invocations.

## What this skill builds

This skill creates or extends the reusable package around PredictRLM: schemas,
the DSPy signature and strategy, capability definitions, a service wrapper, and
smoke tests. Do not add RLM-GEPA optimization wiring here; use the separate
`rlm-gepa` skill once the base RLM works and an optimization objective exists.

Work in two phases:

1. **Plan** — define the RLM with the user, research feasibility, and produce a
   concrete implementation plan.
2. **Build** — implement the approved plan, then smoke-test the generated
   package.

# Phase 1: Plan

Keep the following sequence. It prevents a plausible-looking RLM from having
an unusable boundary, missing runtime capability, or an unfalsifiable output.

## Step 1: Goal definition

Understand what the user wants to build.

Ask:

- What is the desired outcome and what does success look like?
- What is the input material: documents, code, data, APIs, or stateful systems?
- What should the output be: structured data, modified files, a spreadsheet, or
  another artifact?

Then validate RLM fit. An RLM is a good fit when it needs one or more of:

- selective exploration of large inputs such as documents, datasets, or codebases;
- multi-step work with tools, for example extract → transform → validate;
- actions that modify a file or another controlled state boundary;
- parallel sub-LM calls across many items;
- file-to-file transformation such as PDFs to spreadsheets or documents to reports.

If a single LM call or a deterministic script is a better fit, say so and
propose it instead.

## Step 2: Input design

Define every input before defining implementation files.

For each input, decide its name, type, source, description, and whether the
outer LM needs the complete value immediately.

- Use `File` or `list[File]` for large files. Inputs are copied into the sandbox
  under `/sandbox/input/<field>/`; the RLM accesses them on demand.
- Use ordinary `str`, primitives, or lean Pydantic models for metadata and
  configuration.
- Use `CtxStr` only for a small-to-moderate string that the outer LM must see in
  full, such as a rubric or task instruction. `CtxStr` is input-only and only
  valid as a direct field annotation in a class-based DSPy signature.
- Do not put raw document contents or large tables directly in a prompt when a
  `File` reference and focused extraction can keep the context small.

Confirm the input boundary before moving on.

## Step 3: Output design

Define the structured output before selecting packages or writing prompts.

For each output field, decide its name, type, description, and whether it is a
primitive, a Pydantic model, `File`, or `list[File]`.

Push for specific, observable fields. Use Pydantic `Field(description=...)` for
non-obvious model fields. Model only data the caller needs; do not expose
internal IDs or intermediate reasoning.

Ask what users check first, which computed values matter, and whether they need
output files. Confirm the schema before moving on.

## Step 4: Research feasibility

Research autonomously, then report a clear feasibility assessment.

1. Find domain libraries and existing project patterns.
2. Check sandbox compatibility. The default execution environment is Pyodide in
   WASM: pure-Python wheels and Pyodide-built packages work; native binaries and
   ordinary C extensions do not.
3. Identify network needs and list exact domains for `allowed_domains`.
4. Identify host-side needs. Put native libraries, authenticated APIs, database
   access, heavy host filesystem work, and unsupported packages behind typed
   host-side tools.
5. Check whether `pdf`, `spreadsheet`, or `docx` built-in skills already cover
   the task.
6. Check whether the boundary already fits `File`, `Workspace`, `CtxStr`, an
   existing adapter, or a `PreparedInput` path declaration before designing a
   full runtime extension.

Treat a package that cannot run in Pyodide as a design decision, not a surprise
at implementation time: use a host-side tool or change the approach.

## Step 5: Capability design

Choose the smallest capability surface that makes the workflow work.

### Built-in skills

State which built-in skills are needed and why:

- `pdf` for PDF rendering, text extraction, modification, and redaction;
- `spreadsheet` for Excel workbooks, formulas, and formatting;
- `docx` for reading and writing Word documents.

### Custom skills

Create a `Skill` only for reusable sandbox instructions, packages, modules, or
tools. For each skill, specify:

- `name` — concise identifier;
- `instructions` — concrete operating guidance for the RLM;
- `packages` — Pyodide-compatible sandbox packages only;
- `modules` — host paths mapped to importable sandbox module names;
- `tools` — host callables that belong with that reusable capability.

### Host-side tools

For each tool, define the typed signature, a useful docstring, return shape,
and why it must run on the host. Return plain JSON-like data or Pydantic models;
PredictRLM normalizes Pydantic values, including nested values, to mappings and
lists before they cross the sandbox boundary.

Prefer narrow, typed tools. A tool is a system boundary, not an escape hatch for
arbitrary host access.

### Kernel extensions

If the workflow needs a custom runtime boundary, specify it in the plan rather
than discovering it during implementation:

- the caller-facing signature type and model-visible value;
- why built-in files, workspaces, skills, or host tools are insufficient;
- the smallest extension point from the kernel decision order above;
- all sandbox paths, copy versus mount behavior, and backend requirements;
- adapter-owned resources, invocation-local state, persistence points, and
  cleanup on success, failure, cancellation, and partial setup;
- whether the extension is portable across JSPI and SBX;
- the `RuntimeContribution` composition boundary when several pieces belong
  together; and
- focused lifecycle and concurrency tests.

## Step 6: Strategy and architecture

Write the strategy that belongs in the DSPy signature docstring. It is the
RLM's playbook:

1. survey and understand the available inputs;
2. gather evidence with file skills, `predict()`, and host tools;
3. process, cross-check, and synthesize the evidence;
4. emit the declared data and write any output files in their output locations.

Choose one RLM when the work is a coherent workflow with shared state and a
reasonable iteration budget. Chain RLMs only when stages have genuinely
different capabilities, output artifacts consumed by later stages, or separate
model/iteration requirements. For every chain, define typed stage boundaries
and a DAG before implementation.

Choose initial `max_iterations`, `max_llm_calls`, and `max_output_chars` from
the workload. Record required `allowed_domains`, whether a capable `sub_lm` is
needed for extraction, and whether the caller needs `output_dir` for generated
files.

## Feasibility checklist

Before finalizing the plan, verify:

- [ ] Every sandbox package is Pyodide-compatible or replaced by a host tool.
- [ ] Network domains are explicit and minimal.
- [ ] Host-side tools cover every unsupported host capability.
- [ ] The iteration and LM-call budgets are plausible for the workflow.
- [ ] Large inputs stay as `File` references or on-demand metadata.
- [ ] Output schemas are specific enough to validate.
- [ ] Any chained stages have typed, necessary boundaries.

## Plan output

Deliver a plan containing:

1. Overview and RLM-fit decision.
2. Delivery scope and file manifest.
3. Complete input and output schemas.
4. Complete signature strategy.
5. Skill and host-tool contracts.
6. Service architecture, including a typed DAG when chained.
7. Feasibility constraints, alternatives, and initial budgets.
8. Smoke-test cases and exact verification commands.

# Phase 2: Build

Implement the approved design with the smallest maintainable package layout.
Do not scaffold benchmarks or RLM-GEPA optimization speculatively. Add an eval
layer only when the user has examples and a scoring need; use `rlm-gepa` for
optimization wiring.

## Default package layout

```text
my_rlm/
├── pyproject.toml
├── src/
│   └── my_rlm/
│       ├── __init__.py
│       ├── schema.py       # Pydantic inputs and outputs
│       ├── signature.py    # DSPy signature and strategy
│       ├── service.py      # PredictRLM wiring
│       ├── skills.py       # Only when custom skills are needed
│       └── tools.py        # Only when host-side tools are needed
└── tests/
    └── test_smoke.py
```

Create `schema.py`, `signature.py`, `service.py`, package exports, and a smoke
test. Add files only when the chosen boundary needs them.

## Dependencies

Install predict-rlm in the target repository without hardcoding a version:

```bash
uv add predict-rlm
```

Add the `examples` extra only when using built-in document or spreadsheet
skills, and add any domain dependency only where it runs. Do not include GEPA
or visualization extras in an agent-only project.

## Schema

```python
from pydantic import BaseModel, Field


class KeyDate(BaseModel):
    name: str = Field(description="What deadline or effective date this is")
    date: str = Field(description="ISO-8601 calendar date")


class DocumentAnalysis(BaseModel):
    summary: str = Field(description="Grounded Markdown summary")
    key_dates: list[KeyDate] = Field(
        default_factory=list,
        description="Dates supported by the input documents",
    )
```

## Signature and strategy

```python
import dspy

from predict_rlm import CtxStr, File

from .schema import DocumentAnalysis


class AnalyzeDocuments(dspy.Signature):
    """Produce a grounded analysis of the supplied documents.

    1. Inspect the document inventory and task criteria.
    2. Read or render only the relevant files and pages.
    3. Use focused `predict()` calls for extraction when useful.
    4. Cross-check findings, then return the declared analysis.
    """

    documents: list[File] = dspy.InputField(desc="Documents to inspect")
    criteria: CtxStr = dspy.InputField(desc="Rubric to apply in full")
    analysis: DocumentAnalysis = dspy.OutputField(desc="Grounded result")
```

Callers pass a plain string for `criteria`; `CtxStr` makes it both a sandbox
variable and a full prompt appendix. Use an ordinary `str` when the RLM can
inspect the value from the sandbox instead.

## Service

```python
import dspy

from predict_rlm import File, PredictRLM
from predict_rlm.skills import pdf

from .signature import AnalyzeDocuments


class DocumentAnalyzer(dspy.Module):
    def __init__(
        self,
        *,
        lm: dspy.LM | str | None = None,
        sub_lm: dspy.LM | str | None = None,
        max_iterations: int = 30,
        verbose: bool = False,
        debug: bool = False,
    ) -> None:
        self.predictor = PredictRLM(
            AnalyzeDocuments,
            lm=lm,
            sub_lm=sub_lm,
            max_iterations=max_iterations,
            verbose=verbose,
            debug=debug,
            skills=[pdf],
        )

    async def aforward(self, documents: list[File], criteria: str):
        return await self.predictor.acall(documents=documents, criteria=criteria)
```

Use `tools=[callable]` when the callable name is suitable, or
`tools={"name": callable}` to expose a deliberate tool name. Use `skills=[...]`
for reusable sandbox capabilities. Pass `output_dir` to `PredictRLM` when
`File` or `list[File]` outputs must be collected below a caller-selected root.

# Runtime reference

## Core constructor

```python
PredictRLM(
    signature,
    lm=None,
    sub_lm=None,
    max_iterations=30,
    max_llm_calls=50,
    max_output_chars=50_000,
    verbose=True,
    tools=None,
    skills=None,
    adapters=(),
    execution=None,
    modules=(),
    events=(),
    allowed_domains=None,
    output_dir=None,
    interpreter=None,
    runtime_hooks=None,
    submit_confirmation=None,
    debug=False,
)
```

`lm` drives outer code generation; `sub_lm` serves sandbox `predict()` calls.
Both accept a DSPy LM or model string. `predict()` is always available.

The kernel extension contracts are described above. Use `execution=` for a new
session-native backend and reserve `interpreter=` for CodeInterpreter-shaped
compatibility integrations. Consult `docs/api.md` for exact signatures rather
than guessing lifecycle parameters.

## Files, skills, and tools

- `File(path="report.pdf")` represents an input file; `File.from_dir(path)`
  creates a sorted recursive list of file references.
- Input files are copied to `/sandbox/input/<field>/`. File outputs are written
  under `/sandbox/output/<field>/` and synchronized back after execution.
- `Skill(name, instructions, packages, modules, tools)` bundles reusable
  capability. Skill tool-name collisions are errors.
- `pdf`, `spreadsheet`, and `docx` are the built-in skills:

  ```python
  from predict_rlm.skills import docx, pdf, spreadsheet
  ```

- Host tools run outside WASM. Make their docstrings, arguments, and return
  data clear enough for generated code to call safely.

## Sandbox constraints

- Use pure-Python or Pyodide-compatible packages in sandbox skills.
- Do not expect subprocesses, native binaries, or ordinary C extensions.
- Default network access is denied; set a minimal `allowed_domains` list when
  the sandbox must reach a network service.
- Use host-side tools for native code, credentials, databases, and host files.
