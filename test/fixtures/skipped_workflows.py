"""Credential-free workflows for skipped outcome transport and TUI acceptance."""

import time

from pydantic import BaseModel

import avalanche as ava


@ava.source
def optional_records():
    return ava.skip(
        "No eligible records for this partition", {"partition": "today", "count": 0},
    )


@ava.source
def successful_empty():
    return None


@ava.step
def fan_in(optional, empty):
    assert isinstance(optional, ava.Skipped)
    assert empty is None
    print("Fan-in completed: intentional absence is distinct from successful None")
    return "fan-in complete"


@ava.step
def dependency_only():
    print("Dependency-only successor completed")
    return "dependency satisfied"


@ava.workflow
def skipped_outcomes():
    optional, empty = optional_records(), successful_empty()
    joined = (optional & empty) >> fan_in()
    dependent = optional >> dependency_only()
    return optional, joined, dependent


@ava.source
def failure():
    raise ValueError("Deliberate acceptance failure")


@ava.source
def cancellable():
    time.sleep(30)


@ava.workflow
def failed_dependency():
    failure() >> dependency_only()


@ava.workflow
def cancelled_dependency():
    cancellable() >> dependency_only()


class NestedOutcome(BaseModel):
    outcome: ava.Skipped


@ava.source
def nested_result():
    return NestedOutcome(outcome=ava.skip("intentional", {"count": 0}))


@ava.workflow
def nested_skipped_result():
    return nested_result()


class ApplicationContext(ava.BaseContext):
    label: str = "application context"


@ava.source
def skip_container():
    return [ava.skip("nested outcome"), ava.skip("nested outcome")]


@ava.source(num_returns=2)
def equal_skip_slots():
    return ava.skip("equal slot"), ava.skip("equal slot")


@ava.source(num_returns=2)
def omitted_pair():
    return ava.skip("Whole producer intentionally omitted", {"count": 0, "slots": 2})


@ava.step
def inspect_outcome_slots(left, right, nested, first, second):
    assert left == right == ava.skip(
        "Whole producer intentionally omitted", {"count": 0, "slots": 2},
    )
    assert nested == [ava.skip("nested outcome"), ava.skip("nested outcome")]
    assert first == second == ava.skip("equal slot")
    return "all slots consumed; downstream completed"


@ava.step
def inspect_application_context(context: ApplicationContext):
    return context.model_dump()


def outcome_graph():
    pair, equal = omitted_pair(), equal_skip_slots()
    return inspect_outcome_slots(
        pair[0], pair[1], skip_container(), equal[0], equal[1],
    )


@ava.workflow
def run_context_outcomes():
    return outcome_graph()


@ava.workflow(context=ApplicationContext)
def application_context_outcomes():
    return outcome_graph(), inspect_application_context()
