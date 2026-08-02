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

KNOWN LIMITATION -- `expand_document` does not evaluate conditionals, so it
counts source the parser is right to exclude, and register censuses read ~8
registers low as a result:

  - `\\iffalse ... \\fi` blocks (deliberately disabled upstream). Six registers
    across ESP32-C3/24-AES and ESP32-S3/30-AES.
  - `\\tagged{<TAG>}{...}` with an inactive tag, i.e. content belonging to a
    different chip. Two registers in ESP32-H2/33-USBSERIALJTAG carrying
    `\\tagged{ESP32-H21}`.

So `register_census.py check` reports 76.9% for those two AES chapters and 93.3%
for that H2 chapter on a correct ingest. That is the expected reading, not a
regression. Teaching this module the two constructs would take the census to a
true 100% -- worth doing, and worth doing *here* rather than by importing
latex_parser, which would forfeit the independence the module exists for.
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
    """
    doc = ExpandedDoc(path=path)
    _expand_into(path, chip_dir, root, {}, set(), doc)
    return doc


def _expand_into(
    path: Path, chip_dir: Path, root: Path, defs: dict[str, str], seen: set[Path], doc: ExpandedDoc
) -> None:
    path = path.resolve()
    if path in seen:  # cycle guard; a shared fragment included twice counts once
        return
    seen.add(path)

    try:
        body = document_body(path.read_text(errors="replace"))
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
        _expand_into(resolved, chip_dir, root, defs, seen, doc)


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
