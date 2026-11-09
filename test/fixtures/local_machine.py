"""Real local machine fixture: external event, mapped visit work and UTC timer."""

import time
from datetime import timedelta
from pathlib import Path

import avalanche as ava


class Ticket(ava.BaseInput):
    ticket: int
    journal: str
    pause: bool = False


@ava.step
def observe(ticket: Ticket, context: ava.RunContext) -> str:
    with Path(ticket.journal).open("a") as output:
        output.write(context.step_key + "\n")
    while ticket.pause and not Path(ticket.journal + ".release").exists():
        time.sleep(0.05)
    return context.step_key


@ava.step
def mapped(item: int, context: ava.RunContext) -> str:
    return context.step_key


@ava.workflow(input=Ticket)
def review_work():
    observe()
    return mapped.map([1, 2])


@ava.machine(input=Ticket, key=("ticket",))
def approval():
    pending = ava.state(observe, initial=True)
    reviewed = ava.state(review_work)
    done = ava.state(observe, final=True)
    pending.on("approved") >> reviewed
    reviewed.after(timedelta(seconds=2)) >> done


@ava.step
def router_input(ticket: Ticket) -> dict:
    return ticket.model_dump(mode="json")


@ava.step
def started_key(started: ava.MachineStart) -> list:
    return started.key


@ava.workflow(input=Ticket, webhook=ava.Webhook(path="/router"))
def router():
    started = approval.start(router_input())
    return approval.send(started_key(started), "approved")
