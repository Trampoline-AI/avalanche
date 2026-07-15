"""Runnable local DAG example with fan-out and fan-in dependencies."""

from __future__ import annotations

import avalanche as ava


@ava.source
def load_documents() -> dict[str, object]:
    return {
        "documents": [
            {"doc_id": "guide", "tokens": 120, "priority": "high"},
            {"doc_id": "notes", "tokens": 80, "priority": "normal"},
            {"doc_id": "runbook", "tokens": 160, "priority": "high"},
        ]
    }


@ava.step
def chunk_documents(document_batch: dict[str, object]) -> dict[str, object]:
    chunks = []
    for doc in document_batch["documents"]:
        chunks.append(
            {
                "chunk_id": f"{doc['doc_id']}-chunk-1",
                "doc_id": doc["doc_id"],
                "tokens": doc["tokens"],
                "priority": doc["priority"],
            }
        )
    return {"chunks": chunks}


@ava.step
def validate_chunks(chunk_batch: dict[str, object]) -> dict[str, object]:
    chunks = chunk_batch["chunks"]
    return {
        "valid": all(int(chunk["tokens"]) > 0 for chunk in chunks),
        "checked": len(chunks),
    }


@ava.step
def build_search_index(chunk_batch: dict[str, object]) -> dict[str, object]:
    chunks = chunk_batch["chunks"]
    return {
        "indexed_ids": [chunk["chunk_id"] for chunk in chunks],
        "index_name": "local-example-index",
    }


@ava.step
def summarize_corpus(chunk_batch: dict[str, object]) -> dict[str, object]:
    chunks = chunk_batch["chunks"]
    high_priority = [chunk for chunk in chunks if chunk["priority"] == "high"]
    return {"chunks": len(chunks), "high_priority": len(high_priority)}


@ava.dest
def publish_results(
    validation: dict[str, object],
    search_index: dict[str, object],
    summary: dict[str, object],
) -> dict[str, object]:
    return {
        "validated": validation["valid"],
        "indexed": len(search_index["indexed_ids"]),
        "summary": summary,
    }


@ava.workflow
def complex_dag_workflow():
    # Equivalent NodeFuture argument form, less visual:
    # docs = load_documents()
    # chunks = chunk_documents(docs)
    # validation = validate_chunks(chunks)
    # search_index = build_search_index(chunks)
    # summary = summarize_corpus(chunks)
    # published = publish_results(validation, search_index, summary)
    return (
        load_documents()
        >> chunk_documents()
        >> (validate_chunks() & build_search_index() & summarize_corpus())
        >> publish_results()
    )


def _main() -> None:
    result = complex_dag_workflow().run(executor=ava.LocalExecutor()).result()
    print("Complex DAG example complete")
    print(result)


if __name__ == "__main__":
    _main()
