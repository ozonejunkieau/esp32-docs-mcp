"""Scan real TRM .tex files for macros/environments our parser doesn't yet know about.

The core risk with pylatexenc (or any hand-registered-macro approach) is
silent mis-handling: an unregistered macro degrades gracefully into a
best-effort fallback rather than raising, so coverage gaps don't announce
themselves. This walks real files and reports every macro/environment name
used, split into "already handled" vs "not yet looked at" -- sorted by
frequency, so the highest-value gaps to check are at the top.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import typer
from pylatexenc.latexwalker import LatexEnvironmentNode, LatexGroupNode, LatexMacroNode, LatexWalker

from latex_parser import EXTRA_ENVIRONMENTS, EXTRA_MACROS, _ESCAPED_CHARS, _INLINE_TEXT_MACROS, _SKIPPED_MACROS, build_context_db

app = typer.Typer()

# Standard plain-LaTeX macros/environments that are well-understood and safe
# to ignore here even though they're not explicitly special-cased in
# latex_parser.py's render logic -- pylatexenc's default context already
# parses these correctly, and our generic fallback renders them reasonably.
_BASELINE_MACROS = {
    "section", "subsection", "subsubsection", "paragraph", "chapter", "part",
    "footnote", "emph", "textbf", "textit", "textsc", "texttt", "\\",
    "item", "caption", "label", "ref", "cite", "url", "newline", "noindent",
    "vspace", "hspace", "clearpage", "pagebreak", "small", "large", "Large",
}
_BASELINE_ENVIRONMENTS = {
    "itemize", "enumerate", "description", "quote", "quotation", "verbatim",
    "center", "flushleft", "flushright", "tabular",
}

_KNOWN_MACROS = _BASELINE_MACROS | _INLINE_TEXT_MACROS | _SKIPPED_MACROS | set(_ESCAPED_CHARS) | {m.macroname for m in EXTRA_MACROS}
_KNOWN_ENVIRONMENTS = _BASELINE_ENVIRONMENTS | {e.environmentname for e in EXTRA_ENVIRONMENTS}


def _walk_all_nodes(nodelist):
    """Recursively yield every node in a pylatexenc tree, however deeply nested."""
    for node in nodelist or []:
        yield node
        if isinstance(node, LatexGroupNode):
            yield from _walk_all_nodes(node.nodelist)
        elif isinstance(node, LatexEnvironmentNode):
            yield from _walk_all_nodes(node.nodelist)
        elif isinstance(node, LatexMacroNode) and node.nodeargd:
            for arg in node.nodeargd.argnlist or []:
                if arg is not None:
                    yield from _walk_all_nodes(arg if isinstance(arg, list) else [arg])


def _scan_file(path: Path, macro_counts: Counter, env_counts: Counter, macro_examples: dict, env_examples: dict) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    walker = LatexWalker(text, latex_context=build_context_db(), tolerant_parsing=True)
    nodelist, _, _ = walker.get_latex_nodes()

    for node in _walk_all_nodes(nodelist):
        if isinstance(node, LatexMacroNode):
            macro_counts[node.macroname] += 1
            macro_examples.setdefault(node.macroname, str(path))
        elif isinstance(node, LatexEnvironmentNode):
            env_counts[node.environmentname] += 1
            env_examples.setdefault(node.environmentname, str(path))


@app.command()
def check(
    root: Path = typer.Argument(..., help="Directory to recursively search for .tex files."),
    top_n: int = typer.Option(40, help="Show at most this many unknown macros/environments, most frequent first."),
) -> None:
    """Report every macro/environment used in root's .tex files that isn't already handled."""
    files = sorted(p for p in root.rglob("*.tex") if p.stem.endswith("__EN"))
    typer.echo(f"scanning {len(files)} English .tex files under {root}")

    macro_counts: Counter = Counter()
    env_counts: Counter = Counter()
    macro_examples: dict[str, str] = {}
    env_examples: dict[str, str] = {}
    failures: list[tuple[Path, str]] = []

    with typer.progressbar(files, label="scanning") as progress:
        for path in progress:
            try:
                _scan_file(path, macro_counts, env_counts, macro_examples, env_examples)
            except Exception as exc:  # noqa: BLE001 -- one bad file shouldn't kill the scan
                failures.append((path, str(exc)))

    unknown_macros = [(n, c) for n, c in macro_counts.most_common() if n not in _KNOWN_MACROS]
    unknown_envs = [(n, c) for n, c in env_counts.most_common() if n not in _KNOWN_ENVIRONMENTS]

    typer.echo(f"\n{len(unknown_macros)} unknown macros, {len(unknown_envs)} unknown environments\n")

    typer.echo("--- unknown macros (by frequency) ---")
    for name, count in unknown_macros[:top_n]:
        typer.echo(f"  {count:>5}x  \\{name}   (e.g. {macro_examples[name]})")

    typer.echo("\n--- unknown environments (by frequency) ---")
    for name, count in unknown_envs[:top_n]:
        typer.echo(f"  {count:>5}x  {name}   (e.g. {env_examples[name]})")

    if failures:
        typer.echo(f"\n{len(failures)} file(s) failed to parse entirely:")
        for path, error in failures[:10]:
            typer.echo(f"  {path}: {error}")


if __name__ == "__main__":
    app()