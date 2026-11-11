"""Real local machine fixture: external event, mapped visit work and UTC timer."""

import logging
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, JsonValue

import avalanche as ava


class Ticket(ava.BaseInput):
    ticket: int
    journal: str
    pause: bool = False


@ava.step
def observe(ticket: Ticket, context: ava.RunContext) -> str:
    logging.getLogger(__name__).warning("Observing ticket %s", ticket.ticket)
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


class ArtifactTicket(Ticket):
    document: ava.File
    workspace: ava.Workspace


class ArtifactOutput(BaseModel):
    text: str
    counts: list[int]


@ava.step
def inspect_artifacts(ticket: ArtifactTicket, context: ava.RunContext) -> ArtifactOutput:
    value = (
        ticket.document.read_bytes().decode()
        + ":"
        + (ticket.workspace.path / "data.txt").read_text()
    )
    with Path(ticket.journal).open("a") as output:
        output.write(context.node_id + ":" + value + "\n")
    return ArtifactOutput(text=value, counts=[1, 2, 3])


@ava.workflow(input=ArtifactTicket)
def artifact_flow():
    return inspect_artifacts()


@ava.machine(input=ArtifactTicket, key=("ticket",))
def artifact_approval():
    pending = ava.state(inspect_artifacts, initial=True)
    done = ava.state(inspect_artifacts, final=True)
    pending.on("approved") >> done


@ava.step
def router_input(ticket: Ticket) -> dict:
    return {**ticket.model_dump(mode="json"), "ticket": str(ticket.ticket)}


@ava.step
def started_key(started: ava.RunStart) -> list:
    return [float(started.key[0])]


@ava.step(retries=1)
def retry_after_send(
    receipt: ava.MachineEventReceipt, ticket: Ticket
) -> ava.MachineEventReceipt:
    marker = Path(ticket.journal + ".router-retry")
    with marker.open("a") as output:
        output.write("attempt\n")
    if len(marker.read_text().splitlines()) == 1:
        raise ValueError("transient downstream failure after the machine send")
    return receipt


@ava.workflow(input=Ticket, key=("ticket",))
def ordinary_child():
    return observe()


class NestedIdentity(BaseModel):
    ticket: int
    label: str = "nested"


class TypedIdentity(ava.BaseInput):
    token: UUID
    nested: NestedIdentity


@ava.step
def typed_observe(input: TypedIdentity) -> dict[str, JsonValue]:
    return input.model_dump(mode="json")


@ava.machine(input=TypedIdentity, key=("token", "nested"))
def typed_approval():
    pending = ava.state(typed_observe, initial=True)
    done = ava.state(typed_observe, final=True)
    pending.on("approved") >> done


@ava.step
def typed_router_input() -> dict[str, JsonValue]:
    return {
        "token": "A0B1C2D3-E4F5-4678-9ABC-0123456789AB",
        "nested": {"ticket": "1", "ignored": "drop"},
    }


@ava.workflow
def typed_router():
    started = ava.start_run(typed_approval, typed_router_input())
    sent = typed_approval.send(
        ["{a0b1c2d3-e4f5-4678-9abc-0123456789ab}", {"ticket": 1.0}], "approved"
    )
    started >> sent
    return sent


@ava.workflow(input=Ticket, webhook=ava.Webhook(path="/router"))
def router():
    skipped = ava.start_run("not-a-target", {"invalid": True}, when=False)
    skip_send = approval.send((), "", {"input_files": []}, when=False)
    child = ava.start_run(ordinary_child, router_input())
    started = ava.start_run(approval, router_input())
    skipped >> child >> started
    skip_send >> started
    return retry_after_send(approval.send(started_key(started), "approved"))
