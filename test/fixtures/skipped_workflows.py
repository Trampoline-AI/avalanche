"""Credential-free workflows for skipped outcome transport and TUI acceptance."""

import time

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
