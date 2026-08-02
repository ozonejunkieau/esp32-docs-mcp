"""Embed chunked ESP documentation (JSONL) and write it into the shared LanceDB table.

Reads the JSONL produced by dedup_chunks.py (or an equivalent TRM ingest step),
embeds each chunk's text in batches, and writes rows into the same LanceDB
table regardless of source -- doc_type/chip/chips/file_refs/doc_refs/symbol_refs
are what let queries scope to one corner of the corpus without needing
separate tables.
"""

from __future__ import annotations

import json
from pathlib import Path

import lancedb
import typer

from embedder import Embedder
from provenance import SourceRevision, source_revision
from schema import DOC_TYPES, Chunk

app = typer.Typer()

BATCH_SIZE = 32


def _source_doc(section_path: str, text: str, file_path: str) -> str:
    """Derive a human-readable document name from a chunk's heading path.

    When a whole document collapses into a single root-level chunk,
    section_path is empty -- but the document's title is still the first
    line of the rendered text (e.g. "Static Analyzer\n\nA static analyzer
    is..."), which reads far better than falling back to the raw filename.
    """
    if section_path:
        return section_path.split(" > ")[0]
    first_line = text.split("\n", 1)[0].strip()
    return first_line or Path(file_path).stem


def _load_raw_chunks(jsonl_path: Path) -> list[dict]:
    with jsonl_path.open() as f:
        return [json.loads(line) for line in f]


def _batched_by_length(chunks: list[dict]) -> list[dict]:
    """Order chunks by length so each batch pads to a similar width.

    A batch costs the model its longest member times its size, because shorter
    sequences are padded up to match. Chunk lengths here span two orders of
    magnitude -- a handful of words for a stub section, two thousand for an
    oversized register table -- so a batch drawn in document order is padded to
    roughly twice the tokens it actually contains. Measured on the ESP-IDF
    corpus: 6.97M padded tokens in document order against 3.19M real ones, a
    2.18x tax, falling to 1.22x sorted -- a ~1.8x wall-clock saving for a sort,
    with no effect on the embeddings themselves.

    (Sorting on characters while the tax is measured in words keeps that 1.22x
    honest; sorting and measuring on the same metric flatters the result to
    1.01x, and neither is the tokenizer's true count.)

    Row order in the table is irrelevant -- vector search doesn't use it, and
    every chunk carries its own file_path and chunk_index -- so reordering
    writes costs nothing. Ties break on the original position to keep runs
    reproducible.
    """
    return [chunk for _, _, chunk in sorted(
        ((len(chunk["text"]), index, chunk) for index, chunk in enumerate(chunks)),
        key=lambda item: (item[0], item[1]),
    )]


@app.command()
def embed_and_store(
    jsonl_path: Path = typer.Argument(..., help="Chunks JSONL, e.g. the deduplicated output of dedup_chunks.py."),
    doc_type: str = typer.Option(..., help=f"One of {DOC_TYPES}."),
    db_path: Path = typer.Option(Path("./esp_docs.lancedb"), help="LanceDB database directory."),
    table_name: str = typer.Option("chunks", help="Table to write into."),
    chip: str = typer.Option(
        "",
        help="Fallback chip for chunks that don't carry one, e.g. 'esp32p4'. A chunk's own 'chip' field "
        "takes precedence -- ingest_trm.py sets it per row, since one TRM JSONL spans every manual.",
    ),
    source_repo: Path = typer.Option(
        None,
        help="Checkout the chunks were derived from (the esp-idf or TRM repo). Its git revision is "
        "recorded on every row so a result can be traced to the docs that produced it.",
    ),
    batch_size: int = typer.Option(BATCH_SIZE, help="Chunks embedded per batch."),
    overwrite: bool = typer.Option(
        False, help="Recreate the table with the current schema instead of appending. Use when Chunk's fields have changed since the table was created."
    ),
) -> None:
    """Embed every chunk in jsonl_path and write it into the LanceDB table."""
    raw_chunks = _load_raw_chunks(jsonl_path)
    typer.echo(f"loaded {len(raw_chunks)} chunks from {jsonl_path}")
    raw_chunks = _batched_by_length(raw_chunks)

    revision = source_revision(source_repo) if source_repo else SourceRevision()
    if source_repo:
        typer.echo(f"source revision: {revision.version} ({revision.commit[:12]})")
    else:
        # Loud, because provenance can't be reconstructed later from the rows
        # themselves -- only by remembering which checkout was current at the time.
        typer.echo("WARNING: --source-repo not given; rows will carry no upstream revision")

    embedder = Embedder()
    db = lancedb.connect(db_path)
    if overwrite:
        table = db.create_table(table_name, schema=Chunk, mode="overwrite")
    elif table_name in db.list_tables().tables:
        table = db.open_table(table_name)
    else:
        table = db.create_table(table_name, schema=Chunk)

    written = 0
    with typer.progressbar(range(0, len(raw_chunks), batch_size), label="embedding") as progress:
        for start in progress:
            batch = raw_chunks[start : start + batch_size]
            vectors = embedder.embed_documents([c["text"] for c in batch])

            rows = [
                Chunk(
                    vector=vector,
                    text=c["text"],
                    source_doc=_source_doc(c["section_path"], c["text"], c["file_path"]),
                    doc_type=doc_type,
                    # A chunk's own chip wins over --chip. One TRM JSONL spans
                    # every manual, so the per-row value is the only correct one;
                    # --chip remains for single-chip inputs that don't carry it.
                    chip=c.get("chip") or chip,
                    section_path=c["section_path"],
                    file_refs=c.get("file_refs", []),
                    doc_refs=c.get("doc_refs", []),
                    symbol_refs=c.get("symbol_refs", []),
                    chips=c.get("chips", []),
                    revisions=c.get("revisions", []),
                    file_path=c["file_path"],
                    chunk_index=c["chunk_index"],
                    source_version=revision.version,
                    source_commit=revision.commit,
                )
                for c, vector in zip(batch, vectors)
            ]
            table.add(rows)
            written += len(rows)

    typer.echo(f"wrote {written} chunks -> {db_path}/{table_name}")


if __name__ == "__main__":
    app()