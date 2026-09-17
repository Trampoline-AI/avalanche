# Classifier steps

`@ava.classifier_step` is an ordinary Avalanche step with fixed
[TypeSafe](https://typesafe.ai/) questions and an injected, awaitable
`ava.Classifier`. Its Python body prepares state, calls the classifier, and
chooses what to return or persist. It is not an agent loop: there are no tools,
automatic routes, confidence thresholds, or implicit table writes.

TypeSafe support is included in the base Avalanche installation through
`typesafe-sdk`; no optional extra is required.

## Quick start

Set `TYPESAFE_API_KEY` in the environment of the process executing the workflow.
Then save and run this Python file:

```python
import avalanche as ava


QUESTIONS = {
    "department": {
        "type": "choice",
        "instructions": "Which team should review this support ticket?",
        "criteria": {
            "billing": "Payments, invoices, or refunds.",
            "technical": "Product failures or service availability.",
        },
    },
    "urgent": {
        "type": "noul",
        "instructions": {"task": "Does this need immediate attention?"},
        "criteria": {
            "true": "The customer cannot use the service.",
            "false": "The request can wait for normal support.",
        },
    },
    "severity": {
        "type": "score",
        "instructions": ["Assess customer impact.", "Use the ordered rubric."],
        "criteria": ["minor", {"impact": ["service unavailable"]}],
    },
}


@ava.classifier_step(questions=QUESTIONS, timeout=20.0, slug="classify-ticket")
async def classify_ticket(ticket: str, *, classifier: ava.Classifier) -> ava.ClassificationResult:
    return await classifier(state={"ticket": ticket})


@ava.workflow(classifier_defaults={"model": "jev-latest", "timeout": 30.0})
def ticket_workflow():
    return classify_ticket("Our team cannot sign in to the service.")


if __name__ == "__main__":
    result = ticket_workflow().run(executor=ava.LocalExecutor()).result()
    print(result.choices["department"].choice)
    print(result.nouls["urgent"].noul)
    print(result.scores["severity"].score)
    print(result.model_dump_json(indent=2))
```

The required keyword-only `classifier: ava.Classifier` parameter has no default
and is injected by Avalanche. Workflow callers pass only ordinary inputs; they
cannot supply their own classifier. A call accepts only `state=`, containing a
string, JSON object, or JSON array. Nested values must also be JSON-compatible:
string keys, finite numbers, booleans, null, objects, and arrays. Python-only
objects such as sets, datetimes, or Pydantic models must be converted explicitly.

The standalone [classifier workflow example](../examples/classifier_workflow.py)
exports `classifier_workflow`, with `load_ticket` and `classify_ticket` nodes.
From the repository root, after setting the key:

```bash
uv run python examples/classifier_workflow.py
uv run ava dev examples/classifier_workflow.py
```

The first command prints the typed result as JSON. The second starts the operator
workspace: inspect the questions before launching the workflow,
then inspect the run's invocations. Discovery itself does not require a key or
contact TypeSafe. Live classification requires valid credentials and model
access; example answers are not guaranteed.

## Question declarations

`questions` is a nonempty JSON-shaped dictionary keyed by nonempty question IDs.
Each value has a `type`, optional `instructions`, and the corresponding
`criteria`. IDs connect returned answers to their questions; declaration order
is preserved. The quick start shows all three shapes.

| Type | Criteria | Answer |
| --- | --- | --- |
| `"choice"` | Required nonempty object mapping option names to descriptions | Selected option, all option probabilities, and confidence |
| `"noul"` | Optional object with string keys `"true"` and/or `"false"`, or null | Probability of yes, not a Boolean decision |
| `"score"` | Required ordered array of at least two level descriptions | Probability-weighted position on the zero-based levels, legend, all level probabilities, and confidence |

Instructions may be text, a JSON object, a JSON array, or null; omission means
null. Choice and Noul criterion descriptions accept the same forms. Each Score
level must be text, an object, or an array, not null. Structured content is
passed as JSON and displayed as structured values, not converted to a Python
string representation. Extra question fields and unknown question types are
rejected.

Declarations are validated and snapshotted when the decorator runs. Later
mutation of the original dictionary, including nested instructions or criteria,
does not change the step. Questions cannot be overridden on an invocation;
call the same fixed questions with different state, or declare another step.
Keep runtime input out of the static declaration.

## Configuration and client lifecycle

The complete decorator configuration is:

- `questions=`: required static question dictionary.
- `model=`: optional nonblank TypeSafe model name.
- `timeout=`: optional positive, finite number of seconds.
- `slug=`: optional node identifier, following the [DAG API](dag-api.md).

Only `model` and `timeout` are accepted in
`@ava.workflow(classifier_defaults={...})`. Each field resolves independently:

1. Explicit non-`None` value on `@ava.classifier_step`.
2. The same field in `classifier_defaults`.
3. Avalanche defaults: model `"jev-latest"`, timeout `10.0` seconds.

Thus the quick start uses `jev-latest` and a 20-second timeout, not the
workflow's 30 seconds. Omitting a decorator option or setting it to `None`
inherits the workflow/default value; `None` is not a valid value inside
`classifier_defaults`. `agent_defaults` does not affect classifiers. Avalanche
passes the resolved model explicitly, so the SDK's `TYPESAFE_DEFAULT_MODEL`
environment variable does not override it.

`timeout` configures the SDK's HTTP-operation timeout, not an end-to-end step
deadline. The SDK retains its own default request retry policy; Avalanche adds
no second retry layer and exposes no classifier retry, fallback, client,
credential, or per-call model override option.

The async TypeSafe client is created lazily on the first call, inside the
executing process. Calls in one execution of a step share that client. Avalanche
closes it when the step exits, including on error or cancellation, and cancels
outstanding calls during cleanup. Await all calls inside the step; do not retain
the injected classifier for later use. Another step execution gets its own
client. This lifecycle also applies when execution crosses a Ray worker boundary.

Credentials come from runtime `TYPESAFE_API_KEY`, not decorator configuration,
workflow defaults, or serialized declarations. Ensure local, operator, and Ray
worker processes have the required environment. Importing a workflow, building
its DAG, and discovering its questions do not create a client or make a model
request. The requested model name is retained in the declaration; the model
reported by TypeSafe is retained separately in the result.

## Typed results and serialization

Every successful `await classifier(state=...)` returns
`ava.ClassificationResult`, a validated Pydantic model with:

- `model`: the model reported by TypeSafe.
- `answers`: question IDs mapped to `ChoiceAnswer`, `NoulAnswer`, or `ScoreAnswer`.
- `usage.input_tokens` and `usage.output_tokens`: token counts.
- `choices`, `nouls`, and `scores`: typed, question-ID-keyed views of the answers.

Answer types are also importable from `avalanche.classifier`. Their fields are:

| Accessor | Fields |
| --- | --- |
| `result.choices["department"]` | `type="choice"`, `choice`, `probabilities`, `confidence` |
| `result.nouls["urgent"]` | `type="noul"`, `noul` |
| `result.scores["severity"]` | `type="score"`, `score`, `legend`, `probabilities`, `confidence` |

Probabilities and confidence lie in `[0, 1]`. Choice and Score preserve the full
probability distribution; Noul has no separate confidence field. Score is a
fractional, probability-weighted level position, not a rounded or selected
level. Score legend and probability keys are JSON strings (`"0"`, `"1"`, ...).
Avalanche validates exact question IDs, answer types, Choice options, and Score
levels against the declaration, rather than silently dropping or inventing
answers.

For the `result` produced above, serialize or reconstruct without losing those
fields:

```python
payload = result.model_dump(mode="json")
encoded = result.model_dump_json()
restored = ava.ClassificationResult.model_validate_json(encoded)
print(restored.choices["department"].probabilities)
```

Returning this model is optional. The step may return a Boolean, a custom
Pydantic model, a table append receipt, or another ordinary node result. The
body owns any thresholds, routing decisions, validation, and persistence; the
primitive never infers them from probabilities.

## Multiple calls, errors, and evidence

One step may make zero, one, or many calls. Repeated calls use the same declared
questions, and concurrent calls such as
`await asyncio.gather(classifier(state=first), classifier(state=second))`
receive separate invocation IDs. Invocation indexes are zero-based and allocated
when calls start, not when they finish.

Each call emits a `running` snapshot and a terminal `success`, `failed`, or
`cancelled` snapshot. Evidence includes its invocation ID/index, start/end
timestamps, resolved declaration, input state, result on success, and an error
description on failure or cancellation. Valid input is captured as a detached
snapshot of the state sent to TypeSafe, not inferred from the node's inputs.
It remains attached to running and terminal snapshots, including failed and
cancelled calls. A successful result is captured before control returns to the
step body. If later Python postprocessing fails, the node fails but the already
captured input and answers remain inspectable within retention limits. These
records are independent of what the node returns; they are not inferred from
its return value or presented as an agent reasoning trace.

Invalid declarations raise `ava.ClassifierStepError`. Missing credentials,
request failures, invalid state, and malformed or declaration-mismatched answers
surface as `ava.ClassifierStepExecutionError`; there are no fabricated fallback
answers. Ordinary errors in the step body remain ordinary node errors.
`asyncio.CancelledError` propagates as cancellation, with no result invented for
the cancelled call. Cancellation is cooperative: if an operator worker is
terminated before it publishes terminal evidence, the UI marks the remaining
running invocation as interrupted rather than claiming an answer was returned.

Invocation evidence retains valid input state, which may contain sensitive
application data. If state fails validation before capture, the invocation's
`input` is null; null does not stand for an empty string, array, or object.
Avalanche does not capture credentials from the TypeSafe client or environment,
and exception descriptions remain sanitized. This is not blanket redaction:
secrets explicitly placed in state, ordinary node inputs/outputs, user logging,
question declarations, or returned answer content can still be retained.
State is also sent to TypeSafe to perform the classification.

## Inspecting current workflows and historical runs

Classifier nodes have a distinct cyan identity and filter-list icon in the
operator graph and inspector, separate from agent styling and execution-status
colors:

- **Current workflow:** **Definition** opens by default, showing the requested
  model, timeout, and type-specific question explorers. Choice exposes named
  options and their descriptions; Noul exposes yes/no criteria; Score exposes
  its ordered, zero-based levels. Structured instructions and criteria remain
  inspectable as JSON. The secondary **Code** tab shows the step's Python source.
  These are the current workflow's definition and code, not historical evidence.
- **Selected run:** **Calls** opens by default, with **Definition** available for
  the questions prepared for that run. Editing or reloading the workflow does
  not replace its historical questions. Historical inspection never substitutes
  current source code for the retained definition.
- **Call detail:** Select a call to inspect its **Input state**, outcome, model,
  token usage, and typed answers when available. Input is displayed as the
  captured text or JSON, paired with that call's answers rather than the node's
  arguments or return value. Choice shows every option probability and confidence;
  Noul shows probability of yes; Score shows the fractional score, ordered legend,
  every level probability, and confidence. Multiple calls stay separate, while
  lifecycle snapshots for one call are grouped together.
- **Availability:** Not invoked, running, failed, cancelled, interrupted, and
  unavailable details are distinct states. History is paged and bodies load on
  demand; the browser offers reload/retry controls rather than treating a missing
  body as an empty result.
  When a terminal record falls outside the loaded history window, its outcome is
  reported as not loaded rather than inferred from an older running record.

These views are local-development inspection, not a durable execution journal.
The entire invocation detail body, including input and results, shares the
operator's result retention window (24 hours after a terminal run by default)
and event/node/run size limits. Bodies are released when the operator closes.
Historical descriptors can outlive their input and answer bodies; a retained
run does not guarantee that its full evidence is still available. Expired or
size-limited bodies are unavailable, not empty input or empty answers. Browser
caches are bounded independently. There is no recovery or cross-restart history
guarantee: explicitly persist required business results in your step.

Avalanche remains intended for local development and experimentation. This
feature adds no production authentication boundary or remote-service deployment
guarantee; an embedding host remains responsible for access control.
