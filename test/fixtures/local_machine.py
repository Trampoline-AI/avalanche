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
    tree_path = ticket.workspace.path / "data.txt"
    tree_path.write_text(tree_path.read_text() + "x")
    with Path(ticket.journal).open("a") as output:
        output.write(context.node_id + ":" + value + "\n")
    return ArtifactOutput(text=value, counts=[1, 2, 3])


@ava.workflow(input=ArtifactTicket)
def artifact_flow():
    return inspect_artifacts()


class ArtifactDelivery(BaseModel):
    document: ava.File
    workspace: ava.Workspace


@ava.step
def inspect_delivery(
    delivery: ArtifactDelivery, ticket: ArtifactTicket
) -> dict[str, JsonValue]:
    return {
        "sha256": delivery.document.snapshot().sha256,
        "reference": delivery.workspace.reference,
        "bytes": delivery.document.read_bytes().decode(),
        "tree": (delivery.workspace.path / "data.txt").read_text(),
    }


@ava.step
def artifact_child_input(ticket: ArtifactTicket) -> ArtifactTicket:
    return ticket


@ava.workflow(input=ArtifactTicket)
def artifact_child():
    return inspect_artifacts()


@ava.workflow(input=ArtifactTicket)
def artifact_final_visit():
    inspected = inspect_artifacts()
    delivered = inspect_delivery()
    prepared = artifact_child_input()
    inspected >> prepared
    child = ava.start_run(artifact_child, prepared)
    return delivered, child


@ava.machine(input=ArtifactTicket, key=("ticket",))
def artifact_approval():
    pending = ava.state(inspect_artifacts, initial=True, defer=("artifacts",))
    reviewed = ava.state(inspect_artifacts)
    done = ava.state(artifact_final_visit, final=True)
    pending.on("approved") >> reviewed
    reviewed.on("artifacts", payload=ArtifactDelivery) >> done


@ava.step
def artifact_delivery(ticket: ArtifactTicket) -> ArtifactDelivery:
    return ArtifactDelivery(document=ticket.document, workspace=ticket.workspace)


@ava.workflow(input=ArtifactTicket)
def artifact_router():
    prepared = artifact_child_input()
    started = ava.start_run(artifact_approval, prepared)
    buffered = artifact_approval.send(started_key(started), "artifacts", artifact_delivery())
    started >> buffered
    approved = artifact_approval.send(started_key(started), "approved")
    buffered >> approved
    return started, buffered, approved


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


class CallInput(ava.BaseInput):
    id: int
    depth: int = 0
    target: str = "echo_child"
    journal: str = ""
    fail: bool = False
    document: ava.File | None = None
    workspace: ava.Workspace | None = None


class CallOutput(BaseModel):
    id: int
    document: ava.File | None = None
    workspace: ava.Workspace | None = None


@ava.step(retries=0)
def echo_result(value: CallInput, context: ava.RunContext) -> CallOutput:
    if value.journal:
        with Path(value.journal).open("a") as output:
            output.write(context.run_id + "\n")
    if value.fail:
        raise ValueError("requested child failure")
    if value.document is not None:
        assert value.document.read_bytes() == b"native call"
    if value.workspace is not None:
        assert (value.workspace.path / "data.txt").read_text() == "native call"
    return CallOutput(id=value.id, document=value.document, workspace=value.workspace)


@ava.workflow(input=CallInput, output=CallOutput)
def echo_child():
    return echo_result(ava.input)


@ava.workflow(input=CallInput, output=CallOutput, key=("id",))
def keyed_echo_child():
    return echo_result(ava.input)


@ava.workflow(input=CallInput, output=CallOutput, key=("id",))
def slow_child():
    waiting = ava.sleep(timedelta(days=1))
    output = echo_result(ava.input)
    waiting >> output
    return output


@ava.step
def call_target(value: CallInput) -> str:
    return value.target


@ava.step
def next_call(value: CallInput) -> CallInput:
    return value.model_copy(update={"id": value.id + 1, "depth": value.depth - 1})


@ava.step
def has_child(value: CallInput) -> bool:
    return value.depth > 0


@ava.step
def large_child_text() -> str:
    return "x" * (3 * 1024 * 1024)


@ava.workflow
def large_child():
    return large_child_text()


@ava.workflow
def large_call_parent():
    return ava.call(large_child)


@ava.workflow(input=CallInput, output=CallOutput)
def call_parent():
    return ava.call(call_target(ava.input), ava.input)


@ava.workflow(input=CallInput, output=CallOutput)
def timeout_parent():
    return ava.call(slow_child, ava.input, timeout=0.2)


@ava.workflow(input=CallInput, output=CallOutput)
def machine_call_visit():
    called = ava.call(call_machine, next_call(ava.input), when=has_child(ava.input))
    result = echo_result(ava.input)
    called >> result
    return result


@ava.machine(input=CallInput, key=("id",))
def call_machine():
    done = ava.state(machine_call_visit, initial=True, final=True)
    return done


@ava.workflow(input=CallInput, output=CallOutput)
def call_flow_visit():
    return ava.call(echo_child, ava.input)


@ava.machine(input=CallInput, key=("id",))
def flow_call_machine():
    done = ava.state(call_flow_visit, initial=True, final=True)
    return done


@ava.machine(input=CallInput, key=("id",))
def failing_child_machine():
    done = ava.state(echo_result, initial=True, final=True)
    return done


@ava.workflow(input=Ticket)
def lifecycle_call_parent():
    return ava.call(approval, ava.input)


@ava.workflow(input=CallInput)
def self_wait_visit():
    return ava.call(self_wait_machine, ava.input)


@ava.machine(input=CallInput, key=("id",))
def self_wait_machine():
    done = ava.state(self_wait_visit, initial=True, final=True)
    return done


@ava.workflow(input=CallInput)
def cycle_a_visit():
    return ava.call(cycle_b, ava.input)


@ava.workflow(input=CallInput)
def cycle_b_visit():
    return ava.call(cycle_a, ava.input)


@ava.machine(input=CallInput, key=("id",))
def cycle_a():
    done = ava.state(cycle_a_visit, initial=True, final=True)
    return done


@ava.machine(input=CallInput, key=("id",))
def cycle_b():
    done = ava.state(cycle_b_visit, initial=True, final=True)
    return done
