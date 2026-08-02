"""Chunk one built target's Sphinx XML output into JSONL.

Run once per chip target. Each run records which target it came from, so the
deduplication step can later collapse pages that are byte-identical across
targets into a single chunk carrying the full list of chips it applies to.

esp-docs writes doxygen's XML into the same directory tree as the Sphinx
builder's, so the walk filters to Sphinx pages (a <document> root) rather than
trusting the extension.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from chunking import MAX_WORDS_PER_CHUNK, MIN_WORDS_PER_CHUNK
from sphinx_xml import chunk_sphinx_xml, is_sphinx_page

app = typer.Typer()


def find_sphinx_pages(xml_root: Path) -> list[Path]:
    """Every Sphinx-produced page under xml_root, sorted for reproducible output."""
    return sorted(p for p in xml_root.rglob("*.xml") if is_sphinx_page(p))


@app.command()
def ingest(
    xml_root: Path = typer.Argument(..., help="Sphinx XML builder output dir for one target."),
    chip: str = typer.Option(..., help="Target this build was produced for, e.g. 'esp32p4'."),
    out_path: Path = typer.Option(..., help="JSONL destination."),
    max_words: int = typer.Option(MAX_WORDS_PER_CHUNK, help="Soft cap on words per chunk."),
    min_words: int = typer.Option(MIN_WORDS_PER_CHUNK, help="Chunks below this are merged into a neighbour."),
) -> None:
    """Chunk every Sphinx page under xml_root, writing one JSON object per chunk."""
    pages = find_sphinx_pages(xml_root)
    typer.echo(f"found {len(pages)} Sphinx pages under {xml_root}")

    failures: list[tuple[Path, str]] = []
    written = 0

    with out_path.open("w") as out_file, typer.progressbar(pages, label=f"chunking {chip}") as progress:
        for page in progress:
            docname = page.relative_to(xml_root).with_suffix("").as_posix()
            try:
                chunks = chunk_sphinx_xml(page, docname, max_words=max_words, min_words=min_words)
            except Exception as exc:  # noqa: BLE001 - one bad page shouldn't abort the corpus
                failures.append((page, f"{type(exc).__name__}: {exc}"))
                continue
            for chunk in chunks:
                record = chunk.model_dump()
                record["chip"] = chip
                out_file.write(json.dumps(record) + "\n")
                written += 1

    typer.echo(f"wrote {written} chunks -> {out_path}")
    if failures:
        typer.echo(f"\n{len(failures)} pages failed:")
        for page, error in failures:
            typer.echo(f"  {page}: {error}")


if __name__ == "__main__":
    app()
