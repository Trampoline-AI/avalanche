# Native agent-step evaluations

**Status:** agreed specification; implementation pending.
**Issue:** [AVA-81](https://linear.app/avalanche-ai/issue/AVA-81/native-criterias)
**Repository:** https://github.com/Trampoline-AI/avalanche

## Purpose

- Declare quality metrics directly on agent steps.
- Evaluate outputs and agent traces automatically, without a separate workflow.
- **Observe only:** evaluations never delay, fail, retry, or change workflow execution.

## Core decisions

| Area | Decision |
| --- | --- |
| Execution mode | Automatic evaluations in operator mode only |
| Trigger | Each successful step execution, using its final return value |
| Traces | Include every agent invocation in that step; reuse existing trace models |
| Reruns | Separate evaluation records for each execution |
| Metric | One state selector + one Jev-format question = one named answer |
| Context | Existing inputs, returned object, and traces; optional typing, no duplicate schema |
| Composites | Python functions combining metric results into scores in `[0, 1]` |
| Batching | Group metrics sharing selected state and compatible evaluator configuration |
| Errors | Visible on evaluations only; no effect on workflow status or outputs |
| Evaluator | Jev only initially; requires `TYPESAFE_API_KEY`, like classifier steps |
| Configuration | Reuse classifier model/timeout conventions and defaults; no new configuration system |

## Proposed API

Illustrative Avalanche API; result access follows TypeSafe's existing response API.
`Report` and `ReportSignature` are author-defined types.

```python
evaluations = ava.Evaluations(
    metrics={
        "clarity": ava.Metric(
            state=lambda ctx: ctx.output.summary,
            question={
                "type": "score",
                "instructions": "How understandable is this summary?",
                "criteria": ["Confusing", "Mostly clear", "Clear throughout"],
            },
        ),
        "checked_sources": ava.Metric(
            state=lambda ctx: ctx.trace,
            question={
                "type": "noul",
                "instructions": "Did the agent cross-check its main claims?",
            },
        ),
    },
    composites={
        "quality": lambda results: (
            0.7 * (results.scores["clarity"].score / 2)
            + 0.3 * results.nouls["checked_sources"].noul
        ),
    },
)

@ava.agent_step(ReportSignature, evaluations=evaluations)
async def research(topic: str, *, agent: ava.Agent) -> Report:
    prediction = await agent(topic=topic)
    return prediction.report
```

The clarity rubric has three levels (`0–2`), so `/ 2` normalizes its score.
Composites receive the collected answers using TypeSafe's `answers`, `scores`,
`nouls`, and `choices` accessors.

### Context and questions

- `ctx.inputs`: bound user arguments keyed by parameter name, including defaults, excluding injected services.
- `ctx.output`: the step's returned value—not necessarily the raw agent prediction.
- `ctx.trace`: existing traces from all calls, retaining invocation IDs and unavailable-trace errors.
- Select any field or combination, e.g. `{"request": ctx.inputs["topic"], "answer": ctx.output.conclusion}`.
- `question` accepts **one entry** from `classifier_step(questions={...})`:
  - **Noul:** probability of yes.
  - **Score:** position on ordered descriptive levels.
  - **Choice:** selected alternative and its probabilities.
- No magic `"output"` selector strings or second question language.
- No required input/output type declarations or extra models. Reuse existing step contracts; optional selector annotations aid static checking.

### Batching

```text
clarity      ─┐
completeness ─┴─ same summary state → one Jev request, two questions
source_check ─── trace state        → separate Jev request
```

- The evaluator constructs requests; authors declare individual metrics.
- Batch only matching state and compatible model/request settings, within service limits.
- Never combine different states into one larger object: every question would see extra evidence.
- Map answers back to metric names before calculating composites.
- Keep question declarations independent of model-call code; standard-LLM support is deferred.

## Execution and user experience

```text
Workflow:   step completes ───────────────────> downstream steps continue
                   └── submit evaluation
Evaluation:            context → metrics → composites → results/errors
```

- An **operator-owned worker** continues independently after workflow coordinators finish.
- No waiting for evaluations before step completion, downstream execution, or workflow result delivery.
- An **Evaluations** section on each step execution shows metric answers, composite scores, or errors, with separate pending/completed/failed states.
- A successful workflow may still have pending or failed evaluations. Late results do not reopen it.
- Store results against that specific step execution; reruns never overwrite earlier evaluations.
- Embedded Python accepts declarations but executes no evaluations or background workers; clearly report **not evaluated**, not success or pending.
- Declaration/discovery performs no model calls.

## Out of scope

- File-content evaluation, extraction, images, or other media; a path is not file evidence.
- Embedded evaluation runners or shutdown/draining APIs.
- Hard gates, automatic retries, routing, or self-correction.
- Durable recovery after application crashes.
- Standard-LLM evaluators; Jev is the initial implementation.
- Dashboards, aggregate analytics, or additional evaluation controls.

Keep evaluation computation separate from reporting/policy so future explicit gates or embedded runners can reuse it—without implementing either now.

## Delivery

| Ticket | Scope | Blocked by |
| --- | --- | --- |
| [AVA-82](https://linear.app/avalanche-ai/issue/AVA-82) | Authoring API, Jev engine, batching, composites | None |
| [AVA-83](https://linear.app/avalanche-ai/issue/AVA-83) | Operator processing, execution evidence, result storage/API | AVA-82 |
| [AVA-84](https://linear.app/avalanche-ai/issue/AVA-84) | Evaluation display and illustrated documentation | AVA-83 |

- Each ticket includes focused verification and relevant documentation updates.
- Update repository guides/documentation-site pages and the Avalanche workflow-authoring skill/references to match the implemented API.
- Include real screenshots from the running operator in the feature documentation: metric/composite results and a representative evaluation error. No mockups or placeholders; exclude credentials/private data.
- Verify the documented example, links, and rendered images. Screenshot capture belongs to the display ticket after implementation.

## Implementation details to resolve

Product decisions above are agreed. The following are engineering details, not additional author-facing configuration.

| Topic | Still to specify |
| --- | --- |
| Worker integration | Evidence handoff, independent result channel, capacity pressure, operator shutdown |
| Context handoff | Aggregate existing traces and preserve execution snapshots without additional author schemas |
| Jev batching | State equivalence and request limits |

## Acceptance checks

- Attach metrics/composites; select individual output fields, input/output combinations, and all invocation traces.
- Preserve existing valid Noul/Score/Choice questions; reject malformed declarations.
- Verify shared-state batching, separate-state isolation, and correct answer-to-metric mapping.
- An evaluation failure shows an error describing what failed; workflow execution is unaffected.
- Finish the workflow before evaluation: pending work survives normal coordinator exit and later results appear on the correct execution; reruns stay separate.
- Verify schema-free selectors; separately verify boundary validation, normalized composites, and snapshot isolation (annex).
- Embedded execution reports evaluations were not run; discovery makes no model calls; no files are opened for evaluation.
- Repository documentation, documentation-site pages, and the workflow-authoring skill agree with the implemented feature; the user guide includes verified operator screenshots.

## Annex — implementation constraints

### Isolation and lifetime

- Completion hooks submit work only; selectors, validation, model calls, composites, and publication run off the workflow scheduler.
- Submission overhead is unavoidable; evaluation completion and queue backpressure must never block workflow progress.
- Reuse existing execution/storage boundaries for stable evidence: downstream mutation must not change evaluation state, and selectors must not mutate workflow data.
- Use a result channel independent of the coordinator's terminal event channel. Worker process/thread layout is an implementation choice.
- If evaluation fails, show the error. No fallback scores, special dependency handling, or partial-recovery machinery.
- Failed workflow steps have no successful final return and do not trigger these evaluations.

### Typing and validation

- Expose existing execution objects; do not require `input_type`/`output_type`, construct duplicate input models, or revalidate the entire context.
- Optional selector annotations aid static checking; do not promise automatic lambda type inference.
- Missing fields or selector exceptions become evaluation errors, never workflow errors.
- Validate questions at declaration using existing classifier rules.
- Validate selected state as Jev-compatible text/JSON with finite numbers. Convert Python-only values explicitly; do not stringify arbitrary objects.
- Validate service answers against their declared question types/options; composite results must be finite numbers in `[0, 1]`.
- Trace aggregation must preserve existing bodies, invocation IDs, and unavailable-trace errors. A direct `ctx.trace` selector requires a JSON-compatible aggregate.

### Result semantics

- Reuse TypeSafe's answer API: `.score`, `.legend`, `.noul`, `.choice`, `.probabilities`, and `.confidence` where supported.
- Preserve raw answers; do not add `.normalized` or `.probability` aliases.
- Normalize an `N`-level Score in the formula with `score / (N - 1)`.
- Composites are ordinary Python functions over metric answers; no additional model call or composite dependency system.

### Existing implementation references

- `src/avalanche/agent/agent_step.py`: step boundary; `_emit_terminal_trace` exports `to_exportable_json()`.
- `src/avalanche/_agent_evidence.py`: `AgentTraceFinishedEvent` and `AgentTraceUnavailableEvent`, keyed by invocation ID.
- `src/runtime/operator/models.py`: `TraceHeader`, `TraceDescriptor`, `TraceDetail`.
- `src/avalanche/classifier/models.py`: question/state/answer validation.
- `docs/agent-steps.md`, `docs/classifier-steps.md`: current authoring contracts.
- TypeSafe: [state](https://docs.typesafe.ai/concepts/state), [composite scoring](https://docs.typesafe.ai/patterns/composite-scoring).
- [TypeSafe response API](https://docs.typesafe.ai/sdk/python/api/types/responses): existing result accessors.
