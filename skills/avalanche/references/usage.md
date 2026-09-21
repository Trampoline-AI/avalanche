# Avalanche usage

This reference mirrors the README's Usage section and adds the exact local CLI
interfaces an agent may use. Run commands with `uv run ava ...` from a UV
project. Do not start long-lived services unless the user asks to run, inspect,
or verify a workflow through the operator.

## Choose an execution surface

- **Operator CLI:** start discovery with `uv run ava operator path/to/flow.py`,
  then submit runs with `uv run ava run`. All workflow execution goes through
  the operator; never add standalone scripts, `main()`/`__main__` blocks, or
  direct `Workflow.run()` entry points.
- **Browser UI:** the normal interactive local surface. Use it with the local
  operator when the user asks for an interactive run, inspection, or browser
  verification.
- **TUI:** an optional terminal surface. Do not run it from the agent; give the
  user the command to run in a terminal they control.

For the browser UI, prefer `uv run ava dev` unless the user needs separate
process lifecycles or custom browser-listener settings. Do not automatically
launch an operator or UI merely because a workflow was implemented.

## Define a workflow

Use deterministic `@ava.step` nodes for ordinary Python work,
`@ava.classifier_step` for typed classification questions, and `@ava.agent_step`
for adaptive model-backed work:

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

Annotate each node's Python inputs and return value. The browser's **Step
interface** panel uses these annotations for source, ordinary, destination,
agent, and classifier steps, including nested Pydantic schemas. These are the
outer workflow values, not an agent signature or classifier call's state.
Injected runtime parameters are excluded; historical runs retain their own
interface rather than showing the latest source definition.

`@ava.workflow` declares the builder that the operator discovers. Save this as
`flow.py` and start it through the operator and browser:

```bash
uv run ava dev path/to/flow.py
```

The declared `ava.Signature` output names are the prediction attribute names:
read `.completion` for the signature above, not `.summary`. A signature that is
not constructed inline inside the step decorator belongs in `signature.py`.

## Native classifier steps

Prefer `@ava.classifier_step` for Choice, Noul, or Score questions rather
than wrapping the TypeSafe client yourself or using an adaptive agent solely
for classification. The SDK is included in the base Avalanche package.

```python
import avalanche as ava


@ava.source
def load_ticket() -> str:
    return "Please reverse the duplicate charge today."


@ava.classifier_step(
    questions={
        "urgent": {
            "type": "noul",
            "instructions": "Does the ticket require action today?",
        },
    },
)
async def classify_ticket(
    ticket: str, *, classifier: ava.Classifier
) -> ava.ClassificationResult:
    return await classifier(state=ticket)


@ava.workflow(classifier_defaults={"model": "jev-latest", "timeout": 10.0})
def ticket_workflow():
    return load_ticket() >> classify_ticket()
```

The keyword-only `classifier` parameter is injected; never supply it in the
DAG. `state` accepts text, a JSON object, or a JSON array. Decorator `questions=`
declares optional snapshotted defaults; per-call `questions=` replaces the entire
mapping for that invocation. See [runtime questions](#runtime-questions).
Question `criteria` holds named options for `choice`, optional `"true"`/`"false"`
criteria for `noul`, or an ordered list of at least two levels for `score`.
Instructions and criteria entries can contain structured JSON as well as text.

Use `input_model=YourPydanticModel` when the classifier state has a declared shape.
Keep the model in `schema.py` and pass JSON-compatible state matching it; Avalanche
validates the state before the request. The classifier Definition shows this
per-call input separately from the step function's annotated parameters and return
type, which may describe an entire batch or transformed results.

`ava.ClassificationResult` retains model, usage, and typed answers:
`result.choices["department"]` exposes choice, all option probabilities, and
confidence; `result.nouls["urgent"].noul` is P(yes), not a Boolean or a confidence
score; `result.scores["severity"]` retains the fractional probability-weighted
score, legend, probabilities, and confidence. A step may return this result
directly or transform it into a domain result. Operator invocation records remain
separate from the step return, including when later postprocessing fails.

Set `TYPESAFE_API_KEY` in the executing process environment. Discovery needs
neither credentials nor a client; execution without a key fails instead of
substituting answers. Do not put keys in workflow definitions or metadata.
Workflow `classifier_defaults` accepts `model` and `timeout` (seconds);
step-level `model=` and `timeout=` override those defaults. Without either,
the defaults are `jev-latest` and 10 seconds. Optional `slug=` names the node.

The repository's `examples/classifier_workflow.py` demonstrates all three answer
types and returns the full typed result from `classifier_workflow()`:

```bash
uv run ava dev examples/classifier_workflow.py
```

The browser shows default questions before a run, or indicates that questions
are supplied at runtime. Call details retain actual resolved questions and answers,
including overrides. Evidence uses existing local operator retention limits and
lifetime; it is not durable recovery storage.

### Runtime questions

When candidates, question IDs, or rubrics depend on runtime data, build questions
inside the step (or receive them from an upstream node), not in the workflow body.
For example, select from a variable list of passages:

```python
@ava.classifier_step()
async def select_passage(
    query: str, passages: list[str], *, classifier: ava.Classifier
) -> ava.ClassificationResult:
    return await classifier(
        state={"query": query, "passages": passages},
        questions={
            "best_match": {
                "type": "choice",
                "instructions": (
                    "Which passage best answers `query`? "
                    "Select none if no passage answers it."
                ),
                "criteria": {
                    **{
                        f"passage_{i}": f"The passage at `passages[{i}]`."
                        for i in range(len(passages))
                    },
                    "none": "None of the supplied passages answers the query.",
                },
            },
        },
    )
```

Omitted or `None` call questions use decorator defaults. Without either source,
the call fails before client creation. Explicit `{}` or malformed questions
fail rather than falling back. Supplied questions replace the whole mapping;
combine mappings explicitly in Python if needed. Each call validates and owns
its resolved questions before sending, so later mutation or concurrent calls
cannot change its rubric. Results are checked against those exact questions.
Invalid/missing questions produce failed-call evidence with unresolved questions,
not fabricated defaults. Model, timeout, and input-model configuration do not
change per call.

### Structure the `questions` object

Start from what downstream code must decide, then choose the smallest useful
judgments. `questions` maps stable question IDs to objects with `type`,
`instructions`, and type-specific `criteria`. IDs identify answers in code;
**the model does not see question IDs**, so instructions must state the complete
judgment. Keep source content and current facts in `state`; construct runtime
questions when the judgments or candidate options themselves must change.
Use named state fields when context has several parts, and refer to them with
backticked paths such as `ticket.messages[0].text`.

| Need | Type | `criteria` shape |
| --- | --- | --- |
| One route, category, handler, or known argument | `"choice"` | Map of option names to descriptions. Distinguish competing options and include a no-match option when none may fit. |
| A condition, label, or evidence check | `"noul"` | Optional map with string keys `"true"` and `"false"` describing yes and no. Use separate questions when several labels can apply. |
| Degree, quality, relevance, or ranking signal | `"score"` | Ordered list of concrete level descriptions, low to high; at least two levels. Use the same rubric across items being ranked. |

Ask one coherent judgment per question. Split independently useful dimensions,
not the related facts needed to judge one relationship. Keep exact lookups,
calculations, policy enforcement, and external actions in ordinary Python.

### Routing, multiple labels, and graded severity

This declaration expects state shaped like
`{"ticket": {"text": "Export crashes; please refund my subscription."}}`.
The Choice selects one primary route, the Nouls allow overlapping labels, and
the Score measures one dimension under an explicit speculative premise:

```python
questions = {
    "route": {
        "type": "choice",
        "instructions": "Which team should own the primary request in `ticket.text`?",
        "criteria": {
            "engineering": "Fix broken product behavior.",
            "billing": "Resolve charges, invoices, or refunds.",
            "other": "The primary request fits neither engineering nor billing.",
        },
    },
    "refund_requested": {
        "type": "noul",
        "instructions": "Does the customer ask for money back in `ticket.text`?",
        "criteria": {
            "true": "Requests a refund, reversal, or return of a payment.",
            "false": "Does not request money back; a payment mention alone is not enough.",
        },
    },
    "human_requested": {
        "type": "noul",
        "instructions": "Does the customer ask to speak to a person in `ticket.text`?",
    },
    "bug_severity": {
        "type": "score",
        "instructions": (
            "Assuming `ticket.text` reports a product bug, how much does that bug "
            "disrupt the customer's use of the product?"
        ),
        "criteria": [
            "Cosmetic defect; product functions remain usable.",
            "A function is broken or degraded, but a workaround exists.",
            "An essential task is blocked with no workaround.",
        ],
    },
}
```

Pass the mapping as `@ava.classifier_step(questions=questions)` and call
`await classifier(state=...)` inside its async body. Read the results through
`result.choices["route"]`, `result.nouls["refund_requested"]`, and
`result.scores["bug_severity"]`. Code consumes severity only for the applicable
bug-handling branch; an unused speculative answer must not trigger escalation.
The two Noul answers can both be yes regardless of the primary route.

Score levels must stand on their own: describe situations, not bare numbers,
vague labels such as "medium", or comparisons such as "worse than above".
With three levels the score ranges from 0 to 2 and may be fractional; it is not
automatically a 0–1 value. For multiple ranking dimensions, ask one Score per
dimension and combine their values in code. Weights can change without rerunning
inference when evidence and question meanings are unchanged. Do not average away
a serious violation: use separate conditions for hard policy gates.

### Structured instructions and evidence verification

Start with strings. Use objects or arrays inside `instructions` or criterion
descriptions when definitions, exclusions, contrasts, or examples clarify the
judgment. These inner keys are your rubric, not additional API fields. Keep the
outer shape unchanged: Choice criteria remain a map, Score criteria a list,
and Noul criteria a `"true"`/`"false"` map.

For example, pass a proposed claim and its source excerpt as
`{"claim": "...", "source_text": "..."}` and declare:

```python
questions = {
    "claim_supported": {
        "type": "noul",
        "instructions": {
            "question": "Does `source_text` support the entire factual `claim`?",
            "scope": "Judge only against the supplied source, not outside knowledge.",
            "checks": [
                "Match the entities and relationships.",
                "Preserve qualifications, dates, and quantities.",
            ],
        },
        "criteria": {
            "true": {
                "definition": "The source supports every factual part of the claim.",
                "includes": ["Faithful paraphrases that preserve the original meaning."],
            },
            "false": {
                "definition": "The source contradicts or does not establish the full claim.",
                "includes": ["A matching topic without evidence for the claimed fact."],
            },
        },
    },
}
```

This checks source support, not whether the claim is true in the world. If code
must distinguish contradiction from missing evidence, use a Choice with
`supported`, `contradicted`, and `not_established` options instead.

The same structure works for other use cases:

- **Handler arguments:** use one Choice for the handler and separate Choices
  for closed-set arguments. State each branch's premise in its instructions;
  consume only the arguments for the selected handler.
- **Value extraction:** find candidates in code, select with a Choice, then copy
  or normalize the chosen source value in code. Check candidate coverage and
  include a no-match option; the model cannot select an omitted value.
- **Retrieval and ranking:** put a query and candidate passage in state; apply
  the same relevance Score to each pair, then sort in code. A Choice distribution
  compares competing options; it is not an independent relevance score per item.
- **Changing candidates:** construct a Choice's criteria from the actual candidates
  and pass the mapping as runtime `questions=`. Include a no-match option. For
  independent relevance scores, reuse a fixed Score over each candidate instead.
  An earlier result can determine a later call's taxonomy options; do not mutate
  the decorator defaults or expect questions in one call to see each other's answers.

### Batch independent judgments and handle uncertainty

Put independent questions over the same state in one mapping and one call.
They run in parallel and **cannot see each other's answers**. Do not write
"use the route answer" in another question's instructions. Ask useful branch
questions speculatively, as above; make a later call when an earlier answer is
needed to fetch evidence or construct new state. Extra questions still consume
tokens: measure request size, cost, and end-to-end latency.

Keep raw probabilities available for downstream policy. A Noul near 0.5 means
similar probability for yes and no, not medium severity; it has no separate
confidence. Choice/Score confidence summarizes distribution concentration,
not factual correctness or permission to act. Several acceptable alternatives
can also lower confidence. Evaluate thresholds on representative user data and
the consequences of errors, and ignore uncertainty on unused branches.

Verify no-match cases, overlapping labels, missing evidence, ambiguous wording,
and the resulting routing or review behavior. Inspect the exact state, rubric,
candidates, answers, and code decisions when a case fails. Typed answers
guarantee an interface, not truth.

For current prompting guidance and worked patterns, consult the live TypeSafe
[documentation index](https://docs.typesafe.ai/llms.txt),
[Choice](https://docs.typesafe.ai/primitives/choice.md),
[Noul](https://docs.typesafe.ai/primitives/noul.md),
[Score](https://docs.typesafe.ai/primitives/score.md),
[structured questions](https://docs.typesafe.ai/primitives/advanced.md), and
[speculative fan-out](https://docs.typesafe.ai/patterns/fan-out.md).
Adapt SDK examples to Avalanche's injected callable and per-call snapshots;
do not replace the native integration with a custom client.

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

