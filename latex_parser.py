"""Structural parser for Espressif TRM LaTeX source, built on pylatexenc.

Unlike the earlier hand-rolled brace scanner, this uses pylatexenc's
LatexWalker to build a real parse tree, extended with specs for the TRM's
custom macro suite (register/regfield/reglist, etc.) plus common packages
(hyperref, multirow, longtable, threeparttable) it doesn't know about out of
the box.

The node tree is turned into ``chunking.SectionNode``/``chunking.Block`` and
handed to the shared assembler, so TRM chunks come out the same shape and size
distribution as the ESP-IDF ones. Three kinds of content feed it:

  - sections   (\\chapter ... \\subparagraph -> the SectionNode hierarchy)
  - prose      (everything else -> ordinary Blocks)
  - registers  (\\begin{register} -> ``Block(atomic=True)``; see _build_tree)
  - tables     (longtable/tabular -> a flat row-per-line Block)

Two build-time behaviours have to be reproduced *before* pylatexenc sees the
source, because neither survives a per-file parse:

  - **File inclusion.** Register content lives exclusively in files pulled in
    via ``\\subfile``/``\\input``/``\\subfileinclude``, along paths built from
    ``\\def``'d macros. Parsing a chapter file on its own yields prose with a
    register-shaped hole in it and nothing to signal the hole is there.

  - **Tag selection.** ``\\tagged``/``\\untagged``/``\\iftagged`` gate content on
    the active tag set, which the preamble seeds from the chip name. This is
    the TRM's analogue of ESP-IDF's ``only::`` and has the same failure mode:
    flattening both branches produces one chunk asserting two contradictory
    things. It also *has* to be resolved textually rather than on the node
    tree, because a gated branch may open a ``register`` environment its
    sibling closes -- ``ESP32-P4/56-LEDPWM/56-LEDPWM-reg__EN.tex`` has 27
    ``\\begin{register}`` against 20 ``\\end{register}`` and only balances once a
    branch is chosen.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from pathlib import Path

from pydantic import BaseModel
from pylatexenc.latexwalker import (
    LatexCharsNode,
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexMathNode,
    LatexSpecialsNode,
    LatexWalker,
    get_default_latex_context_db,
)
from pylatexenc.macrospec import EnvironmentSpec, MacroSpec, VerbatimArgsParser

from chunking import (
    MAX_WORDS_PER_CHUNK,
    MIN_WORDS_PER_CHUNK,
    Block,
    RawChunk,
    SectionNode,
    build_chunks,
    merge_undersized_chunks,
)


class _NamedVerbatimArgsParser(VerbatimArgsParser):
    """VerbatimArgsParser for an environment other than ``verbatim`` itself.

    pylatexenc's version hardcodes a search for the literal ``\\end{verbatim}``,
    so applying it to ``lstlisting`` raises and (under tolerant parsing) leaves
    the body to be read as LaTeX -- where ``%`` starts a comment and ``_`` is a
    macro. Overriding the terminator is the whole of the fix.
    """

    def __init__(self, environment_name: str):
        super().__init__(verbatim_arg_type="verbatim-environment")
        self._end = f"\\end{{{environment_name}}}"

    def parse_args(self, w, pos, parsing_state=None):
        from pylatexenc import latexwalker
        from pylatexenc.macrospec._argparsers import ParsedVerbatimArgs

        end = w.s.find(self._end, pos)
        if end == -1:
            return super().parse_args(w, pos, parsing_state=parsing_state)
        length = end - pos
        argd = ParsedVerbatimArgs(
            verbatim_chars_node=w.make_node(
                latexwalker.LatexCharsNode,
                parsing_state=parsing_state,
                chars=w.s[pos : pos + length],
                pos=pos,
                len=length,
            )
        )
        return (argd, pos, length)


SECTION_LEVELS = {
    "chapter": 0,
    "section": 1,
    "subsection": 2,
    "subsubsection": 3,
    "paragraph": 4,
    "subparagraph": 5,
}

# Macros/environments used by the TRM build that aren't in plain LaTeX --
# either the custom register-diagram suite, or common packages (hyperref,
# multirow, threeparttable, listings) pylatexenc doesn't ship specs for.
#
# Registering a spec matters even for macros whose output we discard: without
# one, pylatexenc leaves the argument as a following group node and the
# generic fallback renders it as stray text (``\textcolor{red}{x}`` becoming
# "redx", ``\bitbox{8}{FIELD}`` becoming "8FIELD").
EXTRA_MACROS = [
    MacroSpec("hyperref", "[{"),
    MacroSpec("nameref", "{"),
    MacroSpec("regindex", "{"),
    MacroSpec("reglabel", "{"),
    MacroSpec("regnewline", ""),
    MacroSpec("regfield", "{{{{"),
    MacroSpec("regfieldrotate", "{{{{"),
    MacroSpec("makecell", "[{"),
    MacroSpec("thead", "[{"),
    MacroSpec("cline", "{"),
    MacroSpec("rowcolor", "[{"),
    MacroSpec("cellcolor", "[{"),
    MacroSpec("multicolumn", "{{{"),
    MacroSpec("multirow", "{{{"),  # width arg is sometimes a bare "*" token, not braced -- best effort
    # Cosmetic wrappers: the inner text is content, the styling arguments are not.
    MacroSpec("textcolor", "{{"),
    MacroSpec("color", "{"),
    MacroSpec("rotatebox", "[{{"),
    MacroSpec("fbox", "{"),
    MacroSpec("colorbox", "{{"),
    MacroSpec("uppercase", "{"),
    MacroSpec("texorpdfstring", "{{"),
    MacroSpec("phantom", "{"),
    MacroSpec("hypertarget", "{{"),
    MacroSpec("href", "{{"),
    MacroSpec("includegraphics", "[{"),
    MacroSpec("tnote", "[{"),
    MacroSpec("insertTableNotes", ""),
    MacroSpec("bitbox", "[{{"),
    # longtable's repeated-header machinery; consumed structurally by _render_table.
    MacroSpec("endfirsthead", ""),
    MacroSpec("endhead", ""),
    MacroSpec("endfoot", ""),
    MacroSpec("endlastfoot", ""),
    # Counter/length fiddling that must not leak its arguments into the text.
    MacroSpec("setcounter", "{{"),
    MacroSpec("addtocounter", "{{"),
    MacroSpec("setlength", "{{"),
    MacroSpec("arraystretch", ""),
    MacroSpec("tabcolsep", ""),
    MacroSpec("textwidth", ""),
    # Tag conditionals. Resolved textually in the pre-pass, but specs are kept
    # so that any stragglers (e.g. a caller parsing raw source) don't spill
    # their tag names into the rendered text.
    MacroSpec("tagged", "{{"),
    MacroSpec("untagged", "{{"),
    MacroSpec("iftagged", "{{{"),
    MacroSpec("usetag", "{"),
    MacroSpec("GetTranslation", "{"),
    MacroSpec("frac", "{{"),
    MacroSpec("textcircled", "{"),
    MacroSpec("hyperlink", "{{"),
    MacroSpec("itemsep", ""),
    MacroSpec("phantomsection", ""),
    MacroSpec("restoregeometry", ""),
    MacroSpec("Navigation", ""),
    # \lstinline|...| is \verb by another name; without a verbatim parser its
    # delimiters leak into the text as literal pipes.
    MacroSpec("lstinline", args_parser=VerbatimArgsParser(verbatim_arg_type="verb-macro")),
    MacroSpec("verb", args_parser=VerbatimArgsParser(verbatim_arg_type="verb-macro")),
    # Consumed by the text pre-pass. Specs are listed so a raw parse degrades
    # predictably, and so latex_coverage_check.py stops reporting them as gaps
    # it has no way to know are already handled.
    MacroSpec("subfile", "{"),
    MacroSpec("subfileinclude", "{"),
    MacroSpec("input", "{"),
    MacroSpec("myexternaldocument", "{"),
    MacroSpec("InputIfFileExists", "{{{"),
    MacroSpec("documentclass", "[{"),
    MacroSpec("usepackage", "[{"),
    MacroSpec("newif", ""),
    MacroSpec("globaltrue", ""),
    MacroSpec("globalfalse", ""),
    MacroSpec("ifglobal", ""),
    MacroSpec("else", ""),
    MacroSpec("fi", ""),
]

# Symbols and \def'd values take no arguments; registering them keeps the
# generic fallback from treating a following group as an argument.
EXTRA_MACROS += [MacroSpec(name, "") for name in ("chipname", "chipseries", "modulename", "hexprefix", "docversion")]

EXTRA_ENVIRONMENTS = [
    EnvironmentSpec("register", "{{{"),
    EnvironmentSpec("regdesc", ""),
    EnvironmentSpec("reglist", ""),
    EnvironmentSpec("longtable", "[{"),
    EnvironmentSpec("footnotesize", ""),
    # A second table family beyond longtable/tabular: a plain tabular plus a
    # tablenotes list of footnotes keyed by \tnote markers.
    EnvironmentSpec("threeparttable", "[{"),
    EnvironmentSpec("ThreePartTable", ""),
    EnvironmentSpec("tablenotes", "["),
    EnvironmentSpec("TableNotes", ""),
    # Espressif's callout boxes -- ordinary prose in a coloured frame.
    EnvironmentSpec("tiplisting", ""),
    EnvironmentSpec("tiplistinga", ""),
    EnvironmentSpec("importantlisting", ""),
    EnvironmentSpec("landscape", ""),
    EnvironmentSpec("multicols", "{"),
    EnvironmentSpec("bytefield", "[{"),
    EnvironmentSpec("leftwordgroup", "{"),
    EnvironmentSpec("figure", "["),
    EnvironmentSpec("figure*", "["),
    EnvironmentSpec("table", "["),
    EnvironmentSpec("math", ""),
    EnvironmentSpec("displaymath", ""),
    EnvironmentSpec("equation", ""),
    EnvironmentSpec("equation*", ""),
    EnvironmentSpec("gather", ""),
    EnvironmentSpec("gather*", ""),
    EnvironmentSpec("array", "{"),
    EnvironmentSpec("cases", ""),
    EnvironmentSpec("minipage", "[{"),
    EnvironmentSpec("tikzpicture", "["),
    # Code listings are verbatim: without this pylatexenc reads their body as
    # LaTeX, so "%" starts a comment and "\n" is a line break, and the listing
    # comes out shredded.
    EnvironmentSpec("lstlisting", args_parser=_NamedVerbatimArgsParser("lstlisting")),
]


def build_context_db():
    """Default LaTeX context plus specs for the TRM's custom macro suite."""
    db = get_default_latex_context_db()
    db.add_context_category("trm", prepend=True, macros=EXTRA_MACROS, environments=EXTRA_ENVIRONMENTS)
    return db


def parse(text: str):
    """Parse TeX source into a pylatexenc node list."""
    walker = LatexWalker(text, latex_context=build_context_db(), tolerant_parsing=True)
    nodelist, _, _ = walker.get_latex_nodes()
    return nodelist


# ---------------------------------------------------------------------------
# Text pre-pass: comments, preamble, \ifglobal, tag selection, file inclusion.
#
# All of this runs on raw text rather than on the node tree. That is deliberate
# and not laziness: a tag-gated branch can straddle an environment boundary, so
# there is no valid tree to walk until the branch has been chosen.
# ---------------------------------------------------------------------------


def _mask_comments(text: str) -> str:
    """Blank out LaTeX comments, preserving length so offsets stay usable.

    Every scan below matches against the mask and slices from the original, so
    a commented-out ``\\subfileinclude`` is never followed and verbatim content
    is never rewritten. (``ESP32-P4-main__EN.tex`` really does carry a
    commented-out chapter include.)
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2  # escaped character: \% is a literal percent, not a comment
            continue
        if ch == "%":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        i += 1
    return "".join(out)


def _match_brace(masked: str, start: int) -> tuple[int, int, int]:
    """Given masked[start] == '{', return (inner_start, inner_end, index_after)."""
    depth = 0
    i = start
    n = len(masked)
    while i < n:
        ch = masked[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
            if depth == 1:
                inner = i + 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return inner, i, i + 1
        i += 1
    raise ValueError("unbalanced braces")


def _skip_space(masked: str, i: int) -> int:
    while i < len(masked) and masked[i] in " \t\r\n":
        i += 1
    return i


_DOC_BEGIN_RE = re.compile(r"\\begin\s*\{document\}")
_DOC_END_RE = re.compile(r"\\end\s*\{document\}")


def document_body(text: str) -> str:
    """Strip the preamble, which is build scaffolding rather than content.

    Included files mostly have no ``\\begin{document}`` at all (1,156 of the
    1,550 English files), so absence means "this is already a body fragment",
    not "this file is empty".
    """
    masked = _mask_comments(text)
    begin = _DOC_BEGIN_RE.search(masked)
    if not begin:
        return text
    end = _DOC_END_RE.search(masked, begin.end())
    return text[begin.end() : end.start() if end else len(text)]


# \ifglobal selects between a subfiles documentclass and a standalone book
# class; \globaltrue is set in every chapter file, so the true branch wins.
# \iffalse is used as a block comment. Neither gates real content, but the
# \else branch of \ifglobal pulls in a table of contents and todo lists that
# would otherwise land in the extracted text.
_CONDITIONALS = {"ifglobal": True, "iffalse": False, "iftrue": True}
_IF_TOKEN_RE = re.compile(r"\\(if[a-zA-Z]*|else|fi)")


def resolve_conditionals(text: str) -> str:
    """Evaluate the ``\\ifglobal``/``\\iffalse`` blocks left in a document body."""
    masked = _mask_comments(text)
    for match in _IF_TOKEN_RE.finditer(masked):
        name = match.group(1)
        if name not in _CONDITIONALS:
            continue
        try:
            true_branch, false_branch, end = _split_conditional(masked, match.end())
        except ValueError:
            continue  # unbalanced \if...\fi: leave the text alone rather than truncate it
        keep = true_branch if _CONDITIONALS[name] else false_branch
        rebuilt = text[: match.start()] + text[keep[0] : keep[1]] + text[end:]
        return resolve_conditionals(rebuilt)
    return text


def _split_conditional(masked: str, start: int) -> tuple[tuple[int, int], tuple[int, int], int]:
    """Locate the \\else and \\fi matching a conditional opened just before start."""
    depth = 1
    else_at = None
    for match in _IF_TOKEN_RE.finditer(masked, start):
        name = match.group(1)
        if name == "fi":
            depth -= 1
            if depth == 0:
                true_end = else_at[0] if else_at else match.start()
                false_span = (else_at[1], match.start()) if else_at else (match.start(), match.start())
                return (start, true_end), false_span, match.end()
        elif name == "else":
            if depth == 1:
                else_at = (match.start(), match.end())
        elif name.startswith("if") and name not in ("iftagged", "ifthenelse"):
            depth += 1
    raise ValueError("unterminated conditional")


_TAG_RE = re.compile(r"\\(iftagged|untagged|tagged)\s*\{")


def select_tagged(text: str, tags: frozenset[str], defs: dict[str, str]) -> str:
    """Evaluate ``\\tagged``/``\\untagged``/``\\iftagged`` against the active tag set.

    ``\\usetag{\\chipseries}`` in ``00-shared/config/preamble-trm-repo.sty`` makes
    the chip name a tag for every document, and chip-revision variants add
    their own (``ESP32-P4-main__EN.tex`` adds ``ESP32-P4-latest``). A tag may
    name a *different* chip than the directory the file sits in -- ESP32-P4's
    LEDPWM registers test ``\\iftagged{ESP32-C61}`` -- so "tag matches the
    containing directory" is not a shortcut that works; the tag set has to come
    from the document being built.
    """
    masked = _mask_comments(text)
    parts: list[str] = []
    pos = 0
    while True:
        match = _TAG_RE.search(masked, pos)
        if not match:
            break
        parts.append(text[pos : match.start()])
        name = match.group(1)
        try:
            cursor = match.end() - 1
            tag_start, tag_end, cursor = _match_brace(masked, cursor)
            yes_start, yes_end, cursor = _match_brace(masked, _skip_space(masked, cursor))
            if name == "iftagged":
                no_start, no_end, cursor = _match_brace(masked, _skip_space(masked, cursor))
            else:
                no_start = no_end = yes_end
        except (ValueError, IndexError):
            parts.append(text[match.start() : match.end()])
            pos = match.end()
            continue

        wanted = {t.strip() for t in _expand_defs(text[tag_start:tag_end], defs).split(",")}
        active = bool(wanted & set(tags))
        if name == "untagged":
            active = not active
        chosen = text[yes_start:yes_end] if active else text[no_start:no_end]
        parts.append(select_tagged(chosen, tags, defs))
        pos = cursor
    parts.append(text[pos:])
    return "".join(parts)


# \def\name{...}, \newcommand\name{...} and \newcommand{\name}{...}. The
# trailing brace is matched separately so the body can be read with brace
# counting; a regex can't do that.
_DEF_RE = re.compile(
    r"\\(?:def|newcommand|renewcommand|providecommand)\s*\*?\s*"
    r"(?:\{\s*\\([A-Za-z@]+)\s*\}|\\([A-Za-z@]+))\s*"
    r"(?:\[\d+\])?\s*(?:\[[^\]]*\])?\s*\{"
)
_USETAG_RE = re.compile(r"\\usetag\s*\{")


def collect_definitions(text: str, into: dict[str, str] | None = None) -> dict[str, str]:
    """Harvest zero-argument ``\\def``/``\\newcommand`` values.

    Two distinct jobs depend on this: expanding ``\\modulefiles`` and friends in
    include paths, and expanding value macros like ``\\chipname`` at render
    time. Parameterised macros are skipped -- they are formatting helpers, and
    a body containing ``#1`` is not a value.
    """
    defs = into if into is not None else {}
    masked = _mask_comments(text)
    for match in _DEF_RE.finditer(masked):
        name = match.group(1) or match.group(2)
        try:
            start, end, _ = _match_brace(masked, match.end() - 1)
        except ValueError:
            continue
        body = text[start:end].strip()
        if "#" in body:
            continue
        defs[name] = body
    return defs


def extract_body_definitions(text: str) -> tuple[str, dict[str, str]]:
    """Lift ``\\newcommand``/``\\def`` out of a document body, returning both.

    These have to be removed and expanded textually rather than left for
    pylatexenc, because a TRM body macro is routinely a *fragment*: the GPIO
    chapters define ``\\tableheaderHPSignalEN`` as ``\\begin{small}\\begin{longtable}
    ...\\endhead`` with no matching ``\\end``, closed only where the macro is
    used. Handed that as a macro argument, pylatexenc keeps scanning for the
    missing ``\\end{small}`` and swallows the remainder of the file -- which is
    how 221 registers went missing with no error and no drop in word count.
    """
    masked = _mask_comments(text)
    defs: dict[str, str] = {}
    parts: list[str] = []
    pos = 0
    for match in _DEF_RE.finditer(masked):
        if match.start() < pos:
            continue
        try:
            start, end, cursor = _match_brace(masked, match.end() - 1)
        except ValueError:
            continue
        name = match.group(1) or match.group(2)
        body = text[start:end]
        if "#" not in body:
            defs[name] = body.strip()
        parts.append(text[pos : match.start()])
        pos = cursor
    parts.append(text[pos:])
    return "".join(parts), defs


def collect_tags(text: str, defs: dict[str, str]) -> set[str]:
    """Read ``\\usetag{...}`` declarations out of a preamble."""
    masked = _mask_comments(text)
    tags: set[str] = set()
    for match in _USETAG_RE.finditer(masked):
        try:
            start, end, _ = _match_brace(masked, match.end() - 1)
        except ValueError:
            continue
        tags.update(t.strip() for t in _expand_defs(text[start:end], defs).split(",") if t.strip())
    return tags


_MACRO_USE_RE = re.compile(r"\\([A-Za-z@]+)\s*(?:\{\})?")


def _expand_defs(text: str, defs: dict[str, str], depth: int = 0) -> str:
    """Substitute known zero-argument macro values, e.g. ``\\modulefiles`` -> ``./01-I2C``."""
    if depth > 8 or "\\" not in text:
        return text

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in defs:
            return match.group(0)
        return _expand_defs(defs[name], defs, depth + 1)

    return _MACRO_USE_RE.sub(replace, text)


_INCLUDE_RE = re.compile(r"\\(subfileinclude|subfile|input)\s*\{")

# Where to look for an include that isn't relative to the including file.
# Chapter files reference ``./NN-MODULE/tables/...`` from the chip root, and
# main files pull shared front matter out of the repo-level directories.
_SHARED_DIRS = ("00-trm-shared", "00-shared", "00-shared/front-end-matter", "00-shared/config")


class ResolvedDocument(BaseModel):
    """A chapter file with every include spliced in, plus what that cost."""

    text: str
    root: str
    files: list[str] = []  # every file inlined, including the root, in visit order
    missing: list[str] = []  # "<including file>: <as written> -> <after macro expansion>"
    tags: list[str] = []


def repo_root_for(path: Path) -> Path | None:
    """Walk up from a .tex file to the TRM checkout root (the dir holding 00-shared)."""
    for parent in path.resolve().parents:
        if (parent / "00-shared").is_dir():
            return parent
    return None


def chip_dir_for(path: Path) -> Path | None:
    """The chip directory (``ESP32-C3`` etc.) a file belongs to, if any."""
    root = repo_root_for(path)
    if root is None:
        return None
    resolved = path.resolve()
    for parent in [resolved, *resolved.parents]:
        if parent.parent == root and parent.name not in ("00-shared", "00-trm-shared"):
            return parent
    return None


def _preamble_context(path: Path) -> tuple[dict[str, str], set[str]]:
    """Seed definitions and tags from the .sty files the build would load.

    ``\\chipname`` lives in ``<chip>/00-chip-spec-content/chip-spec-settings.sty``
    and appears 1,672 times in content. Left unexpanded it renders to nothing
    at all, which is the TRM's version of the ``{IDF_TARGET_*}`` placeholder
    problem -- except worse, because the sentence silently loses its subject
    rather than keeping a visibly wrong token.
    """
    defs: dict[str, str] = {}
    root = repo_root_for(path)
    chip = chip_dir_for(path)
    candidates = []
    if root is not None:
        candidates.append(root / "00-shared" / "config" / "preamble-trm-repo.sty")
    if chip is not None:
        candidates.append(chip / "00-chip-spec-content" / "chip-spec-settings.sty")
    for sty in candidates:
        if sty.is_file():
            collect_definitions(sty.read_text(encoding="utf-8", errors="replace"), defs)
    # \usetag{\chipseries} -> \chipname: the chip's own name is always a tag.
    tags = {defs[name] for name in ("chipname",) if name in defs}
    return defs, tags


def _resolve_include_path(raw: str, including: Path, root: Path | None, chip: Path | None) -> Path | None:
    raw = raw.strip()
    if not raw:
        return None
    names = [raw] if raw.endswith(".tex") else [raw + ".tex", raw]
    search = [including.parent]
    if chip is not None:
        search.append(chip)
    if root is not None:
        search.extend(root / d for d in _SHARED_DIRS)
    for base in search:
        for name in names:
            candidate = base / name
            if candidate.is_file():
                return candidate.resolve()
    return None


def resolve_document(
    path: Path | str,
    *,
    extra_tags: frozenset[str] | None = None,
) -> ResolvedDocument:
    """Inline every ``\\subfile``/``\\input``/``\\subfileinclude`` under a chapter file.

    Missing includes are recorded rather than dropped: ESP32's register files
    are named ``i2c_reg_en.tex`` rather than ``*__EN.tex``, and a silent skip
    there would cost a whole chip's registers while the totals still looked
    plausible.
    """
    path = Path(path)
    defs, tags = _preamble_context(path)
    if extra_tags:
        tags |= set(extra_tags)
    doc = ResolvedDocument(text="", root=str(path))
    text = _inline(path, defs, tags, doc, ())
    doc.text = text
    doc.tags = sorted(tags)
    return doc


def _inline(path: Path, defs: dict[str, str], tags: set[str], doc: ResolvedDocument, stack: tuple[Path, ...]) -> str:
    source = path.read_text(encoding="utf-8", errors="replace")
    doc.files.append(str(path))

    defs = dict(defs)
    collect_definitions(source, defs)
    tags = tags | collect_tags(source, defs)

    body = resolve_conditionals(document_body(source))
    body = select_tagged(body, frozenset(tags), defs)
    body, body_defs = extract_body_definitions(body)
    if body_defs:
        body = _expand_defs(body, body_defs)

    root = repo_root_for(path)
    chip = chip_dir_for(path)
    masked = _mask_comments(body)

    parts: list[str] = []
    pos = 0
    while True:
        match = _INCLUDE_RE.search(masked, pos)
        if not match:
            break
        parts.append(body[pos : match.start()])
        try:
            start, end, cursor = _match_brace(masked, match.end() - 1)
        except ValueError:
            parts.append(body[match.start() : match.end()])
            pos = match.end()
            continue
        raw = body[start:end]
        expanded = _expand_defs(raw, defs)
        target = _resolve_include_path(expanded, path, root, chip)
        if target is None:
            doc.missing.append(f"{path}: {raw.strip()} -> {expanded.strip()}")
        elif target in stack:
            doc.missing.append(f"{path}: cycle -> {target}")
        else:
            parts.append("\n")
            parts.append(_inline(target, defs, tags, doc, (*stack, path.resolve())))
            parts.append("\n")
        pos = cursor
    parts.append(body[pos:])
    return "".join(parts)


# ---------------------------------------------------------------------------
# Generic text rendering -- replaces the old regex-based _clean_latex.
# Walks any node/nodelist and produces plain, readable text.
# ---------------------------------------------------------------------------

_INLINE_TEXT_MACROS = {
    "textit",
    "textbf",
    "emph",
    "textsuperscript",
    "nameref",
    "makecell",
    "thead",
    "multirow",
    "multicolumn",
    "regindex",  # \regindex{n} is part of the register name: LEDC_CH\regindex{n}_CONF0_REG
    "uppercase",
    "fbox",
    "underline",
    "texttt",
    "textsc",
    "mbox",
    "textcircled",
    "text",
    "mathrm",
    "boldsymbol",
    "overline",
    "widetilde",
    "hat",
    "bar",
}

# Styling declarations and layout directives. Their arguments (where they have
# any, per EXTRA_MACROS) carry no information a reader or an embedding needs.
_SKIPPED_MACROS = {
    "label",
    "regnewline",
    "regindex_",
    "cline",
    "hline",
    "newpage",
    "clearpage",
    "rowcolor",
    "cellcolor",
    "color",
    "centering",
    "raggedright",
    "raggedleft",
    "raggedbottom",
    "bfseries",
    "itshape",
    "it",
    "bf",
    "rm",
    "sf",
    "tt",
    "scriptsize",
    "footnotesize",
    "normalsize",
    "tiny",
    "small",
    "large",
    "Large",
    "LARGE",
    "huge",
    "Huge",
    "includegraphics",
    "tnote",  # footnote marker; the note text itself comes through tablenotes
    "insertTableNotes",
    "endfirsthead",
    "endhead",
    "endfoot",
    "endlastfoot",
    "setcounter",
    "addtocounter",
    "setlength",
    "arraystretch",
    "tabcolsep",
    "textwidth",
    "linewidth",
    "relax",
    "let",
    "expandafter",
    "romannumeral",
    "phantom",
    "vspace",
    "hspace",
    "noindent",
    "thetable",
    "thechapter",
    "thesection",
    "tablename",
    "figurename",
    "listoftodos",
    "listofdonetodos",
    "tableofcontents",
    "selectlanguage",
    "usetag",
    "markboth",
    "fancyhead",
    "toprule",
    "midrule",
    "bottomrule",
    "-",  # \- is a discretionary hyphen
}

# Symbols that carry meaning in TRM prose and would otherwise vanish.
_SYMBOL_MACROS = {
    "dots": "...",
    "ldots": "...",
    "cdots": "...",
    "times": "x",
    "cdot": "*",
    "in": " in ",
    "prime": "'",
    "bmod": " mod ",
    "leq": "<=",
    "geq": ">=",
    "neq": "!=",
    "pm": "+/-",
    "ge": ">=",
    "le": "<=",
    "textasciitilde": "~",
    "textasciicircum": "^",
    "textbackslash": "\\",
    "alpha": "alpha",
    "beta": "beta",
    "gamma": "gamma",
    "delta": "delta",
    "mu": "u",
    "pi": "pi",
    "sigma": "sigma",
    "theta": "theta",
    "lambda": "lambda",
    "Delta": "delta",
    "Omega": "ohm",
    "infty": "infinity",
    "approx": "~=",
    "equiv": "==",
    "rightarrow": "->",
    "leftarrow": "<-",
    "to": "->",
    "quad": " ",
    "qquad": " ",
    ",": " ",
    ";": " ",
    ":": " ",
    "!": "",
    "/": "",
    "@": "",
}

# Row separator. \\ has to stay distinguishable from the newlines that source
# line-wrapping puts inside a single table cell -- splitting rows on "\n" turns
# every wrapped cell into a spurious row.
_ROW_SEP = "\uE000"
_CELL_SEP = "\uE001"


def render_nodes(nodes) -> str:
    """Render a pylatexenc node or nodelist to plain text."""
    if nodes is None:
        return ""
    if not isinstance(nodes, (list, tuple)):
        nodes = [nodes]

    parts = []
    for node in nodes:
        parts.append(_render_node(node))
    text = "".join(parts)
    return text


def _finalize(text: str) -> str:
    """Turn internal row markers back into newlines and tidy whitespace."""
    text = text.replace(_ROW_SEP, "\n").replace(_CELL_SEP, " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _render_node(node) -> str:
    if isinstance(node, LatexCharsNode):
        return node.chars

    if isinstance(node, LatexGroupNode):
        return render_nodes(node.nodelist)

    if isinstance(node, LatexSpecialsNode):
        # "&" is the tabular cell separator and has to survive rendering, or
        # _render_table has nothing to split on and emits every row as one
        # run-on cell. _CELL_SEP keeps it distinct from a literal "\&".
        return {"&": _CELL_SEP, "~": " ", "\\&": "&", "``": '"', "''": '"', "--": "-", "---": "-"}.get(
            node.specials_chars, ""
        )

    if isinstance(node, LatexMathNode):
        # TRM prose carries real inline math -- "$R/\overline W$" in the I2C
        # overview names a signal. Dropping math nodes loses it silently.
        return render_nodes(node.nodelist)

    if isinstance(node, LatexMacroNode):
        return _render_macro(node)

    if isinstance(node, LatexEnvironmentNode):
        return _render_environment(node)

    return ""


_ESCAPED_CHARS = {"_": "_", " ": " ", "%": "%", "&": "&", "#": "#", "{": "{", "}": "}", "$": "$"}

_VALUE_MACROS: ContextVar[dict[str, str]] = ContextVar("_VALUE_MACROS", default={})
_ACTIVE_TAGS: ContextVar[frozenset[str]] = ContextVar("_ACTIVE_TAGS", default=frozenset())

# Path-valued macros exist only to build include arguments; if one reaches the
# renderer it means an include went unresolved, and "./01-I2C" is not prose.
_NON_VALUE_MACROS = {"modulefiles", "feedbacklink", "docpathlatestEN", "docpathlatestCN", "patheolEN", "patheolCN"}


def _render_macro(node: LatexMacroNode) -> str:
    name = node.macroname

    if name in _ESCAPED_CHARS:
        return _ESCAPED_CHARS[name]
    if name == "\\":
        return _ROW_SEP
    if name in _SKIPPED_MACROS:
        return ""
    if name in _SYMBOL_MACROS:
        return _SYMBOL_MACROS[name]
    if name in ("verb", "lstinline"):
        # Inline verbatim: pylatexenc parses the delimiter for us and hands
        # back the raw text, which is usually a register or field name.
        return getattr(node.nodeargd, "verbatim_text", "") or ""
    if name == "frac":
        args = node.nodeargd.argnlist if node.nodeargd else []
        return f"{render_nodes(args[0])}/{render_nodes(args[1])}" if len(args) > 1 else ""
    if name in ("hyperref", "href", "textcolor", "colorbox", "hypertarget", "hyperlink"):
        # [label]{visible text} / {url}{visible text} -- only the last arg is content.
        args = node.nodeargd.argnlist if node.nodeargd else []
        return render_nodes(args[-1]) if args else ""
    if name == "rotatebox":
        args = node.nodeargd.argnlist if node.nodeargd else []
        return render_nodes(args[-1]) if args else ""
    if name == "texorpdfstring":
        args = node.nodeargd.argnlist if node.nodeargd else []
        return render_nodes(args[0]) if args else ""
    if name in ("tagged", "untagged", "iftagged"):
        # Normally resolved in the pre-pass; this is the fallback for callers
        # that parse raw source. Never render the tag-list argument itself.
        return _render_tag_macro(node)
    if name == "reglabel":
        return ""  # diagram row label ("Reset"), not useful outside the bitfield diagram
    if name == "bitbox":
        args = node.nodeargd.argnlist if node.nodeargd else []
        return f"{render_nodes(args[-1]).strip()} " if args else ""
    if name in _INLINE_TEXT_MACROS:
        args = node.nodeargd.argnlist if node.nodeargd else []
        return render_nodes(args[-1]) if args else ""
    if name == "item":
        return ""  # handled structurally by callers that split on \item, not rendered inline

    values = _VALUE_MACROS.get()
    if name in values and name not in _NON_VALUE_MACROS:
        return values[name]

    # Unknown/unregistered macro: best effort, render any parsed args' content.
    args = node.nodeargd.argnlist if node.nodeargd else []
    return render_nodes([a for a in args if a is not None])


def _render_tag_macro(node: LatexMacroNode) -> str:
    args = node.nodeargd.argnlist if node.nodeargd else []
    if not args:
        return ""
    wanted = {t.strip() for t in render_nodes(args[0]).split(",")}
    active = bool(wanted & set(_ACTIVE_TAGS.get()))
    if node.macroname == "untagged":
        active = not active
    if active:
        return render_nodes(args[1]) if len(args) > 1 else ""
    return render_nodes(args[2]) if len(args) > 2 else ""


# Environments that are pure containers: their children are content and should
# be handled by whatever handles content, not swallowed by the wrapper.
_TRANSPARENT_ENVIRONMENTS = {
    "document",
    "footnotesize",
    "scriptsize",
    "tiny",
    "small",
    "normalsize",
    "large",
    "Large",
    "center",
    "centering",
    "flushleft",
    "flushright",
    "landscape",
    "multicols",
    "minipage",
    "table",
    "table*",
    "threeparttable",
    "ThreePartTable",
    "tiplisting",
    "tiplistinga",
    "importantlisting",
    "notelisting",
    "spacing",
}

_ITEM_ENVIRONMENTS = {"itemize", "enumerate", "description", "reglist", "tablenotes", "TableNotes"}
_MATH_ENVIRONMENTS = {"math", "displaymath", "equation", "equation*", "gather", "gather*", "align", "align*", "array", "cases"}
_DROPPED_ENVIRONMENTS = {"tikzpicture", "leftwordgroup"}


def _render_environment(node: LatexEnvironmentNode) -> str:
    name = node.environmentname
    if name in _DROPPED_ENVIRONMENTS:
        return ""
    if name in ("lstlisting", "verbatim", "verbatim*"):
        return getattr(node.nodeargd, "verbatim_text", "") or render_nodes(node.nodelist)
    if name in _ITEM_ENVIRONMENTS:
        return _render_item_list(node.nodelist)
    if name in ("figure", "figure*"):
        # Images are unrecoverable from source, but 97% of figures carry a
        # caption and that caption is real prose about the diagram.
        return _render_caption(node.nodelist)
    if name in ("longtable", "tabular", "tabularx"):
        return _render_table(node)
    if name == "register":
        return _render_register(node)[1]
    if name == "bytefield":
        # Instruction encodings and DMA descriptor layouts, always inside a
        # figure. Not a second register representation -- 31 uses repo-wide.
        return render_nodes(node.nodelist)
    if name in _MATH_ENVIRONMENTS:
        return render_nodes(node.nodelist)
    return render_nodes(node.nodelist)


def _render_caption(nodelist) -> str:
    """Pull \\caption text out of a float, ignoring the graphics around it."""
    captions = []
    for node in nodelist or []:
        if isinstance(node, LatexMacroNode) and node.macroname == "caption":
            args = node.nodeargd.argnlist if node.nodeargd else []
            if args:
                captions.append(render_nodes(args[-1]).strip())
        elif isinstance(node, LatexEnvironmentNode):
            nested = _render_caption(node.nodelist)
            if nested:
                captions.append(nested)
    return "\n".join(c for c in captions if c)


def _render_item_list(nodelist) -> str:
    """Render \\item entries (with optional [label]) as '- ' bullet lines."""
    lines = []
    current: list = []
    label = None

    def flush():
        text = _finalize(render_nodes(current))
        if text or label:
            prefix = f"[{render_nodes(label).strip()}] " if label else ""
            lines.append(f"- {prefix}{text}")

    for node in nodelist:
        if isinstance(node, LatexMacroNode) and node.macroname == "item":
            flush()
            current = []
            args = node.nodeargd.argnlist if node.nodeargd else []
            label = args[0] if args and args[0] is not None else None
        else:
            current.append(node)
    flush()
    return "\n".join(lines)


def _section_title(node: LatexMacroNode) -> str:
    args = node.nodeargd.argnlist if node.nodeargd else []
    return _finalize(render_nodes(args[-1])) if args else ""


def _render_one_line(nodelist) -> str:
    """Render an argument that must stay on a single output line.

    A register's name, address and bitfield labels are header fields, not
    prose: they are consumed line-wise downstream ("Register X at address Y"
    is matched as one line). Source authors wrap long ones across lines --
    ``{MCPWM\\_TIMER\\regindex{n}\\_SYNC\\_REG\\n(\\regindex{n}: 0-2)}`` -- and
    _finalize preserves that newline, which splits the header so the address
    lands on a second line and address-keyed checks read the register as
    having none. Collapse the wrap; it carries no information here.
    """
    return re.sub(r"\s*\n\s*", " ", _finalize(render_nodes(nodelist))).strip()


def _render_register(node: LatexEnvironmentNode) -> tuple[str, str]:
    """Render a register environment to (register_name, text)."""
    args = node.nodeargd.argnlist if node.nodeargd else []
    name = _render_one_line(args[1]) if len(args) > 1 else "unknown"
    address = _render_one_line(args[2]) if len(args) > 2 else "unknown"

    field_lines = []
    field_descs: dict[str, str] = {}

    for child in node.nodelist:
        # \regfieldrotate is \regfield with a rotated label; same four
        # arguments, and it accounts for 734 fields that would otherwise be
        # rendered as a run of digits glued to the field name.
        if isinstance(child, LatexMacroNode) and child.macroname in ("regfield", "regfieldrotate"):
            fargs = child.nodeargd.argnlist
            fname = _render_one_line(fargs[0])
            width = _render_one_line(fargs[1])
            bitpos = _render_one_line(fargs[2])
            reset = _render_one_line(fargs[3])
            try:
                hi = int(bitpos) + int(width) - 1
                bits = f"[{hi}:{bitpos}]" if int(width) > 1 else f"[{bitpos}]"
            except ValueError:
                bits = ""
            field_lines.append(f"- {fname} {bits} reset={reset}")
        elif isinstance(child, LatexEnvironmentNode) and child.environmentname == "regdesc":
            for sub in child.nodelist:
                if isinstance(sub, LatexEnvironmentNode) and sub.environmentname == "reglist":
                    field_descs = _parse_reglist_descriptions(sub.nodelist)

    # A handful of registers carry an empty address argument upstream (RISC-V
    # `pmpXcfg` is a field-layout template, not a mapped register). Emitting a
    # dangling "at address" for those states an address that does not exist.
    header = f"Register {name} at address {address}" if address else f"Register {name}"
    text_lines = [header, "", *field_lines, ""]
    for fname, desc in field_descs.items():
        if fname != "(reserved)" and desc:
            text_lines.append(f"{fname}: {desc}")
            text_lines.append("")

    return name, "\n".join(text_lines).strip()


def _parse_reglist_descriptions(nodelist) -> dict[str, str]:
    descs: dict[str, str] = {}
    current_name = None
    current_body: list = []

    def flush():
        if current_name is not None:
            descs[current_name.strip()] = _finalize(render_nodes(current_body))

    for node in nodelist:
        if isinstance(node, LatexMacroNode) and node.macroname == "item":
            flush()
            args = node.nodeargd.argnlist if node.nodeargd else []
            current_name = _finalize(render_nodes(args[0])) if args and args[0] is not None else None
            current_body = []
        else:
            current_body.append(node)
    flush()
    return descs


_LONGTABLE_MARKERS = ("endfirsthead", "endhead", "endfoot", "endlastfoot")


def _strip_repeated_headers(nodelist):
    """Drop longtable's repeated-header and continuation-footer blocks.

    A longtable writes its header twice -- once before \\endfirsthead for page
    one, once before \\endhead for every later page -- plus "cont'd on next
    page" footer rows. Rendered naively those become duplicate header rows and
    pagination noise in the middle of the extracted table. The layout is
    always: firsthead, head, foot, lastfoot, body; so the first block and the
    body are content and everything between them is repetition.
    """
    segments: list[list] = [[]]
    for node in nodelist or []:
        if isinstance(node, LatexMacroNode) and node.macroname in _LONGTABLE_MARKERS:
            segments.append([])
        else:
            segments[-1].append(node)
    if len(segments) <= 2:
        return [n for seg in segments for n in seg]
    return segments[0] + segments[-1]


def _render_table(node: LatexEnvironmentNode) -> str:
    """Render a longtable/tabular environment: rows split on \\\\, cells on &.

    \\multirow spanning isn't reconstructed -- a spanned cell's value appears
    only on the row it is physically written on.
    """
    caption = _render_caption(node.nodelist)
    raw = render_nodes(_strip_repeated_headers(node.nodelist))

    lines = [caption] if caption else []
    for row in raw.split(_ROW_SEP):
        cells = [_finalize(cell).replace("\n", " ").strip() for cell in row.split(_CELL_SEP)]
        cells = [c for c in cells if set(c) - set("-| ")]  # drop rule-only cells
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


_TABLE_ENVIRONMENTS = ("longtable", "tabular", "tabularx")


def _build_tree(nodelist) -> SectionNode:
    """Turn a parsed TRM document into the SectionNode/Block tree chunking.py expects.

    The LaTeX sectioning commands are a flat sequence carrying a depth rather
    than a nesting, so the hierarchy is rebuilt with an explicit stack: a
    ``\\subsection`` closes any open ``\\subsection``/``\\subsubsection``/... and
    attaches under the nearest shallower heading. Levels are allowed to skip
    (chapter straight to subsection happens in the TRM), which is why the stack
    is popped by comparison rather than indexed by level.

    Registers become ``Block(atomic=True)``: a register is one indivisible
    record -- name, address, bitfield layout, per-field descriptions -- and the
    line-grouping heuristic in ``_split_oversized_block`` would sever a large
    one mid-bitfield, stranding fields from the name that identifies them. The
    flag constrains splitting only; consecutive registers still merge, which is
    wanted, since one peripheral's registers share context and sit well under
    MIN_WORDS_PER_CHUNK individually.
    """
    root = SectionNode(title="")
    stack: list[tuple[int, SectionNode]] = [(-1, root)]
    prose_buffer: list = []

    def add_block(rendered: str, *, atomic: bool = False) -> None:
        rendered = _finalize(rendered)
        if rendered:
            stack[-1][1].blocks.append(Block(text=rendered, atomic=atomic))

    def flush_prose() -> None:
        # One Block per run of prose between structural elements, so the
        # splitter has block boundaries to break an oversized section on.
        add_block(render_nodes(prose_buffer))
        prose_buffer.clear()

    def walk(nodes):
        for node in nodes:
            if isinstance(node, LatexMacroNode) and node.macroname in SECTION_LEVELS:
                flush_prose()
                level = SECTION_LEVELS[node.macroname]
                while len(stack) > 1 and stack[-1][0] >= level:
                    stack.pop()
                child = SectionNode(title=_section_title(node))
                stack[-1][1].children.append(child)
                stack.append((level, child))
            elif isinstance(node, LatexEnvironmentNode) and node.environmentname == "register":
                flush_prose()
                add_block(_render_register(node)[1], atomic=True)
            elif isinstance(node, LatexEnvironmentNode) and node.environmentname in _TABLE_ENVIRONMENTS:
                flush_prose()
                add_block(_render_table(node))
            elif isinstance(node, LatexEnvironmentNode) and node.environmentname in _TRANSPARENT_ENVIRONMENTS:
                # Pure container (footnotesize wrapper, threeparttable, ...):
                # descend so the tables and registers inside are still found.
                walk(node.nodelist)
            else:
                prose_buffer.append(node)

    walk(nodelist)
    flush_prose()

    # A chapter file is a single \chapter and nothing outside it. Keeping the
    # synthetic root as an extra level would prefix every chunk with an empty
    # heading, so promote the chapter to the tree root when it is alone.
    if not root.blocks and len(root.children) == 1:
        return root.children[0]
    return root


def chunk_latex(
    text: str,
    file_path: str,
    *,
    value_macros: dict[str, str] | None = None,
    tags: frozenset[str] | None = None,
    max_words: int = MAX_WORDS_PER_CHUNK,
    min_words: int = MIN_WORDS_PER_CHUNK,
) -> list[RawChunk]:
    """Chunk a TRM .tex document along its heading boundaries.

    ``text`` is expected to be include-resolved already (see
    :func:`resolve_document`); passing raw chapter source still works but
    yields prose with the registers missing.

    The returned chunks carry no ``kind``: the shared model has no such field
    and there is no reason to add one, because register-ness is already legible
    in the text itself -- a register renders as a "Register <NAME> at address
    <ADDR>" line, which is what both a reader and the register census key on.
    ``Refs`` is left empty; ``\\label``/``\\hyperref`` have no doc_refs mapping
    yet.
    """
    value_token = _VALUE_MACROS.set(value_macros or {})
    tag_token = _ACTIVE_TAGS.set(tags or frozenset())
    try:
        tree = _build_tree(parse(text))
    finally:
        _VALUE_MACROS.reset(value_token)
        _ACTIVE_TAGS.reset(tag_token)

    chunks: list[RawChunk] = []
    build_chunks(tree, [tree.title] if tree.title else [], max_words, chunks, file_path, [0])
    return merge_undersized_chunks(chunks, min_words)


def chunk_document(
    path: Path | str,
    *,
    extra_tags: frozenset[str] | None = None,
    file_path: str | None = None,
    max_words: int = MAX_WORDS_PER_CHUNK,
    min_words: int = MIN_WORDS_PER_CHUNK,
) -> tuple[list[RawChunk], ResolvedDocument]:
    """Resolve includes for a chapter file and chunk the result.

    ``file_path`` overrides the recorded chunk identity, so an ingest can store
    a corpus-relative path rather than wherever the checkout happens to live.
    """
    doc = resolve_document(path, extra_tags=extra_tags)
    defs, _ = _preamble_context(Path(path))
    values = {k: _finalize(render_nodes(parse(v))) for k, v in defs.items() if k not in _NON_VALUE_MACROS}
    chunks = chunk_latex(
        doc.text,
        file_path if file_path is not None else str(path),
        value_macros=values,
        tags=frozenset(doc.tags),
        max_words=max_words,
        min_words=min_words,
    )
    return chunks, doc


if __name__ == "__main__":
    import sys

    path = Path(sys.argv[1])
    chunks, doc = chunk_document(path)
    for missing in doc.missing:
        print(f"!! unresolved include: {missing}", file=sys.stderr)
    for c in chunks:
        print(f"=== [{c.chunk_index}] {c.section_path} ===")
        print(c.text[:400])
        print()
