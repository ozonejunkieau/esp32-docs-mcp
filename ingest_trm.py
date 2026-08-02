"""Chunk the TRM LaTeX corpus into JSONL, one row per unique chunk.

Walks each chip's numbered chapter documents, parses them once per silicon
revision the chip publishes, and merges the results so a chunk appears once
carrying every revision it applies to.

The revision axis is why this can't just be a loop over files. Espressif ships
more than one manual for some chips -- ESP32-P4 has a mainline TRM and a separate
"Chip Revision v1.3" one -- and their register sets diverge in both directions
(48 registers only in v1.3, 60 only in mainline). Ingesting one variant silently
drops the other's registers, which for register-level content is a hardware bug
waiting to happen. So every variant is parsed and the outputs deduplicated by
exact text, exactly as the ESP-IDF path deduplicates across chip targets: content
common to both variants stores once listing both, content that differs stays
separate and correctly narrower.

Deliberately no fuzzy matching. Two variants differing by one bitfield must stay
distinct -- that difference is the entire reason someone asks which revision they
have.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import typer

from chip_vocab import load_chip_vocabulary
from chunking import MAX_WORDS_PER_CHUNK, MIN_WORDS_PER_CHUNK
from latex_parser import chunk_document
from trm_verify import chapter_files, chip_dirs, resolve_trm_root

app = typer.Typer()

MAINLINE = "mainline"

# Aggregator documents: "<CHIP>-main__EN.tex" is the mainline manual, and
# "<CHIP>-chip-revision-<label>__EN.tex" a stepping-specific one.
_MAIN_RE = re.compile(r"-main__EN\.tex$")
_REVISION_RE = re.compile(r"-chip-revision-(?P<label>[^_]+)__EN\.tex$")
_USETAG_RE = re.compile(r"\\usetag\s*\{([^}]*)\}")


def _aggregator_variants(chip_dir: Path) -> dict[str, frozenset[str]]:
    """Map revision label -> the extra tags that variant's manual activates.

    Read from the aggregator documents rather than hardcoded, because they are
    where Espressif actually declares it: each `\\usetag` in a manual's preamble
    selects the tagged content for that build. Deriving it means nobody has to
    remember to pass the right tag, and a new revision manual appearing upstream
    is picked up rather than silently ignored.

    `\\usetag{\\chipseries}` is skipped -- it resolves to the chip's own name and
    the parser already activates that for every build.
    """
    variants: dict[str, frozenset[str]] = {}
    for path in sorted(chip_dir.glob("*__EN.tex")):
        name = path.name
        revision_match = _REVISION_RE.search(name)
        if revision_match:
            label = revision_match.group("label")
        elif _MAIN_RE.search(name):
            label = MAINLINE
        else:
            continue
        tags = {t.strip() for t in _USETAG_RE.findall(path.read_text(errors="replace"))}
        variants[label] = frozenset(t for t in tags if t and not t.startswith("\\"))
    return variants or {MAINLINE: frozenset()}


@app.command()
def ingest(
    out_path: Path = typer.Option(..., help="JSONL destination."),
    source_root: Path = typer.Option(None, help="TRM checkout; defaults to $TRM_PATH, then ./trm_latex."),
    chip: str = typer.Option(None, help="Limit to one TRM folder, e.g. 'ESP32-P4'. Omit for all."),
    max_words: int = typer.Option(MAX_WORDS_PER_CHUNK, help="Soft cap on words per chunk."),
    min_words: int = typer.Option(MIN_WORDS_PER_CHUNK, help="Chunks below this merge into a neighbour."),
) -> None:
    """Chunk every TRM chapter into JSONL, deduplicated across silicon revisions."""
    root = resolve_trm_root(source_root)
    vocab = load_chip_vocabulary()
    typer.echo(f"TRM source: {root}")

    targets = [root / chip] if chip else chip_dirs(root)
    failures: list[tuple[Path, str]] = []
    written = 0
    unresolved = 0

    with out_path.open("w") as out_file:
        for chip_dir in targets:
            canonical = vocab.chip_for_trm_folder(chip_dir.name)
            if canonical is None:
                typer.echo(f"  {chip_dir.name}: SKIPPED -- no chip in chips.yaml maps to this folder")
                continue

            variants = _aggregator_variants(chip_dir)
            declared = set(vocab.revisions_for(canonical))
            if declared and set(variants) != declared:
                # chips.yaml drives the MCP server's revision filter and its
                # "is this revision-specific?" answer, so drift between the two
                # would quietly mislabel results.
                typer.echo(
                    f"  {chip_dir.name}: WARNING chips.yaml declares {sorted(declared)} "
                    f"but the source has {sorted(variants)} -- update chips.yaml"
                )

            chapters = chapter_files(chip_dir)
            # Keyed on (file_path, text): identical text from two variants is the
            # same passage and merges; identical text on two different pages is
            # real duplication within one manual and stays separately citable.
            merged: dict[tuple[str, str], dict] = {}
            order: list[tuple[str, str]] = []

            for revision, extra_tags in sorted(variants.items()):
                for chapter in chapters:
                    docname = chapter.relative_to(root).with_suffix("").as_posix()
                    try:
                        chunks, doc = chunk_document(
                            chapter,
                            extra_tags=extra_tags or None,
                            file_path=docname,
                            max_words=max_words,
                            min_words=min_words,
                        )
                    except Exception as exc:  # noqa: BLE001 - one bad chapter shouldn't abort the corpus
                        failures.append((chapter, f"[{revision}] {type(exc).__name__}: {exc}"))
                        continue
                    unresolved += len(doc.missing)
                    for c in chunks:
                        key = (c.file_path, c.text)
                        entry = merged.get(key)
                        if entry is None:
                            record = c.model_dump()
                            record["chip"] = canonical
                            record["revisions"] = [revision]
                            merged[key] = record
                            order.append(key)
                        elif revision not in entry["revisions"]:
                            entry["revisions"].append(revision)

            for key in order:
                record = merged[key]
                record["revisions"] = sorted(record["revisions"])
                out_file.write(json.dumps(record) + "\n")
                written += 1

            spread = defaultdict(int)
            for key in order:
                spread[len(merged[key]["revisions"])] += 1
            detail = ", ".join(f"{n} revision(s): {count}" for n, count in sorted(spread.items()))
            typer.echo(
                f"  {chip_dir.name:12s} -> {canonical:9s} {len(chapters):3d} chapters, "
                f"variants {sorted(variants)}, {len(order):5d} chunks  [{detail}]"
            )

    typer.echo(f"\nwrote {written} chunks -> {out_path}")
    if unresolved:
        typer.echo(f"WARNING: {unresolved} unresolved includes -- register content may be missing")
    if failures:
        typer.echo(f"\n{len(failures)} chapters failed:")
        for path, error in failures:
            typer.echo(f"  {path}: {error}")


if __name__ == "__main__":
    app()
