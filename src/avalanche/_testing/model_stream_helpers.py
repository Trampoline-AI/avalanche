"""Importable functions and models for distributed ModelStream tests."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelStreamRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str


def append_passthrough_people(*, people):
    return people.append(
        [ModelStreamRow(id=2, name="second"), ModelStreamRow(id=1, name="first")]
    )


def append_rerun_people(*, people):
    return people.append(
        [ModelStreamRow(id=3, name="third"), ModelStreamRow(id=4, name="fourth")]
    )


def collect_model_pairs(people: list[ModelStreamRow]) -> list[tuple[int, str]]:
    return [(person.id, person.name) for person in people]


def collect_model_names(people: list[ModelStreamRow]) -> list[str]:
    return [person.name for person in people]


def return_model(person: ModelStreamRow) -> ModelStreamRow:
    return person
