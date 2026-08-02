"""Flag documentation files whose chunk output looks suspiciously thin for their size.

Run after every ingest. The failure mode this guards against is invisible in
aggregate counts: the corpus total looks healthy while individual files are
quietly empty. Both real content-loss bugs in this codebase (`field_list` being
skipped, dropping 4,222 nodes; `literal_strong` treated as block-level) were
found this way and by nothing else.

Two checks, because they fail differently:

- **Source bytes per chunk.** Cheap, catches a file that produced almost no
  chunks at all. Ratios are only comparable within one corpus -- source markup
  that never becomes chunk text differs wildly between Sphinx XML and LaTeX --
  so compare files against each other, never against a fixed idea of "normal".
- **Capture rate**: extracted words against the source's own text words. The
  stronger check, and the one that found the `field_list` bug -- it flags a page
  that produced a plausible number of chunks while dropping most of each one.

The two corpora are separate subcommands rather than one command with a mode
flag, because "what is the source of this chunk" genuinely differs: a Sphinx
page is one self-contained XML file, whereas a TRM chapter is a .tex file plus
everything it \\subfile's in, and its capture rate is meaningless without that
expansion.

    check_thin_files.py xml   chunks.jsonl --xml-root    .../esp32p4/xml
    check_thin_files.py latex chunks.jsonl --source-root ./trm_latex
"""

from __future__ import annotations

import json
import statistics
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import typer

from trm_verify import expand_document, resolve_trm_root, source_file_for, source_text_words

app = typer.Typer(help=__doc__)


class FileStats:
    """Per-source-file totals accumulated from the chunk JSONL."""

    def __init__(self) -> None:
        self.chunks = 0
        self.chars = 0
        self.words = 0


def _load_chunks(jsonl_path: Path) -> dict[str, FileStats]:
    """Aggregate a chunk JSONL by source file.

    Breadcrumb prefixes are excluded from the word count: every chunk repeats
    its section path, so counting it would inflate deeply-nested files and let a
    genuinely thin one pass.
    """
    stats: dict[str, FileStats] = defaultdict(FileStats)
    with jsonl_path.open() as f:
        for line in f:
            chunk = json.loads(line)
            entry = stats[chunk["file_path"]]
            entry.chunks += 1
            entry.chars += len(chunk["text"])
            entry.words += len(chunk["text"].split()) - len(chunk.get("section_path", "").split())
    return stats


def _report(
    rows: list[tuple[str, int, int, int, float | None]],
    missing: list[str],
    root: Path,
    bytes_per_chunk_threshold: int,
    min_capture: float,
    limit: int,
) -> None:
    """Print both checks over rows of (name, source_bytes, chunks, words, capture).

    A `capture` of None means the file was too small to judge. On a seven-word
    toctree index page a single dropped word moves the rate by 14%, so tiny
    files dominate any "worst capture" list with noise and bury the real losses;
    callers filter them out with min_source_words.
    """
    thin = sorted(
        ((name, size, chunks, size / chunks if chunks else float(size)) for name, size, chunks, _, _ in rows),
        key=lambda row: -row[3],
    )
    flagged = [row for row in thin if row[3] > bytes_per_chunk_threshold]
    typer.echo(f"{len(flagged)} of {len(rows)} files look thin (source bytes per chunk too high):\n")
    for name, size, chunks, ratio in flagged[:limit]:
        typer.echo(f"  {ratio:>8.0f} B/chunk  ({size}B, {chunks} chunks)  {name}")

    captures = [(name, cap) for name, _, _, _, cap in rows if cap is not None]
    if captures:
        median = statistics.median(cap for _, cap in captures)
        # Half the corpus median is an outlier by any reading, whatever the
        # corpus-wide bias of the source word estimate happens to be.
        relative_floor = median / 2
        typer.echo(f"\ncapture rate: median {median:.0f}% of source words across {len(captures)} files")
        low = sorted(
            ((name, cap) for name, cap in captures if cap < min_capture or cap < relative_floor),
            key=lambda row: row[1],
        )
        if low:
            typer.echo(f"{len(low)} files below {min_capture:.0f}% or half the median ({relative_floor:.0f}%):\n")
            for name, cap in low[:limit]:
                typer.echo(f"  {cap:>6.1f}%  {name}")
        else:
            typer.echo("no files below either threshold.")

    # A file in the JSONL with no source behind it means the two are out of
    # sync, which invalidates every ratio above -- worth surfacing loudly.
    if missing:
        typer.echo(f"\n{len(missing)} files in the JSONL have no source under {root} (stale JSONL or wrong root):")
        for name in missing[:limit]:
            typer.echo(f"  {name}")


@app.command()
def xml(
    jsonl_path: Path = typer.Argument(..., help="Chunks JSONL produced by ingest_sphinx_xml.py."),
    xml_root: Path = typer.Option(..., help="The Sphinx XML build dir the JSONL was ingested from."),
    bytes_per_chunk_threshold: int = typer.Option(
        20000, help="Flag a page if its XML size / chunk count exceeds this (bytes)."
    ),
    min_capture: float = typer.Option(80.0, help="Flag a page capturing less than this percent of its XML text."),
    min_source_words: int = typer.Option(
        50, help="Skip the capture check for files with fewer source words than this -- see _report."
    ),
    limit: int = typer.Option(30, help="How many of the worst offenders to print."),
) -> None:
    """Check the ESP-IDF corpus: one Sphinx XML page per chunk file_path."""
    stats = _load_chunks(jsonl_path)
    rows: list[tuple[str, int, int, int, float | None]] = []
    missing: list[str] = []

    for docname, entry in stats.items():
        source = xml_root / f"{docname}.xml"
        if not source.exists():
            missing.append(docname)
            continue
        # itertext() is the page's own rendered text, which is what the chunker
        # should have captured; >100% is normal and only well below is loss.
        try:
            source_words = len(" ".join(ET.parse(source).getroot().itertext()).split())
        except ET.ParseError:
            source_words = 0
        capture = 100.0 * entry.words / source_words if source_words >= min_source_words else None
        rows.append((docname, source.stat().st_size, entry.chunks, entry.words, capture))

    _report(rows, missing, xml_root, bytes_per_chunk_threshold, min_capture, limit)


def _chip_dir_of(source: Path, root: Path) -> Path:
    """The manual directory a .tex belongs to -- includes are resolved relative to it."""
    try:
        return root / source.resolve().relative_to(root).parts[0]
    except (ValueError, IndexError):
        return source.parent


@app.command()
def latex(
    jsonl_path: Path = typer.Argument(..., help="TRM chunks JSONL."),
    source_root: Path = typer.Option(
        None, help="TRM LaTeX checkout; defaults to $TRM_PATH, then ./trm_latex, then ~/git/..."
    ),
    bytes_per_chunk_threshold: int = typer.Option(
        20000, help="Flag a chapter if its expanded source size / chunk count exceeds this (bytes)."
    ),
    min_capture: float = typer.Option(
        50.0,
        help="Flag a chapter capturing less than this percent of its source words. Lower than the XML default: "
        "LaTeX source carries far more markup that legitimately never becomes text.",
    ),
    min_source_words: int = typer.Option(
        50, help="Skip the capture check for files with fewer source words than this -- see _report."
    ),
    limit: int = typer.Option(30, help="How many of the worst offenders to print."),
) -> None:
    """Check the TRM corpus: one .tex chapter, plus everything it includes, per file_path."""
    root = resolve_trm_root(source_root)
    typer.echo(f"TRM source: {root}\n")

    stats = _load_chunks(jsonl_path)
    rows: list[tuple[str, int, int, int, float | None]] = []
    missing: list[str] = []

    for file_path, entry in stats.items():
        source = source_file_for(root, file_path)
        if source is None:
            missing.append(file_path)
            continue
        # A chapter's chunks cover its subfiles too, so the comparison has to be
        # against expanded source. Measured against the bare chapter file, every
        # register-heavy chapter would show an absurd capture rate and the real
        # outliers would be invisible.
        doc = expand_document(source, _chip_dir_of(source, root), root)
        source_words = source_text_words(doc.text)
        capture = 100.0 * entry.words / source_words if source_words >= min_source_words else None
        rows.append((file_path, len(doc.text), entry.chunks, entry.words, capture))

    _report(rows, missing, root, bytes_per_chunk_threshold, min_capture, limit)


if __name__ == "__main__":
    app()
