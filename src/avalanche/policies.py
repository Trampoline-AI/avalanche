"""Versioned, provider-neutral contracts pinned at workflow admission.

This is metadata, not an external-effect exactly-once guarantee. Durable hosts
must commit an attempt-start fence for hold/fail before entering user code.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

UncertaintyPolicy = Literal["retry", "hold", "fail"]
MAX_GENERATION = 8


class NonRetryableError(Exception):
    """A known terminal failure; remaining automatic retries are skipped."""

    retryable = False
    outcome_unknown = False


class OutcomeUnknownError(Exception):
    """An external operation may have completed; apply on_uncertain policy."""

    outcome_unknown = True


class DecisionTimeoutError(NonRetryableError):
    """A decision expired without a declared typed timeout outcome."""

    code = "decision_timeout"


class StepPolicy(BaseModel):
    """Extra attempts, bounded by the host's hard generation ceiling."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    retries: int = Field(default=MAX_GENERATION - 1, ge=0, strict=True)
    on_uncertain: UncertaintyPolicy = "retry"

    @property
    def effective_retries(self) -> int:
        return min(self.retries, MAX_GENERATION - 1)


class Outcome(BaseModel):
    """Base for author-defined typed decisions. Audit identity lives outside it."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class WaitSpec(BaseModel):
    """Serializable admission contract. Deadline is computed once when armed.

    ``schema_`` is exported as ``schema`` on the wire. Models/classes are kept
    only in the live authoring node, never in discovery or cached descriptors.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def validate_json_contract(self) -> WaitSpec:
        json.dumps(self.model_dump(mode="json"), allow_nan=False)
        if self.until is not None and self.until.utcoffset() is None:
            raise ValueError("until must be timezone-aware")
        return self

    kind: Literal["sleep", "event", "decision"]
    timeout_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    until: datetime | None = None
    key: str | None = None
    schema_: dict[str, JsonValue] = Field(default_factory=dict, alias="schema")
    prompt: str | None = None
    show_node_id: str | None = None
    show_index: int | None = None
    show: JsonValue = None
    on_timeout: dict[str, JsonValue] | None = None
    assignees: tuple[str, ...] = ()
    approvers: tuple[str, ...] | None = None
    map_items: list[JsonValue] | None = None
    map_source_node_id: str | None = None
    map_source_index: int | None = None
    map_max_items: int | None = Field(default=None, ge=0, le=64, strict=True)
