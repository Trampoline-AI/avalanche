"""Evaluation snapshots retained independently from structural workflow state."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from avalanche.evaluations import EvaluationResult


class EvaluationRecord(BaseModel):
    """One evaluated node execution, including results arriving after workflow exit."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    evaluation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    status: Literal["pending", "completed", "failed"]
    created_at: float
    ended_at: float | None = None
    result: EvaluationResult | None = None
    error: str | None = None

    @model_validator(mode="after")
    def valid_lifecycle(self) -> Self:
        if self.status == "pending":
            if self.ended_at is not None or self.result is not None or self.error is not None:
                raise ValueError("pending evaluations cannot contain terminal evidence")
        else:
            if self.ended_at is None or self.ended_at < self.created_at:
                raise ValueError("terminal evaluations require an end time after creation")
            if self.status == "completed":
                if self.result is None or self.error is not None:
                    raise ValueError("completed evaluations require a result and no error")
            elif self.result is not None or not self.error:
                raise ValueError("failed evaluations require an error and no result")
        return self
