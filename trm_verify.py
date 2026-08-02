"""Shared helpers for verifying the TRM corpus against its LaTeX source.

The verification tools need to answer "what should have come out of this file?"
without trusting the parser under test. So this module deliberately re-derives
the source side independently -- its own comment stripping, its own
`\\subfile`/`\\input` resolution, its own register extraction -- rather than
importing latex_parser. A checker that shares the parser's include resolution
cannot detect a bug in it, and unresolved includes are the failure mode that
would silently cost the TRM corpus most of its register content.

Being independent means being approximate: text extraction here strips macros
crudely, which is fine because every check built on it compares ratios between
files rather than trusting an absolute figure.

`expand_document` evaluates the two constructs that genuinely gate content --
`\\iffalse ... \\fi` and `\\tagged`/`\\untagged`/`\\iftagged` -- because counting
source the parser is right to exclude reads as content loss. It used to skip
both, which cost the census 8 registers: six inside `\\iffalse` in the ESP32-C3
and ESP32-S3 AES register files, and two inside `\\tagged{ESP32-H21}` (a
different chip) in ESP32-H2/33-USBSERIALJTAG. The evaluation here is written
from the LaTeX semantics rather than shared with latex_parser, so the two
agreeing is evidence rather than tautology.

The active tag set is derived from the sources the same way a build does: the
chip's own `\\chipname` (which `\\usetag{\\chipseries}` in
00-shared/config/preamble-trm-repo.sty activates) plus whatever each aggregator
document declares with `\\usetag`. Since ingest_trm parses *every* revision
variant of a chip and merges the results, a tag only some variants activate --
`ESP32-P4-latest` -- leaves both branches in the corpus, so both are counted;
see `TagContext`.

`\\ifglobal` is deliberately left alone: it lives entirely in the preamble that
`document_body` already discards, and its branches hold no content.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import BaseModel

# Chip manuals live in one directory per chip, named with the marketing name
# (ESP8684 rather than esp32c2); chip_vocab.chip_for_trm_folder() maps them.
# 00-shared and 00-trm-shared hold included fragments, not manuals.
_CHIP_DIR_RE = re.compile(r"^(ESP32(-[A-Z0-9]+)?|ESP8684)$")

# A chapter is a numbered top-level file: ESP32-C3/01-I2C__EN.tex. The
# unnumbered siblings (ESP32-C3-main__EN.tex, and P4's chip-revision variant)
# are aggregators that \subfileinclude every chapter; treating them as
# documents too would count the whole manual a second time.
_CHAPTER_RE = re.compile(r"^\d+[-.].*__EN\.tex$")

_COMMENT_RE = re.compile(r"(?<!\\)%.*")
_INCLUDE_RE = re.compile(r"\\(?:subfileinclude|subfile|input|include)\s*\{([^}]*)\}")
_DEF_RE = re.compile(r"\\def\s*\\([A-Za-z]+)\s*\{([^}]*)\}")

# \begin{register}{H}{I2C\_SCL\_LOW\_PERIOD\_REG}{0x0000} -- placement, name, address.
_REGISTER_ENV_RE = re.compile(r"\\begin\{register\}")


def resolve_trm_root(explicit: Path | None = None) -> Path:
    """Locate the TRM LaTeX checkout, mirroring build_idf_docs.sh's IDF_PATH cascade.

    The ./trm_latex symlink is a local convenience that cannot travel between
    machines (it is absolute and gitignored), so $TRM_PATH is the portable
    mechanism and takes precedence over it.
    """
    candidates = [explicit] if explicit else []
    if env := os.environ.get("TRM_PATH"):
        candidates.append(Path(env))
    candidates.append(Path("trm_latex"))
    candidates.append(Path.home() / "git" / "esp-technical-reference-manual-latex")

    for candidate in candidates:
        if candidate and candidate.is_dir():
            return candidate.resolve()

    tried = ", ".join(str(c) for c in candidates if c)
    raise FileNotFoundError(f"no TRM LaTeX source found (tried: {tried}); set $TRM_PATH")


def chip_dirs(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and _CHIP_DIR_RE.match(p.name))


def chapter_files(chip_dir: Path) -> list[Path]:
    """The chapter documents of one manual -- the unit an ingest treats as a document."""
    return sorted(p for p in chip_dir.glob("*__EN.tex") if _CHAPTER_RE.match(p.name))


def strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text)


def document_body(text: str) -> str:
    """Everything between \\begin{document} and \\end{document}, comments removed.

    Every TRM file -- chapter or subfile -- is standalone-compilable, so each
    carries its own preamble. The preamble is scaffolding (\\documentclass,
    \\usepackage, title page includes) and counting its words or following its
    includes would inflate every figure derived from it.
    """
    text = strip_comments(text)
    if (start := text.find(r"\begin{document}")) >= 0:
        text = text[start + len(r"\begin{document}") :]
    if (end := text.find(r"\end{document}")) >= 0:
        text = text[:end]
    return text


# --------------------------------------------------------------------------
# Conditionals: which of a file's text a build of this manual actually keeps.
# --------------------------------------------------------------------------

# \newcommand\chipname{ESP32-H2} in <CHIP>/00-chip-spec-content/chip-spec-settings.sty.
# Read rather than assumed from the directory name so a chip whose folder is
# named for the marketing part (ESP8684) still gets the tag its build uses.
_CHIPNAME_RE = re.compile(r"\\newcommand\s*\\chipname\s*\{([^}]*)\}")
_USETAG_RE = re.compile(r"\\usetag\s*\{([^}]*)\}")

# \chipseries expands to \chipname; both are spellings of "this chip's tag".
_CHIP_TAG_MACROS = {"chipseries", "chipname"}

# A control sequence: a word, or a single non-letter (\\, \_, \%). Matching the
# second form matters -- \\ is a line break, and reading its trailing backslash
# as the start of the next control word would let "\\iffalse" fire on text that
# is really a break followed by a word.
_CS_RE = re.compile(r"\\(?:([A-Za-z]+)\*?|(.))", re.DOTALL)

# TeX skips an unselected conditional by token, counting only *primitive*
# conditionals towards nesting. These all begin "if" but are ordinary macros
# (etoolbox, ifthen, the tagging package), so they consume no \fi and must not
# be counted when scanning for the \fi that closes an \iffalse.
_NON_PRIMITIVE_IFS = frozenset(
    {
        "iftagged",
        "ifthenelse",
        "iflabelexists",
        "iftoggle",
        "ifbool",
        "ifboolexpr",
        "ifblank",
        "ifdef",
        "ifcsdef",
        "ifcsundef",
        "ifdefstring",
        "ifdefempty",
        "ifdefvoid",
        "ifstrequal",
        "ifnumcomp",
        "ifnumequal",
    }
)

_TAG_MACROS = frozenset({"tagged", "untagged", "iftagged"})

# Tag states. A tag is ALWAYS active if every variant of the manual declares it,
# SOMETIMES if only some do, NEVER if none.
_ALWAYS, _SOMETIMES, _NEVER = "always", "sometimes", "never"


class TagContext(BaseModel):
    """The tags a manual's builds activate, and how consistently.

    One chip can ship several manuals -- ESP32-P4 has a mainline one and a
    chip-revision-v1.3 one -- and ingest_trm parses every variant and merges the
    output, so the corpus holds the *union* of what any variant emits. The
    source side has to be counted the same way or the comparison is not
    like-for-like. Hence three states rather than a flat active/inactive set:
    `ESP32-P4-latest` is declared by one variant and not the other, so both
    branches of a conditional on it end up in the corpus and both are counted,
    while `ESP32-H21` is declared by no ESP32-H2 build and its content is
    correctly dropped.
    """

    chip_name: str
    always: frozenset[str] = frozenset()
    sometimes: frozenset[str] = frozenset()

    def state_of(self, taglist: str) -> str:
        """Evaluate a `\\tagged`-style argument, which may be a comma list.

        The tagging package treats the list as a disjunction, so one active tag
        selects the content. A tag written as an unresolvable macro is treated
        as SOMETIMES -- keeping both branches is the conservative reading, and
        the alternative would silently drop content on a macro we failed to
        expand.
        """
        state = _NEVER
        for raw in taglist.split(","):
            tag = raw.strip()
            if not tag:
                continue
            if tag.startswith("\\"):
                name = tag.lstrip("\\").rstrip("{}").strip()
                if name in _CHIP_TAG_MACROS:
                    tag = self.chip_name
                else:
                    return _SOMETIMES
            if tag in self.always:
                return _ALWAYS
            if tag in self.sometimes:
                state = _SOMETIMES
        return state


def _chip_name(chip_dir: Path) -> str:
    settings = chip_dir / "00-chip-spec-content" / "chip-spec-settings.sty"
    try:
        match = _CHIPNAME_RE.search(settings.read_text(errors="replace"))
    except OSError:
        return chip_dir.name
    return match.group(1).strip() if match else chip_dir.name


def tag_context(chip_dir: Path) -> TagContext:
    """Derive the tag states of a manual from its aggregator documents.

    Aggregators are the unnumbered `*__EN.tex` at the top of a chip directory --
    the documents that actually get compiled into a PDF, and the only place
    `\\usetag` is written. Reading them means a new revision manual appearing
    upstream is picked up without editing this file.
    """
    name = _chip_name(chip_dir)
    variants: list[set[str]] = []
    for path in sorted(chip_dir.glob("*__EN.tex")):
        if _CHAPTER_RE.match(path.name):
            continue
        try:
            text = strip_comments(path.read_text(errors="replace"))
        except OSError:
            continue
        tags = {name}
        for arg in _USETAG_RE.findall(text):
            for raw in arg.split(","):
                tag = raw.strip()
                if tag.startswith("\\"):
                    # \usetag{\chipseries} -- the chip's own name, already added.
                    continue
                if tag:
                    tags.add(tag)
        variants.append(tags)

    if not variants:
        variants = [{name}]
    always = frozenset(set.intersection(*variants))
    sometimes = frozenset(set.union(*variants) - always)
    return TagContext(chip_name=name, always=always, sometimes=sometimes)


def _skip_to_fi(text: str, start: int) -> tuple[str, int]:
    """Consume a false conditional's body, returning the surviving text and offset.

    Nesting is counted over primitive conditionals only (see
    _NON_PRIMITIVE_IFS). An `\\else` at depth one ends the dead branch, and what
    follows up to the matching `\\fi` is live and returned to be processed.
    """
    depth = 1
    i = start
    live_from: int | None = None
    while i < len(text):
        j = text.find("\\", i)
        if j < 0:
            break
        match = _CS_RE.match(text, j)
        if match is None:
            i = j + 1
            continue
        i = match.end()
        name = match.group(1)
        if name is None:
            continue
        if name == "fi":
            depth -= 1
            if depth == 0:
                return (text[live_from : match.start()] if live_from is not None else ""), i
        elif name == "else" and depth == 1 and live_from is None:
            live_from = i
        elif name.startswith("if") and name not in _NON_PRIMITIVE_IFS:
            depth += 1
    # Unterminated \iffalse: dropping the rest of the file on a malformed source
    # would look exactly like the content loss this module exists to detect, so
    # keep what came after instead.
    return text[start:], len(text)


def apply_conditionals(text: str, tags: TagContext) -> str:
    """Resolve `\\iffalse` and tag selection, leaving everything else untouched.

    Selection has to happen before includes are followed: a `\\subfile` inside a
    dead branch names a file this manual never compiles, and following it would
    pull a whole chapter of another chip's registers into the census.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        j = text.find("\\", i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        match = _CS_RE.match(text, j)
        if match is None:
            out.append(text[j:])
            break
        name = match.group(1)
        if name == "iffalse":
            kept, i = _skip_to_fi(text, match.end())
            out.append(apply_conditionals(kept, tags))
            continue
        if name in _TAG_MACROS:
            consumed = _select_tagged(text, name, match.end(), tags)
            if consumed is not None:
                kept, i = consumed
                out.append(apply_conditionals(kept, tags))
                continue
        out.append(match.group(0))
        i = match.end()
    return "".join(out)


def _select_tagged(text: str, name: str, pos: int, tags: TagContext) -> tuple[str, int] | None:
    """Read one tag macro's arguments and return (surviving text, offset after it).

    Returns None if the arguments don't parse, so the caller can leave the
    source as it found it rather than guessing where the macro ended.

    Two `\\iftagged` in the corpus (ESP32-C5/42-PARLIO-reg__EN.tex:338 and the
    ESP32-P4-latest one it mirrors) are written with only two arguments, as if
    they were `\\tagged`. Reading the following `\\begin{register}` as the else
    branch would be worse than useless, so a missing third argument is taken as
    an empty one -- which is what the author wrote them to mean.
    """
    arity = 3 if name == "iftagged" else 2
    args: list[str] = []
    for index in range(arity):
        read = _balanced_arg(text, pos)
        if read is None:
            if name == "iftagged" and index == 2:
                args.append("")
                break
            return None
        arg, pos = read
        args.append(arg)

    state = tags.state_of(args[0])
    if name == "tagged":
        return ("" if state == _NEVER else args[1]), pos
    if name == "untagged":
        return ("" if state == _ALWAYS else args[1]), pos
    # \iftagged{tag}{yes}{no}: a SOMETIMES tag selects each branch in some
    # variant, so the merged corpus contains both and both are kept.
    kept = []
    if state != _NEVER:
        kept.append(args[1])
    if state != _ALWAYS:
        kept.append(args[2])
    return "\n".join(kept), pos


class ExpandedDoc(BaseModel):
    """One chapter with every \\subfile/\\input resolved and concatenated."""

    path: Path
    files: list[Path] = []
    missing: list[str] = []
    text: str = ""


def expand_document(path: Path, chip_dir: Path, root: Path) -> ExpandedDoc:
    """Recursively inline a chapter's includes, returning its full source text.

    Include arguments are written against \\def'd path macros (\\modulefiles is
    the common one) and may omit the .tex extension, so both are handled here.
    Resolution tries the including file's own directory first and then the chip
    directory, because subfiles are written with paths relative to whichever the
    author found natural; shared front matter resolves out of 00-trm-shared.
    Unresolved includes are collected rather than raised -- an ingest that loses
    one file should show up as a shortfall, not as a crash.

    Dead conditional branches are removed as each file is read, so neither their
    text nor the files they include reach the result.
    """
    doc = ExpandedDoc(path=path)
    _expand_into(path, chip_dir, root, {}, set(), doc, tag_context(chip_dir))
    return doc


def _expand_into(
    path: Path,
    chip_dir: Path,
    root: Path,
    defs: dict[str, str],
    seen: set[Path],
    doc: ExpandedDoc,
    tags: TagContext,
) -> None:
    path = path.resolve()
    if path in seen:  # cycle guard; a shared fragment included twice counts once
        return
    seen.add(path)

    try:
        body = apply_conditionals(document_body(path.read_text(errors="replace")), tags)
    except OSError:
        doc.missing.append(str(path))
        return

    doc.files.append(path)
    doc.text += body + "\n"

    defs = dict(defs)
    for name, value in _DEF_RE.findall(path.read_text(errors="replace")):
        defs[name] = value.strip()

    for raw in _INCLUDE_RE.findall(body):
        target = _substitute_defs(raw.strip(), defs)
        resolved = _resolve_include(target, path.parent, chip_dir, root)
        if resolved is None:
            doc.missing.append(target)
            continue
        _expand_into(resolved, chip_dir, root, defs, seen, doc, tags)


def _substitute_defs(arg: str, defs: dict[str, str]) -> str:
    for name, value in defs.items():
        arg = re.sub(rf"\\{name}(?![A-Za-z])\s*", value.rstrip("/") + "/", arg)
    arg = re.sub(r"/+", "/", arg)
    return arg if arg.endswith(".tex") else arg + ".tex"


def _resolve_include(target: str, here: Path, chip_dir: Path, root: Path) -> Path | None:
    for base in (here, chip_dir, root, root / "00-trm-shared", root / "00-shared"):
        candidate = base / target
        if candidate.is_file():
            return candidate
    return None


class SourceRegister(BaseModel):
    """One register definition as it appears in source.

    `parameterised` marks a name written with a placeholder group -- e.g.
    SPI\\_MEM\\_C\\_{n}\\_REG for a bank of identical registers. Roughly one in
    seven registers is written this way, and how the placeholder renders is a
    parser choice, so these can't be checked by exact name and are counted
    separately rather than reported as losses.
    """

    name: str
    address: str
    file: str
    parameterised: bool = False


def _unescape(name: str) -> str:
    """LaTeX-escaped identifiers (I2C\\_SCL\\_REG) as they appear once rendered."""
    return name.replace(r"\_", "_").replace(r"\%", "%").replace(r"\&", "&").strip()


def _balanced_arg(text: str, start: int) -> tuple[str, int] | None:
    """Read one brace group starting at or after `start`, honouring nesting.

    Register names are not flat: a bank of registers is written
    {SPI\\_MEM\\_C\\_{n}\\_REG}, and a non-nesting `\\{[^}]*\\}` match truncates
    those at the inner brace. That silently mangled ~15% of names, which then
    looked like missing registers -- exactly the false alarm this tooling exists
    to avoid producing.
    """
    i = start
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if i >= len(text) or text[i] != "{":
        return None
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j], j + 1
    return None


def registers_in(text: str, file_label: str = "") -> list[SourceRegister]:
    """Every \\begin{register} in already-expanded source, with name and address.

    Names matter more than the count: knowing *which* registers are absent from
    the chunks points straight at the chapter whose includes failed, whereas a
    bare total only says something is wrong.
    """
    found: list[SourceRegister] = []
    for match in _REGISTER_ENV_RE.finditer(text):
        pos = match.end()
        args: list[str] = []
        for _ in range(3):  # placement, name, address
            read = _balanced_arg(text, pos)
            if read is None:
                break
            arg, pos = read
            args.append(arg)
        if len(args) < 3:
            continue
        raw_name = args[1]
        found.append(
            SourceRegister(
                name=_unescape(raw_name).replace("{", "").replace("}", ""),
                address=_unescape(args[2]),
                file=file_label,
                parameterised="{" in raw_name,
            )
        )
    return found


def count_register_envs(text: str) -> int:
    """Raw \\begin{register} count, including any whose arguments didn't parse."""
    return len(_REGISTER_ENV_RE.findall(text))


_MATH_RE = re.compile(r"\$[^$]*\$")
_MACRO_WITH_ARG_RE = re.compile(r"\\(?:label|ref|hyperref|cite|includegraphics|usepackage|documentclass)\s*(\[[^\]]*\])?\s*\{[^}]*\}")
_MACRO_RE = re.compile(r"\\[A-Za-z@]+\*?\s*(\[[^\]]*\])?")
_BRACE_RE = re.compile(r"[{}]")
_ALIGN_RE = re.compile(r"[&~^_]|\\\\")


def source_text_words(text: str) -> int:
    """Approximate the human-readable word count of LaTeX source.

    Used for capture rate, which compares a file's extracted words against its
    source's. Macro names, math and labels are dropped because they are markup
    the parser is right to discard; what is left over-counts slightly (table
    cell fragments count as words), so a healthy capture rate sits somewhat
    below 100% rather than at it. The number is only ever compared between
    files in the same corpus, so a consistent bias is harmless -- an outlier is
    the signal.
    """
    text = _MACRO_WITH_ARG_RE.sub(" ", text)
    text = _MATH_RE.sub(" ", text)
    text = _MACRO_RE.sub(" ", text)
    text = _BRACE_RE.sub(" ", text)
    text = _ALIGN_RE.sub(" ", text)
    return len(text.split())


def source_file_for(root: Path, file_path: str) -> Path | None:
    """Map a chunk's file_path back to its .tex, tolerating extension and prefix.

    The TRM ingest's exact file_path convention isn't fixed yet, so accept the
    plausible spellings (with or without .tex, chip-relative or root-relative)
    instead of hard-coding one and reporting every file as missing if it changes.
    """
    candidates = [root / file_path, root / f"{file_path}.tex"]
    if file_path.endswith(".tex"):
        candidates.append(root / file_path[: -len(".tex")])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
