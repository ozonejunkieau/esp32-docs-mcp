# esp32-docs-mcp

An MCP server exposing semantic search over ESP32 documentation: the ESP-IDF
guides/API reference, and the per-chip Technical Reference Manuals. Chunks are
embedded locally with MLX and stored in LanceDB.

Python 3.12+, `uv` only (never `pip` or `uv pip`). Flat module layout, no `src/`.

## Pipeline

Each stage writes a file the next stage reads, so any stage can be re-run without
redoing the ones before it. Embedding is by far the most expensive step; keep it
last and keep its input reproducible.

```
ESP-IDF repo ──build_idf_docs.sh──▶ Sphinx XML (per target)
                                         │ ingest_sphinx_xml.py
                                         ▼
                                  chunks_<chip>.jsonl (one per target)
                                         │ dedup_chunks.py
                                         ▼
                                  idf_chunks.jsonl (deduplicated)
                                         │ embed_and_store.py
                                         ▼
                                  esp_docs.lancedb ◀── mcp_server.py
```

- `build_idf_docs.sh` — builds ESP-IDF docs to docutils XML, one dir per chip.
- `sphinx_xml.py` — XML → `SectionNode`/`Block` tree → chunks.
- `chunking.py` — source-agnostic chunk assembly (`build_chunks`,
  `merge_undersized_chunks`). Knows nothing about XML or LaTeX; both corpora
  chunk identically so retrieved passages have the same shape.
- `ingest_sphinx_xml.py` — walks one target's build, writes per-target JSONL.
- `dedup_chunks.py` — collapses identical chunks across targets.
- `embed_and_store.py` — embeds and writes LanceDB rows.
- `embedder.py` — `mlx-community/Qwen3-Embedding-4B-4bit-DWQ`, 2560-dim.
  Asymmetric: queries get an instruction prefix, documents do not.

  **Changing the model means changing `schema.EMBEDDING_DIM` to match** (4B is
  2560, 8B 4096, 0.6B 1024) and recreating the table — LanceDB fixes the vector
  width at creation. Do it before an embedding run, never after.

  Measured throughput on this corpus: 8B **0.30 chunks/s (~10.3 h)**, 4B
  **0.52 chunks/s (~5.9 h)** — roughly proportional to parameter count, so the
  model is the dominant cost. `embed_and_store.py` then sorts chunks by length
  before batching, which removes a measured 2.18x padding tax (a batch costs its
  longest member times its size, and chunk lengths span two orders of magnitude
  here) for a further ~1.8x, landing near 3 h.

  If throughput still bites, tune `BATCH_SIZE` before reaching for the 0.6B —
  its quality drop is much steeper (~6 MTEB points versus ~1).
- `chip_vocab.py` / `chips.yaml` — verified chip vocabulary and doc coverage.
- `check_thin_files.py` — thin-file and capture-rate checks; `xml` and `latex`
  subcommands, one per corpus. Run after every ingest.
- `register_census.py` — counts TRM registers in source and checks they survive
  into chunks. Exits non-zero past a shortfall threshold.
- `trm_verify.py` — shared source-side helpers for the TRM checkers. Deliberately
  does **not** import `latex_parser`: a checker that shares the parser's include
  resolution cannot detect a bug in it. **Known limitation**: `expand_document`
  does not evaluate `\iffalse` or inactive `\tagged`, so it counts source the
  parser rightly excludes and censuses read ~8 registers low. Fix it in this
  module, never by importing the parser — that would forfeit the independence.
- `make_trm_fixture.py` — synthesises faithful and deliberately-broken TRM chunk
  JSONL so the two checkers above can be proven to discriminate.
- `validate_store.py` — LanceDB row count, sample rows, search round-trip.

## Why the docs must be *built*, not parsed

This is the single most important thing about this codebase. **Never chunk
ESP-IDF `.rst` sources directly.** ESP-IDF documentation is a build, not a set of
standalone files:

- `{IDF_TARGET_SOC_CPU_CORES_NUM}` and friends are substituted at build time from
  soc_caps headers and Kconfig. Parsing `.rst` leaves the literal placeholder text
  in the embedding.
- `only::` branches are per-target. Parsing `.rst` merges mutually exclusive
  per-chip branches into one self-contradictory chunk.
- `api-reference/` content comes from doxygen-generated `.inc` files that do not
  exist in the source tree at all.

An earlier version of this project did parse `.rst`, with hand-rolled
substitution and an `only::` shim. It produced 3,040 chunks of which 133 still
carried unresolved placeholders, and it had zero API reference content. That
approach is deleted; do not reintroduce it.

**Use the XML builder output, not the `.doctrees` pickles.** Sphinx writes
`.doctree` at the end of the *read* phase, before post-transforms — `only::`
nodes are still present and unevaluated there. The XML builder serializes
post-resolution trees. XML also avoids pinning this project to the doc build's
exact docutils/Sphinx/esp_docs versions, which pickles would require.

## Environment traps

Each of these cost real time to diagnose. They fail in ways that point somewhere
other than the cause.

1. **`VIRTUAL_ENV` leaks into the docs build.** The project `.venv` (docutils
   0.23) silently overrides every dependency pin, and esp-docs needs docutils
   <0.21. Symptom: `No module named 'docutils.utils.error_reporting'`, or pins
   that appear to be ignored. Always `unset VIRTUAL_ENV` before a docs build.
   `build_idf_docs.sh` does this.

2. **`libxcb.dylib` not found** (macOS). `sphinxcontrib-wavedrom → cairosvg →
   cairocffi → xcffib` resolves libxcb via ctypes, which doesn't search
   `/opt/homebrew/lib`. Fix is `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`.
   Note macOS strips `DYLD_*` across hardened-runtime re-execs, so setting it and
   then going through esp-docs' own multiprocessing spawn does *not* work — this
   is why the build runs inside the IDF python env.

3. **The docs build needs a full ESP-IDF toolchain install.** It runs
   `idf.py set-target` on a dummy project to extract component info. This is not
   skippable: it is the step that produces the constants. Install with
   `./install.sh <target> --enable-docs`. Tool sets are per IDF version; having
   v5.4 installed does nothing for a v6.x checkout.

4. **`build-docs` exits non-zero on warnings.** It compares against
   `sphinx-known-warnings.txt` and fails on anything new (doxygen `@ingroup`
   warnings do this routinely). Pages are still written. Check for output before
   concluding the build failed.

5. **doxygen XML and Sphinx XML share a directory.** esp-docs writes doxygen's
   XML into the same tree the XML builder targets. Filter to Sphinx pages by
   their `<document>` root — `sphinx_xml.is_sphinx_page()`. Never filter by
   extension or assume every `.xml` is a page.

## Invariants

**`chips` is authoritative, not heuristic.** A chunk's presence in a target's
build *proves* it applies to that chip. Dedup collapses byte-identical chunks
across targets into one row listing every chip it appeared under. Content common
to all chips lists all of them, so there is no empty-list "chip-agnostic" case:
a chip-scoped query is a plain `array_contains(chips, 'X')`. TRM chunks use the
single `chip` column instead (one manual per chip); the two are different
columns, which is why `mcp_server._build_where` scopes each `doc_type`
separately rather than OR-ing conditions across all rows.

**Dedup is exact-match only, deliberately.** Where targets differ by even one
substituted constant ("2 cores" vs "1 core"), the chunks must stay separate and
chip-scoped — that difference is exactly what a chip-specific question needs.
Normalizing text to raise the collapse rate would destroy the most valuable
content in the corpus. Do not "improve" this into fuzzy matching.

The dedup key is `(file_path, text)`, not `text` alone: identical text on two
different pages is real duplication within one target's docs and each location
stays independently citable.

**Every chunk records the upstream revision it came from.** `provenance.py` reads
the source repo's git description and commit at ingest time; `embed_and_store.py
--source-repo <path>` stamps them onto every row. Both corpora track moving
upstreams — ESP-IDF docs change weekly, the TRM sources are explicitly ahead of
the published PDFs — so a register-level claim with no revision attached cannot
be checked or reproduced. It is per-row deliberately, so a search result can cite
"per ESP-IDF v6.1-dev-6485" without a second lookup; a column of one distinct
value costs almost nothing under Lance's dictionary encoding.

Provenance is constant across an ingest, so it can be added to an existing table
without recomputing embeddings — `backfill_provenance.py <source-repo>`. Use that
rather than re-embedding, which is hours of GPU time for metadata.

**`revisions` handles silicon-revision manual variants** (TRM only; empty for
IDF). ESP32-P4 ships a mainline TRM *and* a separate "Chip Revision v1.3" one,
and their register sets diverge both ways — 48 registers only in v1.3, 60 only in
mainline. Both are ingested and deduplicated, exactly as `chips` handles targets.
Labels are `v1.3` and `mainline`; **never "latest"** — that is the tag's internal
name, not a published one, and it would rot when new silicon ships. See
`LATEX.md`. Because it is empty for IDF rows, a revision filter must be scoped to
`doc_type = 'trm'`, the same way `chip` is.

**`chips.yaml` tracks two independent coverage facts.** `idf_docs` (is it in
`conf_common.py`'s `idf_targets`?) and `trm_folder` (is a TRM published?). Neither
implies the other — `esp32c61`/`esp32h2` have TRMs but no docs build; `esp32s31`
has a docs build but no TRM. All 13 keys are real silicon, matching
`components/soc/`. Re-verify against those upstream sources rather than guessing.

## Verify output, don't trust counts

Two silent-loss bugs in this codebase were invisible in aggregate totals and only
surfaced by comparing extracted text against the source:

- `field_list` was skipped as "page metadata" (true in RST, false in Sphinx API
  output, where it holds every function's Parameters/Returns docs). 4,222 nodes
  silently dropped.
- `literal_strong` was treated as block-level, breaking parameter names away from
  their descriptions.

`check_thin_files.py` found the first by flagging one page at 60% capture while
the corpus total looked healthy. **Run it after every ingest.** A useful stronger
check is comparing per-page word counts against the XML's own `itertext()` — a
page should land near or above 100% (breadcrumb prefixes repeat the section path,
so >100% is normal; well below is content loss).

When adding tag handling to `sphinx_xml.py`, audit what's unclassified across the
whole corpus rather than reasoning from one page — count tags not in
`_INLINE_TAGS` or `_SKIP_TAGS` and check the frequent ones are genuinely blocks.

**Test the checkers themselves.** A safety net nobody has fallen into is untested
equipment, and thresholds chosen against no data are guesses.
`make_trm_fixture.py` generates two synthetic corpora from the real LaTeX — one
faithful, one with includes deliberately unresolved — and both TRM checkers must
tell them apart. Current measured behaviour:

| | register census | capture rate |
|---|---|---|
| `--mode good` | 100.0%, exit 0 | median 92%, nothing flagged |
| `--mode broken` | 2.2%, exit 1 | median 52%, 177 files flagged |

Regenerate and re-run both after changing either checker. Note the fixture must
cover **all** chips (the default): `register_census.py` compares against the
whole corpus, so a single-chip fixture makes its total meaningless — it will read
as a catastrophic shortfall when nothing is wrong.

Measure with the repo's own tooling rather than ad-hoc shell. Several numbers
reported during this project were wrong because `find`/`grep` were run against
the `trm_latex` **symlink**, which `find` does not traverse without `-L`, and
because filtering TRM includes by `__EN` filename drops the 1,476 registers that
live in lowercase `_en.tex` files.

## Current state

**The ESP-IDF pipeline works end to end, and all nine targets are built and
deduplicated.** 3,168 pages → 43,274 chunks → **11,157 unique after dedup
(74.2% collapsed)**, in `idf_chunks.jsonl`. Zero unresolved placeholders, zero
page failures, zero pages below 80% capture.

The dedup spread is bimodal and is the evidence that exact-match collapsing is
right: 2,669 chunks apply to all nine chips, 5,811 to exactly one. Of those
single-chip chunks, 81% are per-chip variants of the *same* passage (same page,
same section, different text) — e.g. the "About" page states different radios
and cores per chip. Collapsing those would be a correctness bug.

Remaining for the IDF half: embed and rebuild the store (see below).

**Incomplete — the TRM half. See `LATEX.md` for the full implementation plan;
read it before touching `latex_parser.py`.** In short: the input is Espressif's
public TRM LaTeX repo (resolve via `$TRM_PATH`, the gitignored `./trm_latex`
symlink, or `~/git/esp-technical-reference-manual-latex`), *not* compiled PDFs —
the earlier PDF-download approach is deleted.

**Phases 1-3 of `LATEX.md` are done and independently verified**:
`latex_parser.py` resolves the include graph, evaluates tag conditionals,
recovers **13,597/13,597 registers (100%, exact on all ten chips)** with zero
unresolved includes, and emits `chunking.RawChunk` through the shared
`build_chunks`/`merge_undersized_chunks`. Entry points are
`resolve_document(path)` and `chunk_document(path)`.

Registers are `Block(atomic=True)` and verified never split — 13,597 intact, 0
split. Atomicity is load-bearing, not decorative: **28 registers exceed the
400-word cap on their own** (largest 518) and hit the splitter directly. The IDF
pipeline is byte-identical after the port (SHA-1
`cb3e97a2b9f72a12c572aeb3bd202c802d019baf`).

`ingest_trm.py` (phase 4) is written and validated: **10,515 chunks across all
ten manuals, 99.9% register capture** (10,092/10,100), 0 thin files, 90% median
word capture. It derives each chip's revision variants from the aggregator
documents' own `\usetag` declarations rather than hardcoding them, and warns when
the source disagrees with `chips.yaml` — that file drives the MCP revision
filter, so drift would mislabel results. ESP32-P4 splits into 2,394 chunks common
to both revisions, 162 mainline-only, 142 v1.3-only.

`register_census.py check` reports 8 registers missing. **They are a checker
artifact, not content loss**: 6 sit inside `\iffalse … \fi` and 2 inside
`\tagged{ESP32-H21}` — content for a different chip. `latex_parser` evaluates
both constructs and is right to exclude them; `trm_verify` does not, so it
overcounts.

**Confirm a check before believing its finding.** Register names here routinely
contain spaces and parentheses (`AES_KEY_n_REG (n: 0-7)`), so a pattern like
`Register (AES_[^\s]+) at address` silently matches none of the ~1,156
parameterised registers and reports them as lost when they render perfectly.
Validate any missing-content check against an item known to be present. See
`LATEX.md`.

Two register counts, both correct: `register_census.py source` reports **13,707**
(raw `\begin{register}`, pre-tag-selection); the parser emits **13,597** after
selecting `\iftagged` branches. The 110 difference is unselected branches — 0.8%,
under the census's 2% threshold. Don't hunt for it. A count near **11,539** is
the real alarm: that means includes are being filtered by `__EN` filename.

Note the ESP-IDF "build it, never parse source" rule does **not** apply to the
TRM: its build-time dependencies are shallow (file inclusion plus a few `\def`'d
values) and compiling would yield PDFs — less structure, not more. `\ifglobal` is
build scaffolding, but `\iftagged` **does** gate content in 48 files and must be
evaluated rather than flattened. `LATEX.md` explains all of this so it doesn't
get "fixed" back.

**The LanceDB store is stale.** It still holds the 3,040 old `.rst`-derived
chunks. `idf_chunks.jsonl` (11,157 deduped chunks) is ready to replace them:

    uv run embed_and_store.py idf_chunks.jsonl --doc-type idf --overwrite

Note `--overwrite` destroys the old rows, and the `.rst` pipeline that produced
them is deleted, so they are not reproducible. They are also known-bad (133
unresolved placeholders, no API reference), so this is the right trade — just not
one to make accidentally.

**Nothing is committed.** The repo still has zero commits. `.gitignore` now
excludes the generated artifacts (`esp_docs.lancedb/`, `*.jsonl`, `trm_latex`,
`.DS_Store`), so `git status` is source-only and safe to stage.

**Toolchains are per IDF version, and a present directory is not enough.** The
esp32/esp32s2/esp32s3 doc builds failed `idf.py set-target` at CMake even though
`~/.espressif/tools/xtensa-esp-elf/` existed — it held 14.2.0 and 15.2.0 from
other checkouts, while this tree needs 16.1.0. `./install.sh <targets>
--enable-docs` resolves it. If `set-target` dies at CMake, check the toolchain
*version* before assuming anything is missing.

Conventional Commits; never `git add -A`.
