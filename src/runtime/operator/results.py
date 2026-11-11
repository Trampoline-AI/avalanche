"""Operator result transport; codec and result model are shared with all hosts."""

from avalanche.results import (  # noqa: F401
    MAX_ATTACHMENT_MEDIA_TYPE_LENGTH,
    MAX_ATTACHMENT_NAME_LENGTH,
    MAX_RESULT_ATTACHMENT_BYTES,
    MAX_RESULT_ATTACHMENTS,
    MAX_RESULT_ATTACHMENTS_BYTES,
    MAX_RESULT_NESTING_DEPTH,
    MAX_RESULT_TOTAL_BYTES,
    MAX_RESULT_VALUE_JSON_BYTES,
    MAX_RESULT_VALUE_NODES,
    EncodedWorkflowResult,
    ResultFileAttachment,
    RunResult,
    decode_workflow_result,
    encode_workflow_result,
    strict_json_loads,
    validate_workflow_result_document,
)
