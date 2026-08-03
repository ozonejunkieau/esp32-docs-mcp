# CLAUDE.md — operating guide

An MCP server exposing semantic search over ESP32 documentation: the ESP-IDF
guides and API reference, and the per-chip Technical Reference Manuals. Chunks
are embedded locally with MLX and stored in LanceDB.

Python 3.12+, `uv` only — never `pip` or `uv pip`. Flat module layout, no `src/`.

This file is what you need to work here **safely**. The reasoning behind the
design lives elsewhere, and is worth reading before changing anything
structural:

| Read | For |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Why the pipelines are shaped this way; what was tried and deleted |
| [docs/trm-latex.md](docs/trm-latex.md) | The TRM LaTeX ingest — **read before touching `latex_parser.py`** |
| [docs/building-the-corpus.md](docs/building-the-corpus.md) | Running a build or a refresh |
| [docs/development.md](docs/development.md) | The `justfile`, the tests, the fixtures |
| [docs/usage.md](docs/usage.md) | Querying the server and reading results |
| [README.md](README.md) | The public description |

## Hard rules

**Never chunk ESP-IDF `.rst` sources.** The documentation is a build, not a set
of files: constants are substituted from `soc_caps`/Kconfig at build time,
`only::` branches are selected per target, and the entire API reference is
doxygen output that does not exist in the source tree. Consume the XML builder's
output — not the `.doctrees` pickles, which are written before `only::` is
evaluated. Rationale and the deleted `.rst` attempt:
[architecture.md](docs/architecture.md#why-the-esp-idf-docs-must-be-built-not-parsed).

**That rule does not transfer to the TRM.** Its build-time dependencies are
shallow and compiling would yield PDFs — less structure, not more. Parsing the
LaTeX source is correct there. `\ifglobal` is build scaffolding, but `\iftagged`
**does** gate content in 48 files and must be evaluated rather than flattened.
See [trm-latex.md](docs/trm-latex.md#why-parsing-the-source-is-right-here) so it
does not get "fixed" back.

**`chips` is authoritative, not heuristic** — a chunk's presence in a target's
build proves it applies to that chip, and content common to all chips lists all
of them, so there is no empty-list "chip-agnostic" case. TRM chunks use the
singular `chip` column instead. Different columns, which is why
`mcp_server._build_where` scopes each `doc_type` separately rather than OR-ing
conditions across all rows.

**Dedup is exact-match only, deliberately**, keyed on `(file_path, text)` rather
than `text` alone. Chunks differing by one substituted constant ("2 cores" vs
"1 core") must stay separate and chip-scoped. Do not "improve" this into fuzzy
matching.

**A revision filter must be scoped by the `revisions` column, never by
`doc_type`.** This assumption has caused two separate bugs. `revisions` is not
TRM-only: ESP32-P4's SoC register headers split into `hw_ver1`/`hw_ver3`, which
map to `v1.3`/`mainline`, so 723 `src` rows carry one. Exempting rows by naming a
corpus (`doc_type = 'idf'`, then `doc_type != 'trm'`) returned a hw_ver3-only
register to a caller asking about v1.3 silicon. Test the column — an empty
`revisions` is what means "no revision axis" — and treat "every TRM row carries
revisions" as an ingest invariant, since an empty one now reads as universal.
Labels are `v1.3` and `mainline` — **never "latest"**, which is the LaTeX tag's
internal name and would rot.

**Always pass `embed_and_store.py --source-repo <path>`.** Without it rows carry
no provenance, and it cannot be reconstructed from the rows later. If it
happens, `backfill_provenance.py <source-repo>` fixes it in seconds — never
re-embed for metadata.

**Changing the embedding model means changing `schema.EMBEDDING_DIM`** to match
(4B is 2560, 8B 4096, 0.6B 1024) *and* recreating the table. LanceDB fixes the
vector width at creation. Do it before an embedding run, never after.

**Estimate embedding time from chunk *count*, not word count.** Measured 0.52
chunks/s on ESP-IDF prose and 0.51 on the SoC header cards, despite very
different chunk lengths — per-chunk overhead dominates, so roughly
`chunks / 0.5` seconds. Scaling on total words underestimated a 2-hour run as 1
hour. It also means chunk count is the lever: rendering register headers as
cards rather than prose cut them from 28,652 to 2,754, and the saving is
proportional to that ratio.

**Metadata columns can be repaired without re-embedding.** Vectors can be read
back and rewritten untouched, so a missing column is minutes rather than hours —
see `backfill_provenance.py` and `backfill_trm_symbols.py`. Never re-embed for
metadata. Write a verified backup before any delete-and-re-add, because a crash
between the two costs the whole corpus.

**`chips.yaml` tracks two independent coverage facts** — `idf_docs` and
`trm_folder` — and neither implies the other. All 13 keys are real silicon
matching `components/soc/`. Its header records exactly which upstream source
each field is verified against; re-verify there rather than guessing.

**Run the checkers after every ingest.** Aggregate totals do not catch silent
content loss — every content-loss bug this project has had was invisible in them.
[architecture.md](docs/architecture.md#verify-output-dont-trust-counts).

## Environment traps

Each of these cost real time to diagnose. They fail in ways that point somewhere
other than the cause.

1. **`VIRTUAL_ENV` leaks into the docs build.** The project `.venv` silently
   overrides every dependency pin, and esp-docs needs docutils <0.21. Symptom:
   `No module named 'docutils.utils.error_reporting'`, or pins that appear to be
   ignored. Always `unset VIRTUAL_ENV` before a docs build. `build_idf_docs.sh`
   does this.

2. **`libxcb.dylib` not found** (macOS). `sphinxcontrib-wavedrom → cairosvg →
   cairocffi → xcffib` resolves libxcb via ctypes, which does not search
   `/opt/homebrew/lib`. Fix is `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`.
   Note macOS **strips `DYLD_*` across hardened-runtime re-execs**, so setting it
   and then going through esp-docs' own multiprocessing spawn does *not* work —
   which is why the build runs inside the IDF python env.

3. **The docs build needs a full ESP-IDF toolchain install, of the right
   *version*.** It runs `idf.py set-target` on a dummy project to extract
   component info; that is the step which produces the constants, and it is not
   skippable. Tool sets are per IDF version and **a present directory is not
   enough**: the esp32/esp32s2/esp32s3 builds failed at CMake with
   `~/.espressif/tools/xtensa-esp-elf/` populated, because it held 14.2.0 and
   15.2.0 from other checkouts while this tree needed 16.1.0. Fix is
   `./install.sh <targets> --enable-docs`. If `set-target` dies at CMake, check
   the toolchain *version* before assuming anything is missing.

4. **`build-docs` exits non-zero on warnings.** It compares against
   `sphinx-known-warnings.txt` and fails on anything new (doxygen `@ingroup`
   warnings do this routinely). Pages are still written. Check for output before
   concluding the build failed — `build_idf_docs.sh` prints the page count per
   target for exactly this.

5. **doxygen XML and Sphinx XML share a directory.** esp-docs writes doxygen's
   XML into the same tree the XML builder targets. Filter to Sphinx pages by
   their `<document>` root — `sphinx_xml.is_sphinx_page()`. Never filter by
   extension or assume every `.xml` is a page.

6. **`find` does not traverse symlinks without `-L`.** `trm_latex` is a symlink,
   so ad-hoc `find`/`grep` over it silently measure nothing. Several numbers
   reported during this project were wrong for this reason. **Measure with the
   repo's own tooling** (`register_census.py`, `check_thin_files.py`) rather
   than shell one-liners.

7. **Filtering TRM includes by `__EN` filename drops 1,476 registers.** ESP32,
   ESP32-S2 and ESP8684 use lowercase `_en.tex` for included subfiles. Filter to
   English at the document root only; follow the include graph for everything
   else. A repo-wide register count near **11,539** instead of ~13,700 is this
   bug.

## Commands

`just` is the command surface; every documented invocation has a recipe, and
`just --list` carries the expected reading of each check. Full reference in
[development.md](docs/development.md).

```bash
just                     # list every recipe, grouped
just paths               # resolved IDF_PATH / TRM_PATH, and whether they exist
just test                # uv run pytest
just validate            # row count, doc_type breakdown, search round-trip
just verify-trm          # thin files + source census + census vs chunks
just serve               # run the MCP server on stdio
```

Two recipes destroy data and both prompt:

- **`just embed-idf-fresh`** passes `--overwrite`, which **recreates the table
  and therefore destroys the TRM rows too**. It additionally requires
  `confirm=rebuild-whole-store`. Correct only for a from-scratch rebuild with
  `just embed-trm` following immediately.
- **`just delete-corpus <idf|trm>`** removes one `doc_type`'s rows so that half
  can be refreshed alone.

The raw pipeline commands, in order, are in
[building-the-corpus.md](docs/building-the-corpus.md). Do not run an embed
casually: it is hours.

## Current state

The store at `./esp_docs.lancedb` holds **25,362 rows — 11,157 ESP-IDF, 10,515
TRM and 3,690 SoC-header chunks**.
passing.

**ESP-IDF half:** all nine targets built and deduplicated. 3,168 pages → 43,274
chunks → **11,157 unique after dedup (74.2% collapsed)**, in `idf_chunks.jsonl`.
Zero unresolved placeholders, zero page failures, zero pages below 80% capture.

**TRM half:** all ten manuals ingested. **10,515 chunks**, 13,597/13,597
registers recovered from source, 0 thin files, 90% median word capture, at
`source_version 87b1c88`. ESP32-P4 splits into 2,394 chunks common to both
revisions, 162 mainline-only, 142 v1.3-only, and `revision_scope` is verified
reading correctly through the real `esp32_docs_search` query path.

**Three corpora, 25,362 rows**: 11,157 `idf`, 10,515 `trm`, 3,690 `src`.
`ingest_source.py` chunks the ESP-IDF SoC headers under `components/soc/` as
`doc_type = "src"` — the C counterpart to the TRM, where the manual describes a
register and the header defines its address and bitmasks. Register headers are
one card per file, not full text: as prose they measure 28,652 chunks nobody
retrieves semantically, against 2,754 as cards for the same `symbol_refs`
coverage. `src` rows are chip-scoped through the `chips` **list** like ESP-IDF
rows; ESP32-P4's also carry `revisions`, because IDF splits those headers by
silicon revision as the manuals do.

`esp32_docs_find_symbol` does exact identifier lookup with no embedding, and
interleaves results across corpora — an exact match has no relevance score, so a
plain `limit(k)` dropped a whole corpus for 37% of the ~20,000 symbols spanning
more than one.

Cross-corpus coverage is measured, not assumed: `just check-src-registers`
reports the share of TRM register names resolving to a `#define` in the matching
chip's headers, currently **78.3%**. The residual is upstream divergence
(`PWM_*` against `MCPWM_*`, no `twai_reg.h` for esp32/esp32s2), not lost
extraction. A collapse there means symbol extraction broke, which no chunk count
would reveal.

## File map

| File | Purpose |
|---|---|
| `mcp_server.py` | The MCP server and its tools |
| `schema.py` | The stored chunk schema; `EMBEDDING_DIM` lives here |
| `chunking.py` | Heading-aware chunk assembly, shared by every corpus |
| `chips.yaml`, `chip_vocab.py` | Verified chip vocabulary and per-corpus coverage |
| `provenance.py`, `backfill_provenance.py` | Record / repair the upstream revision a chunk came from |
| `embed_and_store.py`, `embedder.py` | Embed into LanceDB; Qwen3-Embedding-4B via MLX (2560-dim) |
| `build_idf_docs.sh` | Builds ESP-IDF docs to XML for one or more chips |
| `sphinx_xml.py` | One built page's XML → `SectionNode`/`Block` tree → chunks |
| `ingest_sphinx_xml.py` | Walks one target's build, writes per-target JSONL |
| `dedup_chunks.py` | Collapses chunks identical across targets |
| `latex_parser.py` | TRM LaTeX → chunks: includes, tags, registers |
| `ingest_trm.py` | Chunks the TRM manuals, deduplicated across silicon revisions |
| `ingest_source.py` | Chunks the ESP-IDF SoC headers (`doc_type = "src"`) |
| `latex_coverage_check.py` | Macros/environments the LaTeX parser does not handle |
| `check_thin_files.py` | Thin-file and capture-rate checks (`xml` / `latex`) |
| `register_census.py` | Counts TRM registers in source and checks they survive |
| `trm_verify.py` | Source-side helpers for the TRM checkers. **Never imports `latex_parser`** — a checker sharing the parser's include resolution cannot detect a bug in it |
| `make_trm_fixture.py` | Synthetic corpora that prove the checkers discriminate |
| `validate_store.py` | LanceDB row count, samples, search round-trip |
| `justfile` | Every documented command, with the destructive ones guarded |
| `tests/` | `uv run pytest`; `slow` tests need a local corpus and skip without one |

Conventional Commits; never `git add -A`.
