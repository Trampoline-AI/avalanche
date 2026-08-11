"""Runnable Stream example using the current provider API.

`ava.Stream(table, key=..., mode="append_scan")` is a dependency provider. The
runtime injects a
single `polars.DataFrame` for the claimed snapshot; user code does not call
`.read()` on the Stream object.
"""

from __future__ import annotations

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
        else Path(".avalanche/catalogs/stream_workflow") / str(os.getpid())
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
        name="stream_example",
        base_location=str(EXAMPLE_ROOT / "warehouse"),
    )

    document = ava.IcebergTable(schema=DocumentSchema)
    chunk = ava.IcebergTable(schema=ChunkSchema)
    embedding = ava.IcebergTable(schema=EmbeddingSchema)


ns = ExampleNamespace(
    catalog="stream-example",
    load_catalog_props={"type": "sql", "uri": f"sqlite:///{EXAMPLE_ROOT / 'catalog.db'}"},
)


@ava.source
def load_documents(*, docs=ns.document):
    ns.push()
    rows = pl.DataFrame(
        {
            "doc_id": ["doc-1", "doc-2"],
            "title": ["Release notes", "Operator guide"],
            "body": [
                "Stream providers inject one DataFrame per claimed snapshot.",
                "The operator discovers workflow builders from Python files.",
            ],
        }
    )
    return docs.append(rows)


@ava.step
def chunk_documents(
    _loaded: object = None,
    docs: pl.DataFrame = ava.Stream(ns.document, key="documents_to_chunks", mode="append_scan"),
    *,
    dest=ns.chunk,
):
    chunks = process_documents_to_chunks(docs)
    return dest.append(chunks)


@ava.step
def embed_chunks(
    _chunked: object = None,
    chunks: pl.DataFrame = ava.Stream(ns.chunk, key="chunks_to_embeddings", mode="append_scan"),
    *,
    dest=ns.embedding,
):
    embeddings = generate_embeddings(chunks, model="local-demo")
    return dest.append(embeddings)


@ava.dest
def summarize_embeddings(
    _embedded: object = None,
    embeddings: pl.DataFrame = ava.Stream(
        ns.embedding, key="embeddings_to_summary", mode="append_scan"
    ),
) -> str:
    models = sorted(set(embeddings["model"].to_list()))
    return f"summarized {len(embeddings)} embeddings from {', '.join(models)}"


@ava.workflow
def stream_workflow():
    # Equivalent NodeFuture argument form, less visual:
    # load = load_documents()
    # chunk = chunk_documents(load)
    # embed = embed_chunks(chunk)
    # summary = summarize_embeddings(embed)
    return load_documents() >> chunk_documents() >> embed_chunks() >> summarize_embeddings()


def process_documents_to_chunks(doc_df: pl.DataFrame) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for doc in doc_df.iter_rows(named=True):
        sentences = [part.strip() for part in str(doc["body"]).split(".") if part.strip()]
        for index, sentence in enumerate(sentences, start=1):
            rows.append(
                {
                    "chunk_id": f"{doc['doc_id']}-{index}",
                    "doc_id": doc["doc_id"],
                    "chunk_number": index,
                    "text": sentence,
                }
            )
    return pl.DataFrame(rows)


def generate_embeddings(chunk_df: pl.DataFrame, *, model: str) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "chunk_id": chunk_df["chunk_id"],
            "model": [model] * len(chunk_df),
            "vector": [f"vector:{chunk_id}".encode() for chunk_id in chunk_df["chunk_id"]],
        }
    )


def _main() -> None:
    result = stream_workflow().run(executor=ava.LocalExecutor()).result()
    print("Stream example complete")
    print(result)
    print(f"Artifacts: {EXAMPLE_ROOT}")


if __name__ == "__main__":
    _main()
