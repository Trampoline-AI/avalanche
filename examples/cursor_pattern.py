"""Runnable Cursor example for manual checkpoint control.

Use Cursor when a task needs to store custom checkpoint state, track a different
source table than its destination table, or coordinate multiple source tables.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import dataframely as dy
import polars as pl

import avalanche as ava


def example_root() -> Path:
    configured = os.environ.get("AVALANCHE_EXAMPLE_ROOT")
    root = (
        Path(configured)
        if configured
        else Path(".avalanche/examples/cursor_pattern") / str(os.getpid())
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


EXAMPLE_ROOT = example_root()


class DocumentSchema(dy.Schema):
    doc_id = dy.String(nullable=False)
    title = dy.String(nullable=False)
    body = dy.String(nullable=False)


class ChunkSchema(dy.Schema):
    chunk_id = dy.String(nullable=False)
    doc_id = dy.String(nullable=False)
    chunk_number = dy.Int32(nullable=False)
    text = dy.String(nullable=False)


class EmbeddingSchema(dy.Schema):
    chunk_id = dy.String(nullable=False)
    model = dy.String(nullable=False)
    vector = dy.Binary(nullable=False)


class ExampleNamespace(ava.IcebergNs):
    ns_config = ava.IcebergNsConfig(
        name="cursor_example",
        base_location=str(EXAMPLE_ROOT / "warehouse"),
    )

    document = ava.IcebergTable(schema=DocumentSchema)
    chunk = ava.IcebergTable(schema=ChunkSchema)
    embedding = ava.IcebergTable(schema=EmbeddingSchema)


ns = ExampleNamespace(
    catalog="cursor-example",
    load_catalog_props={"type": "sql", "uri": f"sqlite:///{EXAMPLE_ROOT / 'catalog.db'}"},
)


@ava.source
def load_documents(*, docs=ns.document):
    rows = pl.DataFrame(
        {
            "doc_id": ["doc-10", "doc-20"],
            "title": ["Manual cursor", "Coordination"],
            "body": [
                "Cursor stores checkpoints in table metadata.",
                "Cursor can coordinate more than one table snapshot.",
            ],
        }
    )
    return docs.append(rows)


@ava.step
def chunk_documents(
    _loaded: object = None,
    *,
    cursor=ava.Cursor(ns.chunk, key="document_snapshot"),
    source=ns.document,
    dest=ns.chunk,
) -> str:
    with cursor.tx() as tx:
        last_snapshot = cursor.get()
        doc_df = source.append_scan(start_snapshot_id=last_snapshot).to_polars()
        current_snapshot = source.current_snapshot().snapshot_id

        if doc_df.is_empty():
            cursor.set(current_snapshot)
            return "no documents to chunk"

        chunks = process_documents_to_chunks(doc_df)
        tx.append(chunks.to_arrow())
        cursor.set(current_snapshot)
        return f"chunked {len(doc_df)} documents into {len(chunks)} chunks"


@ava.step
def embed_chunks_per_model(
    _chunked: object = None,
    *,
    model: str = "local_demo",
    source=ns.chunk,
    dest=ns.embedding,
) -> str:
    key = f"embed_{model.replace('-', '_')}"
    cursor = ava.Cursor(dest, key=key)

    with cursor.tx() as tx:
        last_snapshot = cursor.get()
        chunk_df = source.append_scan(start_snapshot_id=last_snapshot).to_polars()
        current_snapshot = source.current_snapshot().snapshot_id

        if chunk_df.is_empty():
            cursor.set(current_snapshot)
            return f"no chunks for {model}"

        embeddings = generate_embeddings(chunk_df, model=model)
        tx.append(embeddings.to_arrow())
        cursor.set(current_snapshot)
        return f"embedded {len(chunk_df)} chunks with {model}"


@ava.dest
def sync_to_vector_db(
    _local_embeddings: object = None,
    _backup_embeddings: object = None,
    *,
    cursor=ava.Cursor(ns.embedding, key="vector_db_sync"),
    chunks=ns.chunk,
    embeddings=ns.embedding,
) -> str:
    with cursor.transaction():
        previous_state = cursor.get()
        current_state = [
            chunks.current_snapshot().snapshot_id,
            embeddings.current_snapshot().snapshot_id,
        ]

        if previous_state and json.loads(previous_state) == current_state:
            return "vector db already synced"

        chunk_count = len(chunks.read())
        embedding_count = len(embeddings.read())
        cursor.set(json.dumps(current_state))
        return f"synced {chunk_count} chunks and {embedding_count} embeddings"


@ava.workflow
def cursor_workflow():
    # Equivalent NodeFuture argument form, less visual:
    # load = load_documents()
    # chunk = chunk_documents(load)
    # embed_local = embed_chunks_per_model(chunk)
    # embed_backup = embed_chunks_per_model(chunk, model="backup_demo")
    # sync = sync_to_vector_db(embed_local, embed_backup)
    return (
        load_documents()
        >> chunk_documents()
        >> (embed_chunks_per_model() & embed_chunks_per_model(model="backup_demo"))
        >> sync_to_vector_db()
    )


def process_documents_to_chunks(doc_df: pl.DataFrame) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for doc in doc_df.iter_rows(named=True):
        rows.append(
            {
                "chunk_id": f"{doc['doc_id']}-chunk-1",
                "doc_id": doc["doc_id"],
                "chunk_number": 1,
                "text": doc["body"],
            }
        )
    return pl.DataFrame(rows)


def generate_embeddings(chunk_df: pl.DataFrame, *, model: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "chunk_id": chunk_df["chunk_id"],
            "model": [model] * len(chunk_df),
            "vector": [f"{model}:{chunk_id}".encode() for chunk_id in chunk_df["chunk_id"]],
        }
    )


def _main() -> None:
    ns.push()
    result = cursor_workflow().run(executor=ava.LocalExecutor())
    print("Cursor example complete")
    print(result)
    print(f"Artifacts: {EXAMPLE_ROOT}")


if __name__ == "__main__":
    _main()
