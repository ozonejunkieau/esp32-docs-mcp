"""Quick sanity checks on the LanceDB chunk store -- row count, sample rows,
and a self-search round-trip to confirm the vector index isn't corrupted.

Useful after an interrupted embed_and_store.py run to confirm what's already
durably written before deciding whether to resume.
"""

from __future__ import annotations

from pathlib import Path

import lancedb
import typer

app = typer.Typer()


@app.command()
def validate(
    db_path: Path = typer.Option(Path("./esp_docs.lancedb"), help="LanceDB database directory."),
    table_name: str = typer.Option("chunks", help="Table to check."),
) -> None:
    """Report row count, doc_type breakdown, sample rows, and a self-search sanity check."""
    db = lancedb.connect(db_path)
    if table_name not in db.list_tables().tables:
        typer.echo(f"no table '{table_name}' found at {db_path} -- nothing written yet")
        raise typer.Exit(1)

    table = db.open_table(table_name)
    count = table.count_rows()
    typer.echo(f"{count} rows in {db_path}/{table_name}\n")
    if count == 0:
        typer.echo("table exists but is empty -- nothing survived")
        raise typer.Exit(1)

    df = table.to_pandas()
    typer.echo("doc_type breakdown:")
    typer.echo(df["doc_type"].value_counts().to_string())

    typer.echo("\nfile_path coverage (unique source files with at least one chunk written):")
    typer.echo(f"  {df['file_path'].nunique()} unique files")

    display_cols = [c for c in ("source_doc", "doc_type", "chip", "chips", "file_path", "chunk_index") if c in df.columns]
    missing_cols = [c for c in ("source_doc", "doc_type", "chip", "chips", "file_path", "chunk_index") if c not in df.columns]
    if missing_cols:
        typer.echo(
            f"\nnote: this table predates the current schema -- missing column(s): {missing_cols}. "
            "If schema.Chunk has fields since added, you'll want a fresh --overwrite run once you're "
            "done adding fields, rather than migrating this table piecemeal."
        )

    typer.echo("\nsample rows:")
    typer.echo(df[display_cols].sample(min(5, count)).to_string())

    # Self-search: use one row's own vector as the query. It should come back
    # as its own top hit at ~0 distance -- confirms the vector index round-trips
    # correctly without needing to re-invoke the real embedding model.
    sample_row = df.iloc[0]
    results = table.search(sample_row["vector"]).limit(1).to_pandas()
    is_self_match = results.iloc[0]["file_path"] == sample_row["file_path"] and results.iloc[0]["chunk_index"] == sample_row["chunk_index"]
    typer.echo(f"\nself-search round-trip: {'OK' if is_self_match else 'UNEXPECTED -- investigate'} (distance={results.iloc[0]['_distance']:.4f})")


if __name__ == "__main__":
    app()