"""Clock-neutral machine transition and inbox policy, shared by hosts and tests.

The caller owns execution, clocks, payload validation and persistence. A reducer
never executes work or mutates an inbox. Timers are supplied as entry deadlines.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from .machine_spec import MachineSpec
from .policies import Event, NonRetryableError

MachineValue = BaseModel | JsonValue


class InboxFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event: str
    kind: str


@dataclass(frozen=True)
class BufferedEvent:
    name: str
    payload: MachineValue
    size: int


@dataclass(frozen=True)
class MachineRecord:
    state: str
    payload: MachineValue = None
    internal_consumed: bool = False


@dataclass(frozen=True)
class MachineTransition:
    target: str
    event: str | None
    source: Literal["internal", "external", "timer"]
    payload: MachineValue


@dataclass(frozen=True)
class MachineReduction:
    record: MachineRecord
    finished: bool = False
    transition: MachineTransition | None = None
    consumed_index: int | None = None


def retain_inbox(
    inbox: Sequence[BufferedEvent],
    accepts: Collection[str],
    defer: Collection[str],
) -> tuple[tuple[BufferedEvent, ...], tuple[InboxFact, ...]]:
    retained = tuple(event for event in inbox if event.name in accepts or event.name in defer)
    dropped = tuple(
        InboxFact(event=event.name, kind="event_dropped")
        for event in inbox
        if event.name not in accepts and event.name not in defer
    )
    return retained, dropped


def consumable_index(inbox: Sequence[BufferedEvent], accepts: Collection[str]) -> int | None:
    return next((index for index, event in enumerate(inbox) if event.name in accepts), None)


def reduce_machine(
    spec: MachineSpec,
    record: MachineRecord,
    inbox: Sequence[BufferedEvent],
    timers: Sequence[tuple[float, str]],
    outcome: MachineValue,
    now: float,
    *,
    work_done: bool,
) -> MachineReduction:
    """Select final, earliest expired timer, internal, then first external event.

    No transition interrupts unfinished state entry work. Equal timer deadlines retain
    declaration order. None payloads preserve the previous transition payload.
    """
    if not work_done:
        return MachineReduction(record)
    declaration = spec.states[record.state]
    if declaration.final:
        return MachineReduction(record, finished=True)
    expired = [(due, target) for due, target in timers if due <= now]
    transition = None
    index = None
    consumed = record.internal_consumed
    if expired:
        _, target = min(expired, key=lambda timer: timer[0])
        transition = MachineTransition(target, None, "timer", None)
    else:
        if not consumed:
            consumed = True
            if isinstance(outcome, Event) and outcome.name is not None:
                if outcome.name not in spec.work_graphs[declaration.work].emits:
                    raise NonRetryableError(f"undeclared workflow event {outcome.name!r}")
                edge = next(
                    (edge for edge in declaration.transitions if edge.event == outcome.name),
                    None,
                )
                if edge is not None:
                    transition = MachineTransition(
                        edge.target, outcome.name, "internal", outcome.output
                    )
        if transition is None:
            accepts = {edge.event: edge.target for edge in declaration.transitions}
            index = consumable_index(inbox, accepts)
            if index is not None:
                event = inbox[index]
                transition = MachineTransition(
                    accepts[event.name], event.name, "external", event.payload
                )
    if transition is None:
        return MachineReduction(MachineRecord(record.state, record.payload, consumed))
    payload = transition.payload if transition.payload is not None else record.payload
    return MachineReduction(
        MachineRecord(transition.target, payload),
        transition=transition,
        consumed_index=index,
    )
