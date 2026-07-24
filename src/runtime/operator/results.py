"""Strict workflow result encoding for operator storage and gRPC transport."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from avalanche.runtime import File
from avalanche.runtime.context import _FILE_SERIALIZER_CONTEXT_KEY

_RESULT_FORMAT_VERSION = 1
MAX_RESULT_ATTACHMENTS = 1024
MAX_RESULT_VALUE_JSON_BYTES = 4 * 1024 * 1024
MAX_RESULT_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_RESULT_ATTACHMENTS_BYTES = 32 * 1024 * 1024
MAX_RESULT_TOTAL_BYTES = 32 * 1024 * 1024
MAX_RESULT_NESTING_DEPTH = 100
MAX_RESULT_VALUE_NODES = 100_000
MAX_ATTACHMENT_NAME_LENGTH = 1024
MAX_ATTACHMENT_MEDIA_TYPE_LENGTH = 255
_ATTACHMENT_ID = re.compile(r"file_(0|[1-9][0-9]{0,3})")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ResultFileAttachment:
    """One file separated from the JSON-shaped result value."""

    attachment_id: str
    content: bytes
    name: str | None
    media_type: str | None
    sha256: str


@dataclass(frozen=True)
class EncodedWorkflowResult:
    """Transport-ready workflow result with file bytes out of band."""

    value_json: str
    files: tuple[ResultFileAttachment, ...] = ()


def encode_workflow_result(value: Any) -> EncodedWorkflowResult:
    """Encode a supported result while extracting every native ``File`` value."""
    attachments: list[ResultFileAttachment] = []
    marker_token = uuid4().hex

    def serialize_file(file: File) -> dict[str, str]:
        attachment_id = _append_attachment(file, attachments)
        return {
            "__operator_file_marker__": marker_token,
            "attachment_id": attachment_id,
        }

    if isinstance(value, BaseModel) and not isinstance(value, File):
        value = value.model_dump(
            mode="json",
            by_alias=True,
            context={_FILE_SERIALIZER_CONTEXT_KEY: serialize_file},
        )
    encoded = _encode_value(
        value,
        attachments,
        marker_token,
        serialize_file,
        _DecodeBudget(),
    )
    value_json = json.dumps(
        {"version": _RESULT_FORMAT_VERSION, "value": encoded},
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    value_size = len(value_json.encode("utf-8"))
    if value_size > MAX_RESULT_VALUE_JSON_BYTES:
        raise ValueError(f"Workflow result JSON exceeds {MAX_RESULT_VALUE_JSON_BYTES} bytes")
    total_attachment_bytes = sum(len(item.content) for item in attachments)
    if value_size + total_attachment_bytes > MAX_RESULT_TOTAL_BYTES:
        raise ValueError(f"Workflow result exceeds {MAX_RESULT_TOTAL_BYTES} bytes")
    return EncodedWorkflowResult(value_json=value_json, files=tuple(attachments))


def decode_workflow_result(payload: EncodedWorkflowResult) -> Any:
    """Strictly decode a result and reconstruct public ``File`` values."""
    if type(payload) is not EncodedWorkflowResult:
        raise ValueError("Malformed encoded workflow result")
    value_size = _encoded_json_size(payload.value_json)
    if type(payload.files) is not tuple:
        raise ValueError("Workflow result attachments must be a tuple")
    if len(payload.files) > MAX_RESULT_ATTACHMENTS:
        raise ValueError(f"Workflow result exceeds {MAX_RESULT_ATTACHMENTS} file attachments")

    attachment_contents: dict[str, File] = {}
    attachment_ids: set[str] = set()
    total_attachment_bytes = 0
    for item in payload.files:
        _validate_attachment_shape(item)
        if item.attachment_id in attachment_ids:
            raise ValueError(f"Duplicate result file attachment {item.attachment_id!r}")
        attachment_ids.add(item.attachment_id)
        total_attachment_bytes += len(item.content)
        if total_attachment_bytes > MAX_RESULT_ATTACHMENTS_BYTES:
            raise ValueError(
                "Workflow result file attachments exceed "
                f"{MAX_RESULT_ATTACHMENTS_BYTES} bytes"
            )

    if value_size + total_attachment_bytes > MAX_RESULT_TOTAL_BYTES:
        raise ValueError(f"Workflow result exceeds {MAX_RESULT_TOTAL_BYTES} bytes")

    for item in payload.files:
        _validate_attachment_digest(item)
        attachment_contents[item.attachment_id] = File(
            name=item.name,
            content=item.content,
            content_type=item.media_type,
            sha256=item.sha256,
        )

    document = _load_result_document(payload.value_json)
    used_attachments: set[str] = set()
    value = _decode_value(
        document["value"],
        attachment_contents,
        used_attachments,
        _DecodeBudget(),
    )
    _reject_unused_attachments(attachment_ids, used_attachments)
    return value


def validate_workflow_result_document(
    value_json: str,
    attachment_ids: set[str],
) -> None:
    """Validate a result document without loading attachment content."""
    if type(attachment_ids) is not set:
        raise ValueError("Workflow result attachment IDs must be a set")
    if len(attachment_ids) > MAX_RESULT_ATTACHMENTS:
        raise ValueError(f"Workflow result exceeds {MAX_RESULT_ATTACHMENTS} file attachments")
    for attachment_id in attachment_ids:
        _validate_attachment_id(attachment_id)
    document = _load_result_document(value_json)
    used_attachments: set[str] = set()
    _decode_value(document["value"], attachment_ids, used_attachments, _DecodeBudget())
    _reject_unused_attachments(attachment_ids, used_attachments)


def _append_attachment(
    file: File,
    attachments: list[ResultFileAttachment],
) -> str:
    if len(attachments) >= MAX_RESULT_ATTACHMENTS:
        raise ValueError(f"Workflow result exceeds {MAX_RESULT_ATTACHMENTS} file attachments")
    _validate_optional_metadata_string(
        file.name,
        "Result file attachment name",
        MAX_ATTACHMENT_NAME_LENGTH,
    )
    _validate_optional_metadata_string(
        file.content_type,
        "Result file attachment media type",
        MAX_ATTACHMENT_MEDIA_TYPE_LENGTH,
    )
    if type(file.content) is not bytes:
        raise ValueError("Result file attachment content must be bytes")
    if len(file.content) > MAX_RESULT_ATTACHMENT_BYTES:
        raise ValueError(f"Result file attachment exceeds {MAX_RESULT_ATTACHMENT_BYTES} bytes")
    total_attachment_bytes = sum(len(item.content) for item in attachments) + len(file.content)
    if total_attachment_bytes > MAX_RESULT_ATTACHMENTS_BYTES:
        raise ValueError(
            "Workflow result file attachments exceed " f"{MAX_RESULT_ATTACHMENTS_BYTES} bytes"
        )
    digest = hashlib.sha256(file.content).hexdigest()
    if file.sha256 is not None and file.sha256 != digest:
        raise ValueError("Result file digest does not match its content")
    attachment_id = f"file_{len(attachments)}"
    attachments.append(
        ResultFileAttachment(
            attachment_id=attachment_id,
            content=file.content,
            name=file.name,
            media_type=file.content_type,
            sha256=digest,
        )
    )
    return attachment_id


def _encode_value(
    value: Any,
    attachments: list[ResultFileAttachment],
    marker_token: str,
    serialize_file: Any,
    budget: _DecodeBudget,
    depth: int = 0,
) -> dict[str, Any]:
    if depth > MAX_RESULT_NESTING_DEPTH:
        raise ValueError(f"Workflow result nesting exceeds {MAX_RESULT_NESTING_DEPTH} levels")
    budget.nodes += 1
    if budget.nodes > MAX_RESULT_VALUE_NODES:
        raise ValueError(f"Workflow result exceeds {MAX_RESULT_VALUE_NODES} encoded values")
    if isinstance(value, File):
        return {
            "kind": "file",
            "attachment_id": _append_attachment(value, attachments),
        }
    if (
        type(value) is dict
        and value.get("__operator_file_marker__") == marker_token
        and set(value) == {"__operator_file_marker__", "attachment_id"}
    ):
        attachment_id = value["attachment_id"]
        if type(attachment_id) is not str:
            raise TypeError("Pydantic File serializer returned an invalid marker")
        return {"kind": "file", "attachment_id": attachment_id}
    if value is None or type(value) in {bool, int, str}:
        return {"kind": "scalar", "value": value}
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Workflow result floats must be finite")
        return {"kind": "scalar", "value": value}
    if isinstance(value, BaseModel):
        dumped = value.model_dump(
            mode="json",
            by_alias=True,
            context={_FILE_SERIALIZER_CONTEXT_KEY: serialize_file},
        )
        return _encode_value(
            dumped,
            attachments,
            marker_token,
            serialize_file,
            budget,
            depth,
        )
    if type(value) is tuple:
        return {
            "kind": "tuple",
            "items": [
                _encode_value(
                    item,
                    attachments,
                    marker_token,
                    serialize_file,
                    budget,
                    depth + 1,
                )
                for item in value
            ],
        }
    if type(value) is list:
        return {
            "kind": "list",
            "items": [
                _encode_value(
                    item,
                    attachments,
                    marker_token,
                    serialize_file,
                    budget,
                    depth + 1,
                )
                for item in value
            ],
        }
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("Workflow result dictionaries must use string keys")
        return {
            "kind": "dict",
            "items": [
                [
                    key,
                    _encode_value(
                        item,
                        attachments,
                        marker_token,
                        serialize_file,
                        budget,
                        depth + 1,
                    ),
                ]
                for key, item in value.items()
            ],
        }
    raise TypeError(
        "Operator workflow results support JSON scalar/container values, "
        "Pydantic models, and avalanche.File values"
    )


def _load_result_document(value_json: str) -> dict[str, Any]:
    _encoded_json_size(value_json)
    try:
        document = strict_json_loads(value_json)
    except RecursionError as exc:
        raise ValueError(
            f"Workflow result nesting exceeds {MAX_RESULT_NESTING_DEPTH} levels"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid workflow result JSON: {exc.msg}") from exc
    if type(document) is not dict or set(document) != {"version", "value"}:
        raise ValueError("Malformed workflow result envelope")
    version = document["version"]
    if type(version) is not int or version != _RESULT_FORMAT_VERSION:
        raise ValueError("Unsupported workflow result format version")
    return document


def _validate_attachment_shape(item: ResultFileAttachment) -> None:
    if type(item) is not ResultFileAttachment:
        raise ValueError("Malformed result file attachment")
    _validate_attachment_id(item.attachment_id)
    if type(item.content) is not bytes:
        raise ValueError("Result file attachment content must be bytes")
    if len(item.content) > MAX_RESULT_ATTACHMENT_BYTES:
        raise ValueError(f"Result file attachment exceeds {MAX_RESULT_ATTACHMENT_BYTES} bytes")
    _validate_optional_metadata_string(
        item.name,
        "Result file attachment name",
        MAX_ATTACHMENT_NAME_LENGTH,
    )
    _validate_optional_metadata_string(
        item.media_type,
        "Result file attachment media type",
        MAX_ATTACHMENT_MEDIA_TYPE_LENGTH,
    )
    if type(item.sha256) is not str or _SHA256.fullmatch(item.sha256) is None:
        raise ValueError("Result file attachment sha256 must be lowercase hexadecimal")


def _validate_attachment_digest(item: ResultFileAttachment) -> None:
    if hashlib.sha256(item.content).hexdigest() != item.sha256:
        raise ValueError(
            f"File attachment {item.attachment_id!r} sha256 does not match content"
        )


def _validate_attachment_id(attachment_id: Any) -> None:
    if type(attachment_id) is not str or _ATTACHMENT_ID.fullmatch(attachment_id) is None:
        raise ValueError("Result file attachment ID is malformed")
    if int(attachment_id[5:]) >= MAX_RESULT_ATTACHMENTS:
        raise ValueError("Result file attachment ID is malformed")


def _validate_optional_metadata_string(
    value: Any,
    label: str,
    maximum_length: int,
) -> None:
    if value is not None and type(value) is not str:
        raise ValueError(f"{label} must be a string or null")
    if value is not None and len(value) > maximum_length:
        raise ValueError(f"{label} exceeds {maximum_length} characters")


def strict_json_loads(payload: str | bytes) -> Any:
    """Decode JSON while rejecting duplicate object keys at every depth."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON object key {key!r}")
            result[key] = value
        return result

    return json.loads(
        payload,
        object_pairs_hook=reject_duplicates,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"Invalid JSON constant {value}")
        ),
    )


def _decode_value(
    encoded: Any,
    attachments: dict[str, File] | set[str],
    used_attachments: set[str],
    budget: _DecodeBudget,
    depth: int = 0,
) -> Any:
    if depth > MAX_RESULT_NESTING_DEPTH:
        raise ValueError(f"Workflow result nesting exceeds {MAX_RESULT_NESTING_DEPTH} levels")
    budget.nodes += 1
    if budget.nodes > MAX_RESULT_VALUE_NODES:
        raise ValueError(f"Workflow result exceeds {MAX_RESULT_VALUE_NODES} encoded values")
    if type(encoded) is not dict or type(encoded.get("kind")) is not str:
        raise ValueError("Malformed workflow result value")
    kind = encoded["kind"]
    if kind == "scalar":
        if set(encoded) != {"kind", "value"}:
            raise ValueError("Malformed scalar workflow result")
        value = encoded["value"]
        if value is not None and type(value) not in {bool, int, float, str}:
            raise ValueError("Malformed scalar workflow result")
        if type(value) is float and not math.isfinite(value):
            raise ValueError("Workflow result contains a non-finite number")
        return value
    if kind in {"tuple", "list"}:
        if set(encoded) != {"kind", "items"} or type(encoded["items"]) is not list:
            raise ValueError(f"Malformed {kind} workflow result")
        values = [
            _decode_value(item, attachments, used_attachments, budget, depth + 1)
            for item in encoded["items"]
        ]
        return tuple(values) if kind == "tuple" else values
    if kind == "dict":
        if set(encoded) != {"kind", "items"} or type(encoded["items"]) is not list:
            raise ValueError("Malformed dict workflow result")
        result: dict[str, Any] = {}
        for item in encoded["items"]:
            if (
                type(item) is not list
                or len(item) != 2
                or type(item[0]) is not str
                or item[0] in result
            ):
                raise ValueError("Malformed dict workflow result item")
            result[item[0]] = _decode_value(
                item[1],
                attachments,
                used_attachments,
                budget,
                depth + 1,
            )
        return result
    if kind == "file":
        if set(encoded) != {"kind", "attachment_id"}:
            raise ValueError("Malformed file workflow result")
        budget.attachment_references += 1
        if budget.attachment_references > MAX_RESULT_ATTACHMENTS:
            raise ValueError(
                f"Workflow result exceeds {MAX_RESULT_ATTACHMENTS} file attachment references"
            )
        attachment_id = encoded["attachment_id"]
        _validate_attachment_id(attachment_id)
        if attachment_id not in attachments:
            raise ValueError("Workflow result references a missing file attachment")
        if attachment_id in used_attachments:
            raise ValueError(
                f"Duplicate workflow result file attachment reference {attachment_id!r}"
            )
        used_attachments.add(attachment_id)
        if isinstance(attachments, dict):
            return attachments[attachment_id]
        return None
    raise ValueError(f"Unknown workflow result kind {kind!r}")


def _reject_unused_attachments(
    attachment_ids: set[str],
    used_attachments: set[str],
) -> None:
    unused = attachment_ids - used_attachments
    if unused:
        names = ", ".join(sorted(unused))
        raise ValueError(f"Unreferenced result file attachment(s): {names}")


@dataclass
class _DecodeBudget:
    nodes: int = 0
    attachment_references: int = 0


def _encoded_json_size(value_json: Any) -> int:
    if type(value_json) is not str:
        raise ValueError("Workflow result JSON must be a string")
    size = len(value_json.encode("utf-8"))
    if size > MAX_RESULT_VALUE_JSON_BYTES:
        raise ValueError(f"Workflow result JSON exceeds {MAX_RESULT_VALUE_JSON_BYTES} bytes")
    return size
