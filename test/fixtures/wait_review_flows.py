"""Spawned operator scenarios for reviewed artifacts and terminal wait history."""

from typing import Literal

import avalanche as ava


class Approval(ava.Outcome):
    kind: Literal["approve", "reject"]


@ava.workflow
def large_artifact():
    return ava.decision(Approval, prompt="Review", show="x" * (2 * 1024 * 1024), timeout=None)


@ava.workflow
def large_response():
    return ava.decision(Approval, prompt="p" * (3 * 1024 * 1024), show="x" * 4000, timeout=None)


@ava.workflow
def timeout_outcome():
    return ava.decision(
        Approval, prompt="Review", timeout=0, on_timeout=Approval(kind="reject")
    )


@ava.workflow
def timeout_failure():
    return ava.decision(Approval, prompt="Review", timeout=0)


@ava.workflow
def mapped_large_artifacts():
    return ava.decision.map(
        Approval, ["x" * (2 * 1024 * 1024), "small"], prompt="Review", timeout=None
    )


@ava.workflow
def many_waits():
    for _ in range(70):
        ava.sleep(0)


@ava.workflow
def pending():
    return ava.decision(Approval, prompt="Review", timeout=None)
