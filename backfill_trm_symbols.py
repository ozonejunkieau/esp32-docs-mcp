"""Populate `symbol_refs` on TRM rows from register names already in their text.

`latex_parser` does not populate `Refs`, so TRM chunks reach the store with an
empty `symbol_refs`. That leaves `esp32_docs_find_symbol` half-built: a register
name resolves to its C definition in the SoC headers and to the ESP-IDF API
reference, but not to the manual that describes what the register does -- which
is the cross-corpus join the tool exists for.

The names are recoverable without re-parsing anything. `_render_register` emits
`Register I2C_SCL_LOW_PERIOD_REG at address 0x0000` as a chunk's first line, so
the register a chunk documents is sitting in the stored text.

This is a repair, not the fix. Extracting from rendered output is weaker than
extracting from source, and the durable answer is for `latex_parser` to populate
`Refs` during ingest. Run this to make the feature work today; delete it once a
TRM rebuild carries symbols natively.

`symbol_refs` is metadata, so this needs no re-embedding -- the vectors are read
back and rewritten untouched, exactly as `backfill_provenance.py` avoids hours
of GPU time for a column.
"""

from __future__ import annotations

import re
from pathlib import Path

import lancedb
import typer

app = typer.Typer()

# The header _render_register emits. The optional parenthetical carries the
# index range of a parameterised register -- "LEDC_CH{n}_CONF0_REG (n: 0-7)" --
# and is stripped, since a caller searches for the bare name.
_REGISTER_HEADER_RE = re.compile(r"Register ([A-Za-z0-9_]+)\s*(?:\([^)]*\))?\s* at address")


def register_names(text: str) -> list[str]:
    """Register names documented by one chunk, first-seen order, de-duplicated."""
    return list(dict.fromkeys(_REGISTER_HEADER_RE.findall(text)))


def _symbols_from_jsonl(path: Path) -> dict[tuple[str, str], list[str]]:
    """Map (file_path, text) -> symbol_refs from a freshly chunked JSONL.

    Preferred over re-deriving from stored text: the ingest knows a register's
    declared index range, so it can emit both the manual's parameterised spelling
    and each concrete member. Regex over rendered output only ever sees the
    former, and the headers use the latter.
    """
    import json

    mapping: dict[tuple[str, str], list[str]] = {}
    with path.open() as f:
        for line in f:
            row = json.loads(line)
            mapping[(row["file_path"], row["text"])] = row.get("symbol_refs", [])
    return mapping


@app.command()
def backfill(
    db_path: Path = typer.Option(Path("./esp_docs.lancedb"), help="LanceDB database directory."),
    table_name: str = typer.Option("chunks", help="Table to update."),
    backup: Path = typer.Option(Path("./trm_rows_backup.parquet"), help="Where to write the pre-change snapshot."),
    from_jsonl: Path = typer.Option(
        None,
        help="Freshly chunked TRM JSONL to take symbols from, matched on (file_path, text). "
        "More accurate than re-deriving from stored text; rows with no match keep the derived set.",
    ),
    dry_run: bool = typer.Option(False, help="Report what would change without writing."),
) -> None:
    """Set symbol_refs on TRM rows from the register headers in their text."""
    db = lancedb.connect(db_path)
    table = db.open_table(table_name)

    before = table.count_rows()
    rows = table.search().where("doc_type = 'trm'").limit(before + 1).to_pandas()
    typer.echo(f"table {table_name}: {before} rows, {len(rows)} of them TRM")

    if not len(rows):
        raise typer.BadParameter("no TRM rows found -- nothing to backfill")

    already = int((rows["symbol_refs"].apply(len) > 0).sum())
    if already:
        typer.echo(f"WARNING: {already} TRM rows already carry symbol_refs; they will be overwritten")

    if from_jsonl:
        authoritative = _symbols_from_jsonl(from_jsonl)
        matched = 0

        def pick(row):
            nonlocal matched
            key = (row["file_path"], row["text"])
            if key in authoritative:
                matched += 1
                return authoritative[key]
            return register_names(row["text"])

        rows["symbol_refs"] = rows.apply(pick, axis=1)
        unmatched = len(rows) - matched
        typer.echo(f"matched against {from_jsonl}: {matched} rows; {unmatched} fell back to text extraction")
        if unmatched:
            # Text drift means the store predates a rendering change. Those rows
            # also carry a stale vector, so symbols alone will not resync them.
            typer.echo(f"  {unmatched} rows differ in text from the JSONL -- the store is behind on rendering there")
    else:
        rows["symbol_refs"] = rows["text"].apply(register_names)

    changed = int((rows["symbol_refs"].apply(len) > 0).sum())
    names = {n for refs in rows["symbol_refs"] for n in refs}
    typer.echo(f"rows gaining symbols: {changed}  ({len(names)} distinct register names)")

    if dry_run:
        typer.echo("\ndry run -- nothing written")
        return

    # Written before anything is destroyed, and read back before the delete.
    # The vectors are in here, so a failed run costs a restore rather than the
    # hours of embedding that produced them.
    rows.to_parquet(backup)
    verify = __import__("pandas").read_parquet(backup)
    if len(verify) != len(rows):
        raise RuntimeError(f"backup verification failed: wrote {len(rows)} rows, read back {len(verify)}")
    typer.echo(f"backup written and verified: {backup} ({len(verify)} rows, vectors included)")

    table.delete("doc_type = 'trm'")
    remaining = table.count_rows()
    typer.echo(f"deleted TRM rows; {remaining} remain")

    table.add(rows.to_dict("records"))
    after = table.count_rows()
    typer.echo(f"re-added; table now {after} rows")

    if after != before:
        typer.echo(f"ROW COUNT CHANGED: {before} -> {after}. Restore from {backup} before doing anything else.")
        raise typer.Exit(1)
    typer.echo(f"row count unchanged ({after}); backup at {backup} can be deleted once you have spot-checked")


if __name__ == "__main__":
    app()
