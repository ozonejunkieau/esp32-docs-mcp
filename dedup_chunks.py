"""Collapse per-target chunk sets into one corpus, tagging each chunk with its chips.

Every target's docs are built separately, and the overwhelming majority of
ESP-IDF documentation is identical across chips -- embedding all nine copies
would multiply storage and inference cost while making search return the same
passage nine times.

Deduplication is by *exact* text match, deliberately. Where two targets differ
in even one substituted constant ("2 cores" vs "1 core", a different bootloader
offset), the chunks must stay separate and chip-scoped: that difference is
precisely what a chip-specific question needs. Normalizing text to increase the
collapse rate would erase the most valuable content in the corpus.

The grouping key is (page, text) rather than text alone. Identical text
appearing on two different pages is real duplication within a single target's
docs, and each location stays independently citable; the same text on the same
page across targets is the redundancy worth collapsing.

A chunk that survives on all targets ends up with every chip in `chips`, which
is what "chip-agnostic" now means -- there is no longer an empty-list special
case for a filter to remember.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import typer

app = typer.Typer()


def _merge_unique(lists: list[list[str]]) -> list[str]:
    """Union of several ref lists, first-seen order preserved."""
    merged: dict[str, None] = {}
    for items in lists:
        for item in items:
            merged.setdefault(item, None)
    return list(merged)


@app.command()
def dedup(
    jsonl_paths: list[Path] = typer.Argument(..., help="Per-target chunk JSONL files from ingest_sphinx_xml.py."),
    out_path: Path = typer.Option(..., help="Deduplicated JSONL destination."),
) -> None:
    """Merge per-target chunk files into one deduplicated corpus."""
    # Keyed by (file_path, text); insertion order keeps output stable and
    # roughly document-ordered for the first target seen.
    groups: dict[tuple[str, str], dict] = {}
    total_in = 0

    for path in jsonl_paths:
        count = 0
        with path.open() as f:
            for line in f:
                record = json.loads(line)
                count += 1
                key = (record["file_path"], record["text"])
                existing = groups.get(key)
                if existing is None:
                    groups[key] = {
                        "text": record["text"],
                        "section_path": record["section_path"],
                        "file_path": record["file_path"],
                        "chunk_index": record["chunk_index"],
                        "chips": [record["chip"]],
                        "file_refs": record["file_refs"],
                        "doc_refs": record["doc_refs"],
                        "symbol_refs": record["symbol_refs"],
                    }
                    continue
                if record["chip"] not in existing["chips"]:
                    existing["chips"].append(record["chip"])
                # Identical text should imply identical refs, but a union costs
                # nothing and avoids silently dropping a ref if it doesn't.
                for field in ("file_refs", "doc_refs", "symbol_refs"):
                    existing[field] = _merge_unique([existing[field], record[field]])
                # Content identical but positioned differently across targets:
                # the earliest position is the least surprising to cite.
                existing["chunk_index"] = min(existing["chunk_index"], record["chunk_index"])
        total_in += count
        typer.echo(f"  {path.name}: {count} chunks")

    with out_path.open("w") as out_file:
        for record in groups.values():
            record["chips"] = sorted(record["chips"])
            out_file.write(json.dumps(record) + "\n")

    spread = Counter(len(record["chips"]) for record in groups.values())
    typer.echo(f"\nread {total_in} chunks across {len(jsonl_paths)} targets")
    typer.echo(f"wrote {len(groups)} unique chunks -> {out_path}")
    if total_in:
        typer.echo(f"collapsed {total_in - len(groups)} duplicates ({100 * (1 - len(groups) / total_in):.1f}%)")
    typer.echo("\nchunks by number of chips they apply to:")
    for chip_count in sorted(spread):
        typer.echo(f"  {chip_count:2d} chip(s): {spread[chip_count]}")


if __name__ == "__main__":
    app()
