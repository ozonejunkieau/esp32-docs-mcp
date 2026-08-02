"""Add provenance columns to a table embedded before provenance existed.

Provenance is constant across an ingest -- every row from one run came from one
upstream commit -- so it can be filled in afterwards without recomputing a single
embedding. That matters: re-embedding this corpus is hours of GPU time, and the
vectors are unaffected by a metadata column.

Only for tables genuinely missing the columns. Once `embed_and_store.py` is given
`--source-repo`, new tables carry provenance from the start and this script has
nothing to do.
"""

from __future__ import annotations

from pathlib import Path

import lancedb
import typer

from provenance import source_revision

app = typer.Typer()


def _sql_literal(value: str) -> str:
    """Quote a value for a Lance SQL expression."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


@app.command()
def backfill(
    source_repo: Path = typer.Argument(..., help="Checkout the existing rows were derived from."),
    db_path: Path = typer.Option(Path("./esp_docs.lancedb"), help="LanceDB database directory."),
    table_name: str = typer.Option("chunks", help="Table to update."),
    doc_type: str = typer.Option(
        None,
        help="Only stamp rows of this doc_type. Omit when the whole table came from one ingest; "
        "required once a table mixes corpora, since they have different upstreams.",
    ),
    dry_run: bool = typer.Option(False, help="Report what would change without writing."),
) -> None:
    """Stamp source_version/source_commit onto rows that lack them."""
    revision = source_revision(source_repo)
    if revision.commit == "unknown":
        raise typer.BadParameter(f"{source_repo} is not a usable git checkout -- refusing to stamp 'unknown'")

    db = lancedb.connect(db_path)
    table = db.open_table(table_name)
    existing = set(table.schema.names)
    rows = table.count_rows()

    typer.echo(f"table {table_name}: {rows} rows")
    typer.echo(f"source revision: {revision.version} ({revision.commit})")

    missing = [name for name in ("source_version", "source_commit", "revisions") if name not in existing]
    typer.echo(f"columns present: {sorted(existing & {'source_version', 'source_commit', 'revisions'}) or 'none'}")
    typer.echo(f"columns to add:  {missing or 'none'}")

    if doc_type:
        # A mixed table needs per-corpus values, which add_columns cannot express
        # in one constant expression -- surface that rather than stamping wrongly.
        other = table.count_rows(f"doc_type != '{doc_type}'")
        if other:
            typer.echo(f"\n{other} rows have a different doc_type and would be stamped with the wrong revision.")
            typer.echo("Refusing. Stamp each corpus before mixing them, or extend this script.")
            raise typer.Exit(1)

    if dry_run:
        typer.echo("\ndry run -- nothing written")
        return

    if missing:
        # revisions is TRM-only and empty for IDF; an empty list is the correct
        # value for a table that predates the TRM corpus entirely.
        additions = {}
        if "source_version" in missing:
            additions["source_version"] = _sql_literal(revision.version)
        if "source_commit" in missing:
            additions["source_commit"] = _sql_literal(revision.commit)
        if "revisions" in missing:
            additions["revisions"] = "array_remove(['x'], 'x')"
        table.add_columns(additions)
        typer.echo(f"\nadded {len(additions)} column(s)")
    else:
        typer.echo("\nnothing to add -- table already has provenance columns")

    stamped = table.count_rows(f"source_commit = {_sql_literal(revision.commit)}")
    typer.echo(f"rows carrying this revision: {stamped}/{rows}")
    if stamped != rows:
        typer.echo("WARNING: not every row was stamped -- inspect before trusting provenance on this table")


if __name__ == "__main__":
    app()
