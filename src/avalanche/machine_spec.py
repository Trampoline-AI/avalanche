"""Lightweight authoring mirror of delta_api_spec.manifests machine contracts.

Like WaitSpec, these descriptors contain JSON only, never live Python work.
Delta validates the exported document at its manifest admission boundary.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .machine_host import MachineOpSpec
from .mapping import MapSpec
from .policies import StepPolicy, WaitSpec
from .run_steps import RunOpSpec


class MachineTransitionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event: str = Field(min_length=1)
    target: str
    payload_schema: dict[str, JsonValue] | None = None


class MachineTimerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    delay_seconds: float = Field(ge=0, allow_inf_nan=False)
    target: str


class MachineStateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    work: str
    initial: bool = False
    final: bool = False
    defer: tuple[str, ...] = ()
    transitions: tuple[MachineTransitionSpec, ...] = ()
    timers: tuple[MachineTimerSpec, ...] = ()


class MachineMapSpec(MapSpec):
    sources: tuple[str | int, ...] = ()


class MachineWorkSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_ids: tuple[str, ...] = Field(max_length=64)
    graph: dict[str, tuple[str, ...]]
    node_slugs: dict[str, str]
    node_types: dict[str, str]
    node_policies: dict[str, StepPolicy]
    node_waits: dict[str, WaitSpec] = Field(default_factory=dict)
    node_machines: dict[str, MachineOpSpec] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    node_runs: dict[str, RunOpSpec] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    map_specs: dict[str, MachineMapSpec] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    node_emits: dict[str, tuple[str, ...]]
    node_output_schemas: dict[str, dict[str, object]] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    emits: tuple[str, ...] = ()
    output_node_id: str | None = None
    output_schema: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def validate_work(self) -> MachineWorkSpec:
        positions = {name: index for index, name in enumerate(self.node_ids)}
        if len(positions) != len(self.node_ids):
            raise ValueError("duplicate work node IDs")
        nodes = set(positions)
        if not set(self.node_output_schemas).issubset(nodes):
            raise ValueError("node_output_schemas keys must be a subset of node_ids")
        for metadata in (self.node_slugs, self.node_types, self.node_policies, self.node_emits):
            if set(metadata) != nodes:
                raise ValueError("work node metadata must cover exactly node_ids")
        if not set(self.map_specs) <= nodes:
            raise ValueError("unknown work map node")
        expanded = (
            len(nodes)
            + sum(spec.max_items for spec in self.map_specs.values())
            + sum(wait.map_max_items or 0 for wait in self.node_waits.values())
        )
        if expanded > 64:
            raise ValueError("state visit work exceeds 64 nodes including mapped children")
        if not set(self.node_runs) <= nodes:
            raise ValueError("unknown work run node")
        if not set(self.node_machines) <= nodes:
            raise ValueError("unknown work machine node")
        if not set(self.node_waits) <= nodes:
            raise ValueError("unknown work wait node")
        for parent, children in self.graph.items():
            if parent not in nodes or not set(children) <= nodes:
                raise ValueError("unknown work graph node")
            if any(positions[parent] >= positions[child] for child in children):
                raise ValueError("work node_ids must be topological (no cycles)")
        if self.output_node_id is not None and self.output_node_id not in nodes:
            raise ValueError("unknown work output node")
        expected = (
            self.node_emits[self.output_node_id] if self.output_node_id is not None else ()
        )
        if self.emits != expected:
            raise ValueError("work emits must match its returned node emits")
        return self


class MachineSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["avalanche.machine/v1"] = "avalanche.machine/v1"
    name: str
    input_schema: dict[str, JsonValue]
    key_fields: tuple[str, ...] = Field(min_length=1)
    key_declarations: dict[str, JsonValue] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    initial: str
    states: dict[str, MachineStateSpec]
    work_graphs: dict[str, MachineWorkSpec]

    @model_validator(mode="after")
    def validate_machine(self) -> MachineSpec:
        initials = [name for name, state in self.states.items() if state.initial]
        if initials != [self.initial]:
            raise ValueError("machine requires exactly one initial state")
        properties = self.input_schema.get("properties", {})
        if not isinstance(properties, dict) or not set(self.key_fields) <= properties.keys():
            raise ValueError("machine key fields must be declared input fields")
        if len(set(self.key_fields)) != len(self.key_fields):
            raise ValueError("duplicate machine key fields")
        for name, state in self.states.items():
            if state.work not in self.work_graphs:
                raise ValueError(f"unknown work graph for state {name}")
            if state.final and (state.transitions or state.timers):
                raise ValueError(f"final state {name} has outgoing transitions")
            events = [transition.event for transition in state.transitions]
            if len(events) != len(set(events)):
                raise ValueError(f"duplicate event transition in state {name}")
            for edge in (*state.transitions, *state.timers):
                if edge.target not in self.states:
                    raise ValueError(f"unknown target state {edge.target}")
        return self
