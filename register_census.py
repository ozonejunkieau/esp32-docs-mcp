"""Count register definitions in the TRM source and check they survive into chunks.

This is the highest-value TRM check. Register content is reachable only through
`\\subfile`/`\\input`: a chapter file on its own contains prose and almost no
registers. So if include resolution breaks -- a `\\def`'d path macro that isn't
expanded, a filename case difference, an include written relative to the wrong
directory -- the corpus loses its most valuable content while every aggregate
count still looks plausible, because the prose is all still there. Nothing in
the chunk output would indicate the loss. This script is what indicates it.

It compares by register *name*, not just totals, so a shortfall names the
chapter and the specific registers that vanished rather than leaving a number
to be explained.

    register_census.py source                     # source-side census only
    register_census.py check chunks_esp32c3.jsonl # census vs. chunk output
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import typer

from trm_verify import (
    chapter_files,
    chip_dirs,
    count_register_envs,
    expand_document,
    registers_in,
    resolve_trm_root,
)

app = typer.Typer(help=__doc__)

# Register names are the only tokens we test for, so tokenising chunk text once
# per chip beats running ~11.5k substring searches over it.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


class ChipCensus:
    """Registers found in one chip's source, indexed for comparison against chunks."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.by_chapter: dict[str, list[str]] = defaultdict(list)
        self.env_count = 0
        self.parameterised = 0
        self.unresolved: list[str] = []
        self.chapters = 0

    @property
    def named_count(self) -> int:
        return sum(len(v) for v in self.by_chapter.values())

    @property
    def names(self) -> set[str]:
        return {n for v in self.by_chapter.values() for n in v}


def census_for_chip(chip_dir: Path, root: Path) -> ChipCensus:
    census = ChipCensus(chip_dir.name)
    for chapter in chapter_files(chip_dir):
        doc = expand_document(chapter, chip_dir, root)
        census.chapters += 1
        census.env_count += count_register_envs(doc.text)
        label = chapter.relative_to(root).as_posix()
        registers = registers_in(doc.text, label)
        # Parameterised names (FOO_{n}_REG) can't be matched literally against
        # rendered chunk text, so they are counted but kept out of the by-name
        # comparison; including them would manufacture a permanent shortfall.
        census.parameterised += sum(1 for r in registers if r.parameterised)
        census.by_chapter[label] = [r.name for r in registers if not r.parameterised]
        census.unresolved.extend(doc.missing)
    return census


def _collect(root: Path, only_chip: str | None) -> list[ChipCensus]:
    def _squash(name: str) -> str:
        return name.replace("-", "").replace("_", "").lower()

    # Accept either spelling of a chip: the directory name (ESP32-C3) or the
    # short name the rest of the pipeline uses (esp32c3).
    dirs = [d for d in chip_dirs(root) if not only_chip or _squash(d.name) == _squash(only_chip)]
    if not dirs:
        raise typer.BadParameter(f"no chip directory matching {only_chip!r} under {root}")
    return [census_for_chip(d, root) for d in dirs]


@app.command()
def source(
    trm_root: Path = typer.Option(None, help="TRM LaTeX checkout; defaults to $TRM_PATH, ./trm_latex, ~/git/..."),
    chip: str = typer.Option(None, help="Restrict to one chip directory, e.g. 'ESP32-C3'."),
) -> None:
    """Census the source only: how many registers each manual should yield."""
    root = resolve_trm_root(trm_root)
    typer.echo(f"TRM source: {root}\n")

    censuses = _collect(root, chip)
    typer.echo(f"{'chip':12} {'chapters':>8} {'registers':>10} {'checkable':>10} {'param.':>7} {'unresolved':>11}")
    for c in censuses:
        typer.echo(
            f"{c.name:12} {c.chapters:>8} {c.env_count:>10} {c.named_count:>10} "
            f"{c.parameterised:>7} {len(c.unresolved):>11}"
        )

    total_env = sum(c.env_count for c in censuses)
    total_named = sum(c.named_count for c in censuses)
    total_param = sum(c.parameterised for c in censuses)
    typer.echo(
        f"\n{'TOTAL':12} {sum(c.chapters for c in censuses):>8} {total_env:>10} {total_named:>10} {total_param:>7}"
    )

    # A register whose \begin{register} arguments didn't parse can't be checked
    # against the chunks by name, so it would show up as a phantom shortfall.
    unparsed = total_env - total_named - total_param
    if unparsed:
        typer.echo(f"\nWARNING: {unparsed} register environments had unparseable arguments")

    unresolved = sorted({m for c in censuses for m in c.unresolved})
    if unresolved:
        typer.echo(f"\n{len(unresolved)} unresolved includes (content behind these is invisible to any parser):")
        for target in unresolved[:20]:
            typer.echo(f"  {target}")


@app.command()
def check(
    jsonl_path: Path = typer.Argument(..., help="TRM chunk JSONL to check."),
    trm_root: Path = typer.Option(None, help="TRM LaTeX checkout; defaults to $TRM_PATH, ./trm_latex, ~/git/..."),
    chip: str = typer.Option(None, help="Restrict to one chip directory, e.g. 'ESP32-C3'."),
    shortfall_threshold: float = typer.Option(2.0, help="Fail if more than this percent of registers are missing."),
    limit: int = typer.Option(20, help="How many missing registers / worst chapters to print."),
) -> None:
    """Compare the source census against the registers actually present in chunks."""
    root = resolve_trm_root(trm_root)
    censuses = _collect(root, chip)

    # Chunks carry the chip's short name (esp32c3); the source carries the
    # directory name (ESP32-C3). Matching on squashed lowercase avoids importing
    # chip_vocab just to bridge two spellings, and ESP8684/esp32c2 is handled by
    # falling back to a single pooled bucket when no chip matches.
    tokens_by_chip: dict[str, set[str]] = defaultdict(set)
    all_tokens: set[str] = set()
    # A few register names contain literal spaces -- upstream typos such as
    # "TWAI\_ARB LOST CAP\_REG" -- which no token set can match. They are rare
    # enough to justify keeping the raw text around for a substring fallback,
    # and reporting them as missing would be a false alarm on a real typo.
    spacey = {n for c in censuses for n in c.names if " " in n}
    text_by_chip: dict[str, list[str]] = defaultdict(list)
    chunk_count = 0
    with jsonl_path.open() as f:
        for line in f:
            record = json.loads(line)
            chunk_count += 1
            text = record.get("text", "")
            found = set(_TOKEN_RE.findall(text))
            all_tokens |= found
            key = str(record.get("chip") or "").replace("-", "").replace("_", "").lower()
            tokens_by_chip[key] |= found
            if spacey:
                text_by_chip[key].append(text)

    typer.echo(f"TRM source: {root}")
    typer.echo(f"chunks:     {jsonl_path} ({chunk_count} chunks)\n")

    typer.echo(f"{'chip':12} {'source':>8} {'in chunks':>10} {'missing':>8} {'capture':>9}")
    worst_chapters: list[tuple[float, str, int, int]] = []
    missing_examples: list[str] = []
    total_src = total_found = 0

    joined_cache: dict[str, str] = {}

    for c in censuses:
        key = c.name.replace("-", "").lower()
        tokens = tokens_by_chip.get(key) or all_tokens

        def present(name: str, key: str = key) -> bool:
            if " " not in name:
                return name in tokens
            if key not in joined_cache:
                joined_cache[key] = "\n".join(text_by_chip.get(key) or sum(text_by_chip.values(), []))
            return name in joined_cache[key]

        names = c.names
        found = {n for n in names if present(n)}
        total_src += len(names)
        total_found += len(found)
        pct = 100.0 * len(found) / len(names) if names else 100.0
        typer.echo(f"{c.name:12} {len(names):>8} {len(found):>10} {len(names) - len(found):>8} {pct:>8.1f}%")

        for chapter, chapter_names in c.by_chapter.items():
            unique = set(chapter_names)
            if not unique:
                continue
            hit = sum(1 for n in unique if present(n))
            worst_chapters.append((100.0 * hit / len(unique), chapter, hit, len(unique)))
        missing_examples.extend(sorted(names - found)[:limit])

    overall = 100.0 * total_found / total_src if total_src else 100.0
    typer.echo(f"\n{'TOTAL':12} {total_src:>8} {total_found:>10} {total_src - total_found:>8} {overall:>8.1f}%")

    worst_chapters.sort()
    incomplete = [row for row in worst_chapters if row[0] < 100.0]
    if incomplete:
        typer.echo(f"\n{len(incomplete)} chapters missing registers (worst first):")
        for pct, chapter, hit, total in incomplete[:limit]:
            typer.echo(f"  {pct:>6.1f}%  {hit:>4}/{total:<4}  {chapter}")

    if missing_examples:
        typer.echo("\nexample missing registers:")
        for name in missing_examples[:limit]:
            typer.echo(f"  {name}")

    shortfall = 100.0 - overall
    if shortfall > shortfall_threshold:
        typer.echo(f"\nFAIL: {shortfall:.1f}% of registers absent (threshold {shortfall_threshold}%).")
        typer.echo("A shortfall this size almost always means \\subfile/\\input resolution is dropping files.")
        raise typer.Exit(code=1)
    typer.echo(f"\nOK: {shortfall:.1f}% shortfall, within the {shortfall_threshold}% threshold.")


if __name__ == "__main__":
    app()
