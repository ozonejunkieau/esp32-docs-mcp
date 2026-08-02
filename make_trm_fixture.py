"""Synthesise a TRM chunk JSONL, faithful or deliberately broken, to test the checkers.

`register_census.py` and `check_thin_files.py latex` are the safety net for the
TRM corpus, but a safety net nobody has fallen into is untested equipment: until
`ingest_trm.py` exists there is no real TRM chunk JSONL to point them at, and
thresholds picked against nothing are guesses.

So this generates two corpora from the real LaTeX source:

- `--mode good` expands each chapter's `\\subfile`/`\\input` graph, i.e. what a
  correct parser should produce. The checkers must pass on it.
- `--mode broken` uses only each chapter file's own body, simulating exactly the
  failure this project cares most about -- unresolved includes, which silently
  strip the register content that lives entirely in included files. The checkers
  must fail on it, loudly, and name the affected chapters.

A checker that cannot tell those two apart is not protecting anything. Run both
after changing either checker.

The text rendering here is crude on purpose: it flattens LaTeX with regexes
rather than parsing it. Fixtures only need to be representative in *shape* --
chunk sizes, register coverage, file attribution -- and using the real parser
would make the fixture agree with the parser by construction, which is precisely
the agreement these checkers exist to test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import typer

import trm_verify as tv

app = typer.Typer()

# Roughly the real chunker's cap, so synthetic chunk counts land in a plausible
# range. Exactness doesn't matter -- the checkers compare ratios, not totals.
WORDS_PER_CHUNK = 350

_MATH = re.compile(r"\$[^$]*\$")
_MACRO_WITH_ARG = re.compile(r"\\(?:label|ref|hyperref|includegraphics)\s*(\[[^\]]*\])?\s*\{[^}]*\}")
_MACRO = re.compile(r"\\[A-Za-z@]+\*?")
_SYNTAX = re.compile(r"[{}&~^\\]")


def _flatten(text: str) -> str:
    """Reduce LaTeX to bare words, keeping register names readable."""
    text = text.replace(r"\_", "_")
    text = _MATH.sub(" ", text)
    text = _MACRO_WITH_ARG.sub(" ", text)
    text = _MACRO.sub(" ", text)
    text = _SYNTAX.sub(" ", text)
    return " ".join(text.split())


@app.command()
def make(
    out_path: Path = typer.Argument(..., help="JSONL destination."),
    mode: str = typer.Option("good", help="'good' expands includes; 'broken' omits them."),
    chip: str = typer.Option(
        "all",
        help="TRM chip directory to synthesise from, e.g. 'ESP32-P4'. Default 'all' covers every chip -- "
        "register_census.py compares against the whole corpus, so a single-chip fixture makes its total meaningless.",
    ),
    source_root: Path = typer.Option(None, help="TRM checkout; defaults to $TRM_PATH, then ./trm_latex."),
    words_per_chunk: int = typer.Option(WORDS_PER_CHUNK, help="Words per synthetic chunk."),
) -> None:
    """Write a synthetic TRM chunk JSONL for exercising the verification tools."""
    if mode not in ("good", "broken"):
        raise typer.BadParameter("mode must be 'good' or 'broken'")

    root = tv.resolve_trm_root(source_root)
    if chip == "all":
        chip_dirs = tv.chip_dirs(root)
    else:
        chip_dir = root / chip
        if not chip_dir.is_dir():
            raise typer.BadParameter(f"no such chip directory: {chip_dir}")
        chip_dirs = [chip_dir]

    records: list[dict] = []
    chapter_count = 0

    for chip_dir in chip_dirs:
        for chapter in tv.chapter_files(chip_dir):
            chapter_count += 1
            if mode == "good":
                text = tv.expand_document(chapter, chip_dir, root).text
            else:
                text = tv.document_body(chapter.read_text(errors="replace"))

            label = chapter.relative_to(root).with_suffix("").as_posix()
            tokens = _flatten(text).split()
            pieces = [" ".join(tokens[i : i + words_per_chunk]) for i in range(0, len(tokens), words_per_chunk)] or [""]
            for index, piece in enumerate(pieces):
                records.append(
                    {
                        "text": f"{label}\n\n{piece}",
                        "section_path": label,
                        "file_path": label,
                        "chunk_index": index,
                        "chip": chip_dir.name.replace("-", "").lower(),
                        "file_refs": [],
                        "doc_refs": [],
                        "symbol_refs": [],
                        "chips": [],
                    }
                )

    with out_path.open("w") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")

    typer.echo(
        f"wrote {len(records)} synthetic chunks from {chapter_count} chapters "
        f"across {len(chip_dirs)} chip(s) ({mode}) -> {out_path}"
    )
    if mode == "broken":
        typer.echo("expect the checkers to FAIL on this file -- that is the point")


if __name__ == "__main__":
    app()
