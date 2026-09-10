"""Successful non-value outcomes shared by workers, storage, and clients."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    SerializationInfo,
    TypeAdapter,
    model_serializer,
)

if TYPE_CHECKING:
    from .storage import Table

_METADATA = TypeAdapter(dict[str, JsonValue] | None)
_SKIP_SERIALIZER_CONTEXT_KEY = "__avalanche_operator_skipped_serializer__"


def _validate_metadata(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _validate_metadata(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate_metadata(item)
        return
    raise TypeError("skip metadata must contain only finite JSON values with string keys")


@dataclass(frozen=True, init=False)
class Skipped:
    """An intentional absence of a node value, not an error or ``None``.

    Python value edges retain this outcome (including fan-in positions). Consumers
    can inspect it with ``isinstance(value, ava.Skipped)``. Metadata is copied on
    access so an outcome cannot change after publication.
    """

    reason: str
    _metadata_json: str

    def __init__(self, reason: str, metadata: dict[str, JsonValue] | None = None) -> None:
        if type(reason) is not str or not reason.strip():
            raise ValueError("skip reason must be a non-empty string")
        if len(reason.encode("utf-8")) > 4096:
            raise ValueError("skip reason exceeds 4096 bytes")
        if metadata is not None and type(metadata) is not dict:
            raise TypeError("skip metadata must be a JSON object or None")
        _validate_metadata(metadata)
        encoded = json.dumps(metadata, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 16384:
            raise ValueError("skip metadata exceeds 16384 bytes")
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "_metadata_json", encoded)

    @property
    def metadata(self) -> dict[str, JsonValue] | None:
        return _METADATA.validate_json(self._metadata_json)

    @model_serializer
    def _serialize(self, info: SerializationInfo) -> dict[str, JsonValue]:
        context = info.context
        serializer = (
            context.get(_SKIP_SERIALIZER_CONTEXT_KEY) if isinstance(context, dict) else None
        )
        if callable(serializer):
            return serializer(self)
        return {"reason": self.reason, "metadata": self.metadata}


def skip(reason: str, metadata: dict[str, JsonValue] | None = None) -> Skipped:
    """Return a successful skipped outcome without creating a payload value."""
    return Skipped(reason, metadata)


class _ExpandedSkip(tuple[object, ...]):
    """Internal multi-return slots expanded from one authored node absence."""


def _expand_skip(value: object, *, num_returns: int) -> object:
    if num_returns > 1 and isinstance(value, Skipped):
        return _ExpandedSkip((value,) * num_returns)
    return value


def _skip_outcome(value: object) -> Skipped | None:
    """Read a whole-node outcome, including expanded multi-return transport."""
    from .types import LineagedResult

    if isinstance(value, LineagedResult):
        return value.value if isinstance(value.value, Skipped) else None
    if isinstance(value, Skipped):
        return value
    if isinstance(value, _ExpandedSkip):
        return _skip_outcome(value[0])
    return None


class _SkipReceipt(BaseModel):
    """Durable empty producer version, decoded before traversing rerun ancestry."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str
    node_slug: str
    reason: str
    metadata: dict[str, JsonValue] | None


def _skip_property_key(run_id: str, node_slug: str) -> str:
    identity = json.dumps([run_id, node_slug], separators=(",", ":"))
    return "avalanche.skip." + hashlib.sha256(identity.encode()).hexdigest()


def _persist_skip(table: Table, outcome: Skipped) -> Skipped:
    """Record an empty producer version without appending payload rows."""
    from .runtime import get_current_run_context

    context = get_current_run_context()
    if context is None or context.node_slug is None:
        raise RuntimeError("persisting skip requires an active workflow node context")
    document = _SkipReceipt(
        run_id=context.run_id,
        node_slug=context.node_slug,
        reason=outcome.reason,
        metadata=outcome.metadata,
    ).model_dump_json()
    properties = {_skip_property_key(context.run_id, context.node_slug): document}
    if context.rerun is not None and context.rerun.run_id != context.run_id:
        from .runtime.providers.stream import _rerun_edge_property_key

        properties[_rerun_edge_property_key(context.run_id)] = context.rerun.run_id
    with table.transaction() as tx:
        tx.set_properties(**properties)
    return outcome
