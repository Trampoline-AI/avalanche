# Classifiers

Avalanche 0.7.0's workflow-author classification API, backed by TypeSafe. See [workflows](workflows.md) for node calls, composition, and workflow defaults, and [inputs and context](inputs-context.md) for ordinary step injection. Classifier state and questions are JSON data; they are distinct from [agent signatures](agents.md#signature-declarations).

## Classifier step

```python
ava.classifier_step(
    *,
    questions: dict[str, JsonValue] | None = None,
    input_model: type[pydantic.BaseModel] | None = None,
    model: str | None = None,
    timeout: float | None = None,
    slug: str | None = None,
)
```

Returns a decorator that turns a function into a single-output workflow step. Use `@ava.classifier_step(...)` or `@ava.classifier_step()`, not the bare decorator. `JsonValue` means a JSON-compatible value.

| Parameter | Default and behavior |
| --- | --- |
| `questions` | `None`; optional nonempty question mapping in the [shapes below](#question-shapes). Validated and snapshotted at decoration. If absent, supply questions on calls. |
| `input_model` | `None`; optional Pydantic model **class**, including `RootModel`, used to validate each call's state. Not a model instance. Its serialization schema must be constructible at decoration. |
| `model` | `None` inherits workflow/default configuration; otherwise a nonblank TypeSafe model name. |
| `timeout` | `None` inherits; otherwise positive finite seconds per HTTP operation, not a whole-step deadline. |
| `slug` | `None`; optional node identifier, following [ordinary node rules](workflows.md). |

The body must declare exactly `*, classifier: ava.Classifier`, with no default. Avalanche injects it and removes it from the step's public call signature. Synchronous and asynchronous bodies are accepted; awaitable results are awaited. The body decides what to return or persist:

```python
import avalanche as ava

@ava.classifier_step(
    questions={
        "department": {
            "type": "choice",
            "criteria": {"support": "Support request", "sales": "Purchase request"},
        }
    }
)
async def classify_ticket(ticket: dict, *, classifier: ava.Classifier) -> str:
    result = await classifier(state=ticket)
    return result.choices["department"].choice
```

Python step annotations describe workflow inputs/results; `input_model` separately describes the state sent on each classifier call. The decorator performs no automatic routing or application-table writes.

### Workflow defaults and credentials

Declare `classifier_defaults={"model": ..., "timeout": ...}` on [the workflow decorator](workflows.md). Only these two keys are allowed. Each resolves from a non-`None` decorator value, then workflow defaults, then:

| Key | Default | Constraint |
| --- | --- | --- |
| `model` | `"jev-latest"` | Nonempty, non-whitespace-only string. |
| `timeout` | `10.0` | Positive finite seconds per HTTP operation. |

`None` values and extra keys are invalid in workflow defaults. `agent_defaults` does not configure classifiers. Avalanche supplies the resolved model explicitly, so the SDK's `TYPESAFE_DEFAULT_MODEL` environment variable does not replace it. Calls have no per-call model or timeout override. The SDK retains its default retry behavior; Avalanche does not add another retry layer.

Runtime calls require `TYPESAFE_API_KEY`. If the variable is absent, the executing process loads the nearest `.env` from its working directory upward without overriding existing variables. An existing blank variable does not trigger that lookup and fails the credential check. Declaration/import does not require credentials; distributed workers need their own accessible credentials.

## Injected Classifier call

```python
result = await classifier(
    state=state,        # required: text, JSON object, or JSON array
    questions=None,    # optional complete replacement question mapping
)  # -> ava.ClassificationResult
```

Both arguments are keyword-only. Await calls inside the step body; the injected classifier cannot be used after the body finishes. The framework manages its resources.

`state` must be text, a JSON object, or a JSON array. Nested values may be strings, booleans, integers, finite floats, null, lists, and dictionaries with string keys. Top-level null/numbers/booleans, Python-only values such as tuples, sets, datetimes and Pydantic instances, non-string keys, and cycles are rejected. Serialize application models explicitly, for example `item.model_dump(mode="json")`. Read [workflow files](files-workspaces.md) into appropriate content before supplying classifier state; file objects are not JSON state.

With `input_model`, Avalanche first checks JSON compatibility, then validates against that model as JSON. It sends the model's `model_dump(mode="json", by_alias=True)`, including defaults and aliases, after rechecking the content shape. The author's model validators/configuration control coercion; declaring `input_model` does not force it to be strict. Invalid state fails before any request.

| Step questions | Call questions | Effective questions |
| --- | --- | --- |
| Mapping | Omitted or `None` | Step defaults. |
| `None` | Mapping | Call mapping. |
| Mapping | Mapping | Complete replacement, not a merge. |
| `None` | Omitted or `None` | Error before any request. |

An explicit empty or malformed mapping fails rather than falling back. Step defaults are snapshotted at decoration; call questions are validated and copied. Later mutations to the original defaults do not change the declaration.

## Question shapes

Supply a nonempty dictionary mapping nonempty question IDs to JSON dictionaries. Each question requires its `type` discriminator; unknown types and extra fields are rejected. Ordinary question IDs and option names must have at least one character; unlike model names, whitespace-only strings are not specially rejected.

In this section, **content** means text, a JSON object, or a JSON array with the nested-value rules above; **description** means content or `None`. Bare numeric/Boolean descriptions are not accepted, though they may appear inside structured content.

| Shape | Field | Type and default |
| --- | --- | --- |
| Choice | `type` | Required `"choice"`. |
| | `instructions` | Description; default `None`. |
| | `criteria` | Required nonempty mapping of nonempty option names to descriptions. |
| Noul | `type` | Required `"noul"`. |
| | `instructions` | Description; default `None`. |
| | `criteria` | Mapping with only `"true"` and/or `"false"` keys to descriptions, or `None`; default `None`. Either key, both keys, or an empty mapping is allowed. |
| Score | `type` | Required `"score"`. |
| | `instructions` | Description; default `None`. |
| | `criteria` | Required ordered list of at least two content values. Null level descriptions are not allowed. |

```python
questions = {
    "category": {
        "type": "choice",
        "instructions": "Select the best category.",
        "criteria": {"bug": "Broken behavior", "request": "Requested capability"},
    },
    "urgent": {"type": "noul", "criteria": {"true": "Needs immediate action"}},
    "impact": {"type": "score", "criteria": ["Minor", "Moderate", "Major"]},
}
```

Choice selects an option; Noul returns the probability of true, not a Boolean decision; Score returns an expected zero-based level, not a rounded label. Instructions and descriptions can remain structured JSON.

`ChoiceQuestion`, `NoulQuestion`, and `ScoreQuestion` from `avalanche.classifier` are validated models of these shapes, but the decorator/call boundary expects **JSON dictionaries, not nested model instances**. If using those models to assemble questions, serialize each to a dictionary with `model_dump(mode="json")` before passing it. Model field validation is strict, forbids extra fields and nonfinite numbers, and rejects invalid shapes with Pydantic validation errors.

## ClassificationResult

An awaited call returns `ava.ClassificationResult`. Read answers by the supplied question IDs:

```python
category = result.choices["category"].choice
urgent_probability = result.nouls["urgent"].noul
impact_score = result.scores["impact"].score
input_tokens = result.usage.input_tokens
```

| Field/property | Type | Meaning |
| --- | --- | --- |
| `model` | Nonempty `str` | Model reported by the service; may differ from the requested name. |
| `answers` | Nonempty mapping of IDs to `ChoiceAnswer \| NoulAnswer \| ScoreAnswer` | Every returned answer. |
| `usage` | `ClassificationUsage` | Token usage; individual counts may be unknown. |
| `choices` | `dict[str, ChoiceAnswer]` | Read-only property returning a new filtered dictionary; answer objects are not deep-copied. |
| `nouls` | `dict[str, NoulAnswer]` | Corresponding Noul filter. |
| `scores` | `dict[str, ScoreAnswer]` | Corresponding Score filter. |

`model`, `answers`, and `usage` are required result fields. Result/answer/usage model classes are available from `avalanche.classifier`. They use strict Pydantic validation, forbid extra fields and nonfinite numbers, and prevent field reassignment; nested dictionaries/lists are not deeply immutable. Use `result.model_dump(mode="json")` for JSON-shaped application output.

### Returned answer fields

Every field below is required. A probability is a finite number in `[0, 1]`.

| Answer | Field | Value |
| --- | --- | --- |
| `ChoiceAnswer` | `type` | `"choice"`. |
| | `choice` | Selected option name. |
| | `probabilities` | Mapping of option names to probabilities. |
| | `confidence` | Service confidence in `[0, 1]`; not inferred from the chosen probability by Avalanche. |
| `NoulAnswer` | `type` | `"noul"`. |
| | `noul` | Probability of true. |
| `ScoreAnswer` | `type` | `"score"`. |
| | `score` | Finite probability-weighted zero-based level. |
| | `legend` | Mapping of string indices `"0"` through `str(n-1)` to the declared content levels; at least two entries. |
| | `probabilities` | Mapping of those same indices to probabilities. |
| | `confidence` | Service confidence in `[0, 1]`. |

Choice must identify an option with the highest probability (ties allowed). Score must lie in `[0, n-1]` and equal `sum(index * probability)`. These comparisons use relative and absolute tolerance `1e-5`. Choice/Score probability totals must be positive and within `0.005 * number_of_options + 1e-12` of 1 to accommodate rounded service probabilities; Avalanche does not renormalize them.

Before returning, Avalanche checks the raw response against the supplied questions: answer IDs and types must match exactly, Choice option sets must match, and Score legends must preserve every declared level. Malformed responses fail rather than silently losing unknown answers or options.

### Usage fields

| `ClassificationUsage` field | Type | Default |
| --- | --- | --- |
| `input_tokens` | Nonnegative `int \| None` | `None`. |
| `output_tokens` | Nonnegative `int \| None` | `None`. |

Unknown usage is `None`, not zero.

## Errors

`ava.ClassifierStepError` and `ava.ClassifierStepExecutionError` are separate direct `RuntimeError` subclasses.

| Error | Caller-visible condition |
| --- | --- |
| `ClassifierStepError` | Invalid decorator options, invalid injected `classifier` parameter, an attempt to override the injected classifier, or use after the step has finished. |
| `ClassifierStepExecutionError` | Invalid call state/questions (including missing questions), `input_model` validation failure, missing credentials, SDK/network failure, invalid service response, or resource cleanup failure without an earlier body error. |
| `asyncio.CancelledError` | Cancellation propagates without conversion. |

Declaration errors are raised while decorating; invalid call inputs fail before a request. Execution messages include the step name and a sanitized error description rather than raw state, credentials, or server-echoed text. Exceptions from the surrounding user-written step body propagate as body errors.
