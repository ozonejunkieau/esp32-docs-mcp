"""Chunk the ESP-IDF SoC headers into JSONL -- the third corpus, doc_type "src".

These headers are the C counterpart to the Technical Reference Manuals. Where
`ESP32-C3/01-I2C` *describes* `I2C_SCL_LOW_PERIOD_REG`, a SoC header *defines*
its address and bitmasks, and an agent holding one usually wants the other. This
ingest closes that loop by making the definitions retrievable alongside the prose
and, more importantly, exactly addressable by symbol name (see
`mcp_server.esp32_docs_find_symbol`).

Two kinds of header are in scope, ingested two different ways, because they
have two different retrieval characters.

**`components/soc/**/include/soc/*.h` -- full text.** 481 files, ~203K words,
936 chunks. `soc_caps.h`, `clk_tree_defs.h`, `periph_defs.h` and the type
definitions are hand-written and explanatory; a question like "which chips
support the ADC calibration scheme" is genuinely a semantic one, so these are
chunked and embedded like any other prose.

**`components/soc/*/register/**/soc/*.h` -- one card per file.** 1,555 files,
~6M words of mechanical `#define I2C_SCL_LOW_PERIOD_REG (DR_REG_I2C_BASE +
0x0)`. Embedding that would be pointless and expensive in the same breath:
nobody retrieves a bitmask by vibe, and the full-text route measured 29,588
chunks -- roughly sixteen hours of embedding for content whose entire value is
exact lookup. So a register header contributes a *card* instead: its
repo-relative path, its leading comment, and the full list of symbols it
declares. `symbol_refs` is populated exactly as it would have been, which is
what `esp32_docs_find_symbol` keys on and the only thing these files were ever
going to be found by. The card names symbols without their values, so it locates
a definition rather than reproducing it -- an agent that knows the file can read
the address off disk in one step.

The wider public headers (4,321 files, 4.9M words) and every `.c` file remain out
of scope entirely: they are implementation, not definition.

Getting the register half in at all matters more than it looks, because of a
recent upstream move. ESP-IDF v6.x relocated the generated register headers from
`include/soc/` to `register/soc/` (upstream commit "refactor(soc): sort esp32c3
soc headers"), so `i2c_reg.h` is not in the `include/` set at all any more.
Measured against the TRM corpus's 10,100 non-parameterised register names,
`include/` alone resolves **1.8%**; with register cards it is **78.3%**, for
about 3.4K extra chunks rather than 28.7K. The 22% that still misses is genuine
upstream divergence, not lost extraction: the TRM's `PWM_*` registers are the
headers' `MCPWM_*`, its `RTCIO_*` are `RTC_IO_*`, and ESP32/ESP32-S2 never got
a `twai_reg.h` at all (that peripheral is struct-only for those targets).

No C parser is involved and none is needed. This is a locate-and-cite index, and
the value is in `symbol_refs`, which regexes over `#define`, `typedef`, tags,
enum members, prototypes and extern declarations populate densely enough that an
exact symbol lookup lands on the right file. A mis-parsed macro body costs
nothing; a missed `#define` name costs a lookup, which is why extraction errs
towards over-collecting.

Chip applicability follows the IDF corpus's rule, not the TRM's: the `chips`
LIST, never the singular `chip`. A header under `components/soc/esp32c3/` is
that chip's; a header outside any per-chip directory is chip-agnostic and gets
*every* chip in chips.yaml. An empty list would mean "matches no chip filter" --
the trap this project already removed once from the IDF corpus, and the reason
`chips` has no "applies to everything" sentinel value.

`revisions` is empty for every src row except ESP32-P4's register cards, where
the directory split *is* the silicon-revision axis the TRM already models. See
P4_REGISTER_REVISIONS for the evidence that mapping rests on.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import typer

from chip_vocab import ChipVocabulary, load_chip_vocabulary
from chunking import (
    MAX_WORDS_PER_CHUNK,
    MIN_WORDS_PER_CHUNK,
    Block,
    RawChunk,
    Refs,
    SectionNode,
    build_chunks,
    merge_undersized_chunks,
)

app = typer.Typer()

# components/soc/<chip>/include/soc/*.h, plus the chip-agnostic
# components/soc/include/soc/*.h. Matched structurally rather than by glob so
# both depths are covered by one rule.
SOC_COMPONENT = Path("components") / "soc"

# ESP32-P4's register headers are split by silicon revision, and that split is
# the same axis the TRM corpus already models as `revisions`. Mapping them lets a
# P4 register card say which stepping it applies to, exactly as the manuals do.
#
# It is asserted here rather than inferred at runtime because a *wrong* mapping
# is worse than none -- it would mark a register applicable to silicon it is not,
# which is the hardware-bug failure `revision_scope` exists to prevent. Four
# independent pieces of evidence agree:
#
#   1. components/soc/CMakeLists.txt selects hw_ver1 under
#      CONFIG_ESP32P4_SELECTS_REV_LESS_V3 and hw_ver3 otherwise, and that
#      Kconfig option is "Select ESP32-P4 revisions <3.0", i.e. 0.x and 1.x.
#   2. The TRM's own chip-spec-settings.sty defines the non-`-latest` manual's
#      title annotation as `\untagged{ESP32-P4-latest}{Chip Revision v1.3}` --
#      so the `v1.3` variant is chip revision 1.3, inside hw_ver1's 0.x/1.x band,
#      and `mainline` (the `-latest` tag) is the current 3.x silicon.
#   3. Of the 61 registers appearing *only* in the mainline manual, 59 are
#      defined in hw_ver3 and 0 in hw_ver1.
#   4. Of the 227 registers defined only in hw_ver3, all 227 appear in the
#      mainline manual and only 168 in the v1.3 manual.
#
# Note the label is the TRM's, not IDF's: hw_ver1 actually covers revisions 0.x
# *and* 1.x, while the manual names only v1.3. chips.yaml drives the MCP server's
# revision filter, so matching the manual's vocabulary is what keeps a
# cross-corpus revision filter coherent.
#
# ESP32-H21 has the same directory shape (hw_ver_mp, hw_ver_beta1) and is
# deliberately absent: no TRM is published for it, chips.yaml gives it only
# [mainline], and there is nothing to check a mapping against.
P4_REGISTER_REVISIONS = {"hw_ver1": ["v1.3"], "hw_ver3": ["mainline"]}

# Symbols per line on a card. Chosen so a line is a handful of words: the
# oversized-block splitter groups whole lines, so this is the granularity at
# which a large card divides.
_SYMBOLS_PER_LINE = 10

_SPDX_HEADER_RE = re.compile(r"\A\s*/\*.*?SPDX-License-Identifier.*?\*/\s*", re.S)

_INCLUDE_RE = re.compile(r"^\s*#\s*include\s+[\"<]([^\">]+)[\">]")
_DEFINE_RE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)")
_IFNDEF_RE = re.compile(r"^\s*#\s*ifndef\s+([A-Za-z_]\w*)\s*$")
_CPLUSPLUS_RE = re.compile(r"^\s*#\s*ifdef\s+__cplusplus\s*$")
_ENDIF_RE = re.compile(r"^\s*#\s*endif\b")
_PRAGMA_ONCE_RE = re.compile(r"^\s*#\s*pragma\s+once\s*$")

# A standalone comment line short enough to be a banner:
#   /*-------------------------- COMMON CAPS -----------------*/
#   /** Group: USB wrapper registers. */
#   /* Configuration registers */
_BANNER_RE = re.compile(r"^\s*/\*+[-=*\s]*(.*?)[-=*\s]*\*+/\s*$")
_MAX_BANNER_WORDS = 12

_TYPEDEF_CLOSE_RE = re.compile(r"^\s*\}\s*(?:__attribute__\s*\(\([^)]*\)\)\s*)?([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*;")
_TYPEDEF_SIMPLE_RE = re.compile(r"^\s*typedef\s+[A-Za-z_][\w\s*]*?([A-Za-z_]\w*)\s*;")
_TAG_RE = re.compile(r"^\s*(?:typedef\s+)?(?:struct|union|enum)\s+([A-Za-z_]\w*)")
_ENUM_MEMBER_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:=[^,]*)?,?\s*$")
_FUNC_RE = re.compile(r"^\s*(?:extern\s+|static\s+|inline\s+|__attribute__\s*\(\([^)]*\)\)\s*)*[A-Za-z_][\w\s*]*?[\s*]([A-Za-z_]\w*)\s*\([^;{]*\)\s*;")
_EXTERN_VAR_RE = re.compile(r"^\s*extern\s+[\w\s*]+?([A-Za-z_]\w*)\s*(?:\[|;|=)")

# Words a declaration regex can capture by accident. `defined` and `sizeof` look
# exactly like function calls; the control keywords look like prototypes when a
# macro body spills across lines.
_NOT_SYMBOLS = frozenset(
    {
        "if", "else", "for", "while", "do", "switch", "case", "return", "goto",
        "sizeof", "defined", "typedef", "struct", "union", "enum", "static",
        "extern", "const", "volatile", "inline", "void", "unsigned", "signed",
        "char", "short", "int", "long", "float", "double", "true", "false",
    }
)


def resolve_idf_root(explicit: Path | None = None) -> Path:
    """Locate the ESP-IDF checkout, mirroring the TRM path's $TRM_PATH cascade.

    $IDF_PATH is the portable mechanism and is what every other ESP-IDF tool
    reads, so it takes precedence over the conventional checkout location.
    """
    candidates = [explicit] if explicit else []
    if env := os.environ.get("IDF_PATH"):
        candidates.append(Path(env))
    candidates.append(Path.home() / "git" / "esp-idf")

    for candidate in candidates:
        if candidate and (candidate / SOC_COMPONENT).is_dir():
            return candidate.resolve()

    tried = ", ".join(str(c) for c in candidates if c)
    raise FileNotFoundError(f"no ESP-IDF checkout with {SOC_COMPONENT} found (tried: {tried}); set $IDF_PATH")


def is_register_header(path: Path, idf_root: Path) -> bool:
    """Whether a header is a generated register definition, i.e. gets a card not full text."""
    return "register" in path.relative_to(idf_root / SOC_COMPONENT).parts


def header_files(idf_root: Path, with_registers: bool = True) -> list[Path]:
    """Every SoC header in scope, sorted for reproducible output.

    Two sets, matched structurally rather than by glob because both span more
    than one depth:

    - `components/soc/**/include/soc/*.h` -- the per-chip
      `components/soc/esp32c3/include/soc/` and the chip-agnostic
      `components/soc/include/soc/`. 481 files, taken as full text.
    - `components/soc/*/register/**/soc/*.h` -- generated register definitions,
      including ESP32-P4's `register/hw_ver1/` and `register/hw_ver3/` and
      ESP32-H21's `register/hw_ver_mp/` and `register/hw_ver_beta1/`. 1,555
      files, taken as cards.

    `with_registers=False` drops the second set. It exists as an escape hatch,
    not as the recommended mode: without it the corpus resolves 1.8% of TRM
    register names instead of 78.3%, which is most of the reason this corpus
    exists.
    """
    soc = idf_root / SOC_COMPONENT
    found = [p for p in soc.rglob("*.h") if p.parent.name == "soc" and p.parent.parent.name == "include"]
    if with_registers:
        found += [p for p in soc.rglob("*.h") if p.parent.name == "soc" and is_register_header(p, idf_root)]
    return sorted(found)


def revisions_for(path: Path, idf_root: Path) -> list[str]:
    """Silicon revisions a header applies to -- empty for everything but P4's register split.

    Empty is the correct and overwhelmingly common answer: a header carries no
    revision axis, and `mcp_server._build_where` lets non-TRM rows through a
    revision filter unconditionally, so an empty list costs nothing. ESP32-P4 is
    the one case where the source genuinely encodes the distinction; see
    P4_REGISTER_REVISIONS for why that mapping is trusted.
    """
    parts = path.relative_to(idf_root / SOC_COMPONENT).parts
    if parts and parts[0] == "esp32p4":
        for part in parts:
            if part in P4_REGISTER_REVISIONS:
                return list(P4_REGISTER_REVISIONS[part])
    return []


def chips_for(path: Path, idf_root: Path, vocab: ChipVocabulary) -> list[str]:
    """Which chips a header applies to -- never an empty list.

    The directory under `components/soc/` is authoritative when it names a chip:
    `components/soc/esp32c3/include/soc/i2c_reg.h` is esp32c3's and no other
    target compiles it. Everything else -- `components/soc/include/soc/`, and the
    `linux` host-emulation stubs -- is not chip-scoped, so it gets every chip.

    Returning [] for those would read as "matches no chip filter" and make them
    invisible to every chip-scoped query, which is the failure this project
    already removed once from the IDF corpus. `chips` has no chip-agnostic
    sentinel by design: listing all of them *is* the representation.
    """
    parts = path.relative_to(idf_root / SOC_COMPONENT).parts
    if parts and parts[0] in vocab.chips:
        return [parts[0]]
    return sorted(vocab.chips)


def _strip_boilerplate(text: str) -> list[str]:
    """Drop the licence header, include guard, `#pragma once` and extern "C" scaffolding.

    All of it is per-file ceremony that says nothing about the hardware, and at
    481 files the SPDX block alone is ~19K words -- a tenth of the corpus, and
    identical every time, which is exactly the content that makes a nearest
    neighbour search worse. Everything else is kept verbatim, including ordinary
    comments: a `/*!< Approximate RC_FAST_CLK frequency in Hz */` trailing a
    define is often the only prose explaining it.
    """
    text = _SPDX_HEADER_RE.sub("", text)
    lines = text.splitlines()

    kept: list[str] = []
    guard: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]

        if _PRAGMA_ONCE_RE.match(line):
            index += 1
            continue

        if guard is None and (match := _IFNDEF_RE.match(line)):
            # An include guard is `#ifndef X` immediately followed by `#define X`.
            following = lines[index + 1] if index + 1 < len(lines) else ""
            define = _DEFINE_RE.match(following)
            if define and define.group(1) == match.group(1):
                guard = match.group(1)
                index += 2
                continue

        if _CPLUSPLUS_RE.match(line):
            # `#ifdef __cplusplus / extern "C" { / #endif` and its closing twin.
            close = index + 1
            while close < len(lines) and close <= index + 4 and not _ENDIF_RE.match(lines[close]):
                close += 1
            if close < len(lines) and _ENDIF_RE.match(lines[close]):
                index = close + 1
                continue

        kept.append(line)
        index += 1

    if guard is not None:
        for position in range(len(kept) - 1, -1, -1):
            if _ENDIF_RE.match(kept[position]):
                del kept[position]
                break
            if kept[position].strip():
                break

    return kept


def _code_lines(lines: list[str]) -> list[str]:
    """Each line with comment text blanked out, for declaration matching only.

    Symbol extraction must not read comments -- `/* see ledc_channel_config_t */`
    is a mention, not a declaration, and a doxygen block full of `@param name`
    would otherwise flood symbol_refs with parameter names. The chunk text keeps
    the comments; only this shadow copy loses them.
    """
    out: list[str] = []
    in_block = False
    for line in lines:
        result: list[str] = []
        index = 0
        while index < len(line):
            if in_block:
                end = line.find("*/", index)
                if end < 0:
                    index = len(line)
                    break
                in_block = False
                index = end + 2
                continue
            if line.startswith("//", index):
                break
            if line.startswith("/*", index):
                in_block = True
                index += 2
                continue
            result.append(line[index])
            index += 1
        out.append("".join(result))
    return out


def _add(target: set[str], name: str | None) -> None:
    if name and name not in _NOT_SYMBOLS:
        target.add(name)


def symbols_per_line(code: list[str]) -> list[set[str]]:
    """Symbols *declared* by each comment-stripped line.

    Per line rather than per file so a chunk gets the symbols its own text
    defines and not its neighbours'; `symbol_refs` is what the exact-lookup tool
    keys on, so an over-broad attribution would make every chunk in a file a hit
    for every symbol in it.

    Enum members are tracked with a brace-depth state machine, which is the one
    piece of real parsing here. It earns its keep: `soc_root_clk_t`'s members and
    `periph_defs.h`'s `PERIPH_*_MODULE` constants are exactly the identifiers
    someone looks up, and nothing else in a header declares them.
    """
    out: list[set[str]] = [set() for _ in code]
    depth = 0
    enum_depth: int | None = None
    pending_enum = False

    for index, line in enumerate(code):
        found = out[index]

        # Runs whatever the state: `} soc_root_clk_t;` closes the enum body.
        if match := _TYPEDEF_CLOSE_RE.match(line):
            _add(found, match.group(1))

        if enum_depth is not None:
            if match := _ENUM_MEMBER_RE.match(line):
                _add(found, match.group(1))
        else:
            if match := _DEFINE_RE.match(line):
                _add(found, match.group(1))
            elif match := _TYPEDEF_SIMPLE_RE.match(line):
                _add(found, match.group(1))
            elif match := _FUNC_RE.match(line):
                _add(found, match.group(1))
            elif match := _EXTERN_VAR_RE.match(line):
                _add(found, match.group(1))
            if match := _TAG_RE.match(line):
                _add(found, match.group(1))
            if re.search(r"\benum\b", line):
                pending_enum = True

        for char in line:
            if char == "{":
                depth += 1
                if pending_enum and enum_depth is None:
                    enum_depth = depth
                    pending_enum = False
            elif char == "}":
                if enum_depth is not None and depth == enum_depth:
                    enum_depth = None
                depth = max(0, depth - 1)

        if ";" in line and enum_depth is None:
            pending_enum = False

    return out


def _banner_title(line: str) -> str | None:
    """The heading text of a standalone banner comment, or None if it isn't one."""
    match = _BANNER_RE.match(line)
    if not match:
        return None
    title = match.group(1).strip().strip("-=* \t")
    if not title or not any(c.isalpha() for c in title):
        return None
    if len(title.split()) > _MAX_BANNER_WORDS:
        return None
    return title


def parse_header(text: str, title: str) -> SectionNode:
    """Turn one header into a section tree: banner comments become headings.

    Espressif's headers are already sectioned by hand -- `/*----- ADC CAPS
    -----*/` in soc_caps.h, `/** Group: Configuration registers */` in the
    generated ones -- and reusing those headings costs nothing and makes
    `section_path` say which peripheral a chunk belongs to. Files without banners
    simply come out as one flat section, which is the right answer for them.

    Blocks are runs of consecutive non-blank lines, so a `#define` and the
    comment above it stay together.
    """
    lines = _strip_boilerplate(text)
    code = _code_lines(lines)
    line_symbols = symbols_per_line(code)

    root = SectionNode(title=title)
    current = root
    buffer: list[int] = []

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        block_text = "\n".join(lines[i] for i in buffer).strip()
        if block_text:
            symbols: list[str] = []
            files: list[str] = []
            for i in buffer:
                symbols.extend(sorted(line_symbols[i]))
                if match := _INCLUDE_RE.match(code[i] or lines[i]):
                    files.append(match.group(1))
            current.blocks.append(
                Block(
                    text=block_text,
                    refs=Refs(
                        symbol_refs=list(dict.fromkeys(symbols)),
                        file_refs=list(dict.fromkeys(files)),
                    ),
                )
            )
        buffer = []

    for index, line in enumerate(lines):
        if (banner := _banner_title(line)) is not None:
            flush()
            current = SectionNode(title=banner)
            root.children.append(current)
            continue
        if not line.strip():
            flush()
            continue
        buffer.append(index)
    flush()

    return root


def _leading_comment(lines: list[str]) -> str:
    """The file's opening comment, once the licence header and guards are gone.

    Register headers are generated, and what little human text they carry sits
    right at the top -- "The following registers are for the LP I2C peripheral",
    or nothing at all. It is the only prose a card can honestly offer, so it goes
    on the card; when there is none, the card is just a path and its symbols,
    which is still exactly what the lookup needs.
    """
    collected: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if collected:
                break
            continue
        if in_block:
            collected.append(stripped.lstrip("*").strip())
            if "*/" in stripped:
                break
            continue
        if stripped.startswith("/*"):
            in_block = True
            collected.append(stripped.lstrip("/*").strip())
            if "*/" in stripped[2:]:
                break
            continue
        if stripped.startswith("//"):
            collected.append(stripped.lstrip("/").strip())
            continue
        break
    text = " ".join(part.rstrip("*/").strip() for part in collected if part.strip("*/ "))
    return text.strip()


def build_card(text: str, title: str) -> SectionNode:
    """Reduce one generated register header to a card: what it is, and what it declares.

    The alternative -- chunking these like prose -- was measured and rejected:
    1,555 register headers are ~6M words of `#define X (BASE + 0x0)` and produced
    28,652 chunks, an embedding run of roughly sixteen hours. None of it would
    ever be *retrieved* semantically, because a bitmask has no meaning a nearest
    neighbour can find; the only question anyone asks of these files is "where is
    X defined", and that is answered exactly, by `symbol_refs`, with no embedding
    at all. A card keeps the whole of that answer and discards the part that was
    never going to earn its vector.

    Symbols are listed in source order, in short lines. Both matter: source order
    keeps a register's own fields next to it, and the line width is the
    granularity at which `chunking._split_oversized_block` divides a card too big
    for one chunk, so each piece ends up with its own accurate slice of the
    symbols rather than the whole file's.
    """
    lines = _strip_boilerplate(text)
    code = _code_lines(lines)

    symbols: list[str] = []
    for found in symbols_per_line(code):
        symbols.extend(sorted(found))
    symbols = list(dict.fromkeys(symbols))

    includes = list(
        dict.fromkeys(
            match.group(1)
            for index, line in enumerate(code)
            if (match := _INCLUDE_RE.match(line or lines[index]))
        )
    )

    node = SectionNode(title=title)
    summary = f"Generated ESP-IDF SoC register header. Declares {len(symbols)} symbols."
    if comment := _leading_comment(lines):
        summary = f"{comment}\n\n{summary}"
    node.blocks.append(Block(text=summary, refs=Refs(file_refs=includes)))

    for start in range(0, len(symbols), _SYMBOLS_PER_LINE):
        run = symbols[start : start + _SYMBOLS_PER_LINE]
        node.blocks.append(Block(text=", ".join(run), refs=Refs(symbol_refs=run)))

    return node


def chunk_header(
    path: Path,
    file_path: str,
    card: bool = False,
    max_words: int = MAX_WORDS_PER_CHUNK,
    min_words: int = MIN_WORDS_PER_CHUNK,
) -> list[RawChunk]:
    """Chunk one header through the shared chunker, so `src` rows look like the rest.

    `card` picks the low-resolution route for generated register headers -- see
    build_card. Everything downstream is identical either way, which is the point
    of routing both through `build_chunks`: a card is a chunk like any other, and
    nothing in the store, the schema or the server has to know which it is.
    """
    text = path.read_text(errors="replace")
    root = build_card(text, file_path) if card else parse_header(text, file_path)
    chunks: list[RawChunk] = []
    build_chunks(root, [root.title], max_words, chunks, file_path, [0])
    return merge_undersized_chunks(chunks, min_words)


@app.command()
def ingest(
    out_path: Path = typer.Option(..., help="JSONL destination."),
    idf_path: Path = typer.Option(None, help="ESP-IDF checkout; defaults to $IDF_PATH, then ~/git/esp-idf."),
    chip: str = typer.Option(None, help="Limit to one chip's headers, e.g. 'esp32c3'. Omit for all."),
    registers: bool = typer.Option(
        True,
        help="Include components/soc/*/register/**/soc/*.h as one card per file (path, leading "
        "comment, declared symbols). On by default: cards cost ~3.4K chunks and take TRM register "
        "resolution from 1.8% to 78.3%. --no-registers drops them entirely.",
    ),
    max_words: int = typer.Option(MAX_WORDS_PER_CHUNK, help="Soft cap on words per chunk."),
    min_words: int = typer.Option(MIN_WORDS_PER_CHUNK, help="Chunks below this merge into a neighbour."),
) -> None:
    """Chunk every SoC header into JSONL, one JSON object per chunk."""
    root = resolve_idf_root(idf_path)
    vocab = load_chip_vocabulary()
    typer.echo(f"ESP-IDF source: {root}")

    headers = header_files(root, with_registers=registers)
    if chip:
        if chip not in vocab.chips:
            raise typer.BadParameter(f"'{chip}' is not in chips.yaml")
        headers = [p for p in headers if chips_for(p, root, vocab) == [chip]]
    cards = sum(1 for p in headers if is_register_header(p, root))
    typer.echo(f"found {len(headers)} SoC headers ({len(headers) - cards} full text, {cards} as cards)")

    failures: list[tuple[Path, str]] = []
    per_chip: dict[str, list[int]] = {}
    with_symbols = 0
    revision_scoped = 0
    written = 0

    with out_path.open("w") as out_file:
        for header in headers:
            file_path = header.relative_to(root).as_posix()
            chips = chips_for(header, root, vocab)
            card = is_register_header(header, root)
            revisions = revisions_for(header, root)
            try:
                chunks = chunk_header(header, file_path, card=card, max_words=max_words, min_words=min_words)
            except Exception as exc:  # noqa: BLE001 - one bad header shouldn't abort the corpus
                failures.append((header, f"{type(exc).__name__}: {exc}"))
                continue
            label = chips[0] if len(chips) == 1 else "(all chips)"
            counts = per_chip.setdefault(label, [0, 0])
            for c in chunks:
                record = c.model_dump()
                # chips is a LIST here, exactly as the IDF corpus uses it. `chip`
                # stays empty: the singular column is the TRM's
                # one-manual-per-chip axis. `revisions` is empty too except for
                # ESP32-P4's register directories, which really are the silicon
                # revision split the manuals model -- see P4_REGISTER_REVISIONS.
                record["chips"] = chips
                record["chip"] = ""
                record["revisions"] = revisions
                out_file.write(json.dumps(record) + "\n")
                written += 1
                counts[1 if card else 0] += 1
                if record["symbol_refs"]:
                    with_symbols += 1
                if revisions:
                    revision_scoped += 1

    typer.echo(f"\n{'chip':14s} {'full text':>10s} {'cards':>8s} {'total':>8s}")
    for label, (full, card_count) in sorted(per_chip.items()):
        typer.echo(f"  {label:12s} {full:10d} {card_count:8d} {full + card_count:8d}")
    typer.echo(f"\nwrote {written} chunks -> {out_path}")
    typer.echo(f"{with_symbols}/{written} chunks carry symbol_refs")
    if revision_scoped:
        typer.echo(f"{revision_scoped} chunks carry a silicon revision (ESP32-P4 register cards)")
    if failures:
        typer.echo(f"\n{len(failures)} headers failed:")
        for path, error in failures:
            typer.echo(f"  {path}: {error}")


@app.command("verify-registers")
def verify_registers(
    jsonl_path: Path = typer.Argument(..., help="src chunks JSONL to check."),
    source_root: Path = typer.Option(None, help="TRM checkout; defaults to $TRM_PATH, then ./trm_latex."),
) -> None:
    """Report what share of TRM register names resolve to a symbol in the SoC headers.

    This is the check that matters for this corpus, and no chunk count is a
    substitute for it. The corpus can look perfectly healthy -- hundreds of
    files, thousands of chunks, symbols on nearly every row -- while resolving
    almost nothing, which is exactly what happened when ESP-IDF moved the
    register headers out of `include/soc/` and the ingest kept cheerfully
    indexing the directory they had left. Only a comparison against the other
    corpus catches that.

    Expect **78.3%** overall. A number near 1.8% means the register headers are
    not being picked up at all; a broad collapse across every chip means symbol
    extraction has regressed. Per-chip figures are printed rather than averaged
    because the interesting failure is one chip going to zero.
    """
    # Imported here, not at module scope: this is a verification path, and the
    # ingest itself must not fail to run because a checker's dependency moved.
    from trm_verify import chapter_files, chip_dirs, expand_document, registers_in, resolve_trm_root

    vocab = load_chip_vocabulary()
    trm_root = resolve_trm_root(source_root)

    symbols_by_chip: dict[str, set[str]] = {}
    with jsonl_path.open() as f:
        for line in f:
            record = json.loads(line)
            for name in record["chips"]:
                symbols_by_chip.setdefault(name, set()).update(record["symbol_refs"])

    typer.echo(f"TRM source: {trm_root}")
    typer.echo(f"\n{'chip':10s} {'TRM regs':>9s} {'resolved':>9s} {'rate':>7s}")
    total = resolved = 0
    for chip_dir in chip_dirs(trm_root):
        canonical = vocab.chip_for_trm_folder(chip_dir.name)
        if canonical is None:
            continue
        names: set[str] = set()
        for chapter in chapter_files(chip_dir):
            doc = expand_document(chapter, chip_dir, trm_root)
            # Parameterised names (SPI_MEM_C_{n}_REG) render per-bank and can't be
            # compared by exact name; counting them would understate the rate.
            names.update(r.name for r in registers_in(doc.text, chapter.name) if not r.parameterised)
        hits = len(names & symbols_by_chip.get(canonical, set()))
        total += len(names)
        resolved += hits
        typer.echo(f"{canonical:10s} {len(names):9d} {hits:9d} {hits / max(1, len(names)):6.1%}")
    typer.echo(f"{'TOTAL':10s} {total:9d} {resolved:9d} {resolved / max(1, total):6.1%}")


if __name__ == "__main__":
    app()
