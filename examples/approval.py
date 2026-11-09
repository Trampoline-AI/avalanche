"""Run with `ava operator approval.py`, then `ava run approval --input '{"ticket":1}'`."""

from datetime import timedelta

from pydantic import BaseModel

import avalanche as ava


class Request(ava.BaseInput):
    ticket: int


class Approval(BaseModel):
    reviewer: str


@ava.step
def waiting(request: Request) -> int:
    return request.ticket


@ava.step
def acknowledge(payload: Approval) -> str:
    return payload.reviewer


@ava.step
def complete(payload: Approval) -> str:
    return f"Approved by {payload.reviewer}"


@ava.machine(input=Request, key=("ticket",))
def approval():
    pending = ava.state(waiting, initial=True)
    cooling = ava.state(acknowledge)
    done = ava.state(complete, final=True)
    pending.on("approved", payload=Approval) >> cooling
    cooling.after(timedelta(seconds=2)) >> done
