"""Step schemas describe graph values, not agent calls or runtime dependencies."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_serializer

import avalanche as ava

pair_step = ava.step(num_returns=2)


def test_local_model_interfaces_survive_all_decorators_and_execute_unchanged():
    class Ticket(BaseModel):
        subject: str = Field(min_length=1)
        priority: int

        @field_serializer("priority")
        def serialize_priority(self, value: int) -> str:
            return str(value)

    class Batch(BaseModel):
        tickets: list[Ticket]

    @ava.source
    def load() -> Batch:
        return Batch(tickets=[Ticket(subject="Review", priority=2)])

    @pair_step
    def split(batch: Batch, *, limit: int = 1, logger=ava.Logger()) -> tuple[list[Ticket], int]:
        return batch.tickets[:limit], len(batch.tickets)

    @ava.agent_step(ava.Signature("prompt: str -> answer: str"))
    async def select(tickets: list[Ticket], *, agent: ava.Agent) -> Ticket:
        # No model call: the outer step need not share the agent's call signature.
        return tickets[0]

    @ava.dest()
    def save(ticket: Ticket, count: int) -> str:
        return f"{ticket.subject}:{ticket.priority}:{count}"

    @ava.workflow
    def flow():
        split_result = split(load())
        return save(select(split_result[0]), split_result[1])

    assert load.step_interface.step_inputs == []
    batch_schema = load.step_interface.step_output.json_schema
    assert batch_schema["properties"]["tickets"]["items"] == {"$ref": "#/$defs/Ticket"}
    assert batch_schema["$defs"]["Ticket"]["properties"]["priority"]["type"] == "string"

    batch_input, limit_input = split.step_interface.step_inputs
    assert batch_input.name == "batch"
    assert batch_input.required is True
    assert batch_input.json_schema["$defs"]["Ticket"]["properties"]["priority"]["type"] == (
        "integer"
    )
    assert limit_input.name == "limit"
    assert limit_input.required is False
    assert split.step_interface.step_output.json_schema["prefixItems"][1] == {"type": "integer"}

    [agent_input] = select.step_interface.step_inputs
    assert agent_input.name == "tickets"
    assert agent_input.type_name == "list[Ticket]"
    assert agent_input.json_schema["items"] == {"$ref": "#/$defs/Ticket"}
    assert select.step_interface.step_output.type_name == "Ticket"
    assert select.step_interface.step_output.json_schema["properties"]["priority"]["type"] == (
        "string"
    )
    assert [field.name for field in save.step_interface.step_inputs] == ["ticket", "count"]
    assert save.step_interface.step_output.json_schema == {"type": "string"}
    assert flow().run(executor=ava.LocalExecutor()).result() == "Review:2:1"
