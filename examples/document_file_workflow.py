"""Document input/output example for the operator CLI."""

from typing import Any, cast

from pydantic import BaseModel

import avalanche as ava


class DocumentInput(ava.BaseInput):
    value: int
    document: ava.File


class DocumentReport(BaseModel):
    summary: str
    files: list[ava.File]


@ava.dest
def build_document_report(payload: DocumentInput) -> DocumentReport:
    """Build a report and processed copy of the input document."""
    text = payload.document.read_bytes().decode("utf-8")
    content = f"value={payload.value}\n{text}".encode()
    return DocumentReport(
        summary=f"Processed {payload.document.name or 'document'}",
        files=[
            ava.File(
                name="processed-document.txt",
                content=content,
                content_type="text/plain",
            )
        ],
    )


@ava.workflow(input=DocumentInput)
def document_file_workflow():
    return build_document_report()


if __name__ == "__main__":
    workflow = cast(Any, document_file_workflow)()
    result = workflow.run(
        executor=ava.LocalExecutor(),
        input={
            "value": 41,
            "document": ava.File(name="doc.txt", content=b"hello"),
        },
    ).result()
    print(result)
