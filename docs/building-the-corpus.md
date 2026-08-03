# Building the corpus

There is no prebuilt index to download. The store is derived data — every row
records the upstream commit it came from — so building it locally is what makes
it trustworthy rather than merely available.

Three independent pipelines write into one LanceDB table:

```
ESP-IDF repo ──build_idf_docs.sh──▶ Sphinx XML (per target)
                                        │ ingest_sphinx_xml.py
                                        ▼
                                 chunks_<chip>.jsonl
                                        │ dedup_chunks.py
                                        ▼
                                 idf_chunks.jsonl ──┐
                                                    │ embed_and_store.py
TRM LaTeX repo ──ingest_trm.py──▶ trm_chunks.jsonl ─┤
ESP-IDF soc/ ──ingest_source.py──▶ src_chunks.jsonl ─┤
                                                    ▼
                                            esp_docs.lancedb ◀── mcp_server.py
```

Each stage writes a file the next stage reads, so any stage can be re-run without
redoing the ones before it. Embedding is by far the most expensive step; it is
last, and its input is reproducible.

Every command below has a `just` recipe, listed alongside it. The recipes are
the authoritative spelling — several flags are load-bearing — and `just --list`
also records what a healthy result from each check looks like. See
[development.md](development.md) for the full recipe reference and for the
guards on the two destructive ones.

## Cost, before you start

- **ESP-IDF docs build: roughly 20 minutes per chip target**, three in parallel by
  default.
- **Chunking and dedup: minutes.**
- **Embedding: hours.** Measured throughput for the 4B model on this corpus is
  0.52 chunks/s, which length-sorted batching brings to around three hours for
  the ESP-IDF half. Start it when you do not need the machine.
- **Disk:** the ESP-IDF docs build output, three JSONL corpora, and a LanceDB
  store of 25,362 vectors at 2560 dimensions each (~716 MB).

## What you are producing, licence-wise

The store holds verbatim documentation text, not just vectors, so it inherits the
upstream terms: the ESP-IDF half is Apache-2.0, and the Technical Reference
Manual half is **CC-BY-SA 4.0**. That second one is copyleft, so a store you
build here is not yours to republish under a permissive licence.

Building it for your own use is unencumbered. Distributing it — as a release
asset, a shared volume, or a hosted index — means honouring ShareAlike and
attribution. See [Upstream licensing](../README.md#upstream-licensing-of-the-corpus)
before you publish anything derived from it.

## Prerequisites

### For the ESP-IDF half

An ESP-IDF checkout installed **with the docs feature**, plus `doxygen`:

```bash
brew install doxygen
cd ~/git/esp-idf && ./install.sh esp32p4 --enable-docs
```

Install every target you intend to index. This is not optional and not
skippable: the docs build runs `idf.py set-target` against a dummy project to
extract component information, and that is the step which produces the
substituted constants. A target directory merely existing is not enough —
toolchains are per IDF version, and a v5.4 install does nothing for a v6.x
checkout. If `set-target` dies at CMake, check the toolchain *version* before
assuming something is missing.

### For the TRM half

A clone of Espressif's
[TRM LaTeX sources](https://github.com/espressif/esp-technical-reference-manual-latex).
The tools resolve it in this order:

1. `$TRM_PATH`
2. `./trm_latex` — a local symlink, gitignored because absolute-path symlinks do
   not travel between machines
3. `~/git/esp-technical-reference-manual-latex`

`TRM_PATH` is the portable mechanism; the symlink is convenience.

## ESP-IDF pipeline

```bash
# 1. Build the docs to docutils XML (3 targets in parallel).      just build-idf-docs
#    Output: $IDF_PATH/docs/_build/en/<target>/xml/
export IDF_PATH=~/git/esp-idf
./build_idf_docs.sh esp32 esp32s2 esp32s3 esp32s31 esp32c2 esp32c3 esp32c5 esp32c6 esp32p4

# 2. Chunk each target's build.                                   just chunk-idf
for chip in esp32 esp32s2 esp32s3 esp32s31 esp32c2 esp32c3 esp32c5 esp32c6 esp32p4; do
    uv run ingest_sphinx_xml.py "$IDF_PATH/docs/_build/en/$chip/xml" \
        --chip "$chip" --out-path "chunks_$chip.jsonl"
done

# 3. Collapse chunks identical across chips into one row each.    just dedup-idf
uv run dedup_chunks.py chunks_*.jsonl --out-path idf_chunks.jsonl

# 4. Embed and store. --overwrite recreates the table -- see the warning below.
#                                     just embed-idf-fresh confirm=rebuild-whole-store
uv run embed_and_store.py idf_chunks.jsonl --doc-type idf --overwrite \
    --source-repo "$IDF_PATH"
```

The nine targets currently yield **3,168 pages → 43,274 chunks → 11,157 unique**
after dedup (74.2% collapsed).

`build_idf_docs.sh` handles two environment traps for you — it unsets
`VIRTUAL_ENV` (the project venv's docutils otherwise silently overrides esp-docs'
pin) and sets `DYLD_FALLBACK_LIBRARY_PATH` for Homebrew's libxcb. It also exits
non-zero when the build produces warnings that are not in
`sphinx-known-warnings.txt`, which happens routinely. **Pages are still written.**
The script prints the page count per target so you can tell a warning from a
failure.

Pass `--source-repo` so every row records the ESP-IDF revision it came from.
Without it the run prints a warning and the rows carry no provenance, which
cannot be reconstructed later from the rows themselves — only from remembering
which checkout was current. If it does happen, `backfill_provenance.py` fixes it
without re-embedding.

## TRM pipeline

```bash
uv run ingest_trm.py --out-path trm_chunks.jsonl                          # just chunk-trm
uv run embed_and_store.py trm_chunks.jsonl --doc-type trm \
    --source-repo "$TRM_PATH"                                             # just embed-trm
```

**No `--overwrite` on that second command** — the TRM rows append alongside the
ESP-IDF ones in the same table.

There is no dedup step: one manual per chip, so `dedup_chunks.py` does not apply.
`ingest_trm.py` does deduplicate internally across *silicon revisions*, parsing
each chip's chapters once per revision variant it publishes and merging the
results, so a chunk appears once carrying every revision it applies to. Current
output: **10,515 chunks across ten manuals**, with ESP32-P4 splitting into 2,394
chunks common to both revisions, 162 mainline-only and 142 v1.3-only.

## A third corpus: ESP-IDF SoC headers

`ingest_source.py` chunks the ESP-IDF SoC headers under `components/soc/` as
`doc_type = "src"` — the C counterpart to the manuals, where the TRM *describes*
a register and the header *defines* its address and bitmasks. It reads
`$IDF_PATH` (falling back to `~/git/esp-idf`) and writes JSONL like the other
two ingests, so it embeds through the same `embed_and_store.py`. `src` rows are
chip-scoped through the `chips` **list**, like ESP-IDF rows. Most carry no
`revisions`, but ESP32-P4's do: IDF splits its register headers by silicon
revision exactly as the manuals do.

```bash
uv run ingest_source.py ingest --out-path src_chunks.jsonl \
    --idf-path "$IDF_PATH"                                                # just chunk-src
uv run embed_and_store.py src_chunks.jsonl --doc-type src \
    --source-repo "$IDF_PATH"                                             # just embed-src
```

**No `--overwrite`** — `src` rows append alongside the other two corpora.

Current output: **3,690 chunks** from 2,036 files, about two hours to embed.

Register headers are ingested as **one card per file** — path, leading comment,
declared symbols — rather than as full text. Chunking them as prose measures
28,652 chunks of `#define` walls that nobody retrieves semantically; cards give
the same `symbol_refs` coverage for 2,754. `--no-registers` drops them entirely,
at the cost of TRM register resolution falling from 78.3% to 1.8%.

That 78.3% is the check worth running (`just check-src-registers`): it measures
how many register names the manuals document actually resolve to a `#define` in
the matching chip's headers. A collapse there means symbol extraction broke, and
no chunk count would show it.

Note the headers moved upstream — ESP-IDF v6.x relocated them from
`components/soc/*/include/soc/` to `register/soc/`, and ESP32-P4 splits further
into `hw_ver1`/`hw_ver3`, which map to the manuals' `v1.3`/`mainline`. Scoping to
the old path yields 1.8% and looks like a broken parser rather than a moved
directory, so re-check after any ESP-IDF upgrade.

## Verify before you trust it

Aggregate counts do not catch silent content loss — every content-loss bug this
project has had was invisible in totals and was found only by comparing
extracted text against the source. Run the checkers after every ingest.

```bash
# Store-level: row count, doc_type breakdown, samples, search round-trip.
uv run validate_store.py                                          # just validate

# ESP-IDF: files that produced suspiciously few chunks, or captured a low share
# of their source's words.
uv run check_thin_files.py xml chunks_esp32p4.jsonl \
    --xml-root "$IDF_PATH/docs/_build/en/esp32p4/xml"              # just check-idf-thin

# TRM: same idea, plus the register census.                        just verify-trm
uv run check_thin_files.py latex trm_chunks.jsonl                 # just check-trm-thin
uv run register_census.py source                                  # just census-source
uv run register_census.py check trm_chunks.jsonl                  # just census-check
```

`register_census.py source` currently reports **13,707** registers across 379
chapters — a raw count of `\begin{register}` before tag selection. The parser
emits 13,597 after selecting `\iftagged` branches; the 110 difference is
unselected branches and is correct. A count near **11,539** is the real alarm:
that means includes are being filtered by `__EN` filename, which drops 1,476
registers living in lowercase `_en.tex` files.

`register_census.py check` compares that source census against the chunk output,
per chip, and exits non-zero past a shortfall threshold. **A small shortfall is
not automatically content loss**: some source registers are disabled upstream
inside `\iffalse` or tagged for a different chip, and the parser is right to drop
them while the checker may still count them. Read the named registers before
treating a shortfall as a regression — see
[trm-latex.md](trm-latex.md#verification), which also records the expected
reading.

### Macro coverage

`latex_coverage_check.py` reports every LaTeX macro and environment used in the
TRM sources that the parser does not explicitly handle, most frequent first. It
takes the **directory to scan as a required positional argument**, so it works
on one chip or on the whole repo:

```bash
uv run latex_coverage_check.py "$TRM_PATH"                        # just latex-coverage
uv run latex_coverage_check.py "$TRM_PATH/ESP32-C3"               # one chip
uv run latex_coverage_check.py "$TRM_PATH" --top-n 60             # default is 40
```

Run it after pulling new TRM sources, and after any change to `latex_parser.py`.
A macro appearing more than ~50 times and still unknown is worth investigating;
most of what remains is correctly ignorable. What "normal" currently looks like,
and how to read the report, is in
[trm-latex.md](trm-latex.md#macro-coverage).

### The checkers themselves

`make_trm_fixture.py` synthesises a faithful corpus and a deliberately broken one
from the real LaTeX, and both checkers must tell them apart:

```bash
just fixtures-verify
```

Regenerate and re-run after changing either checker. The expected readings are in
`just --list` and in [development.md](development.md#fixtures--proving-the-checkers-still-discriminate).

## Refreshing

Both upstreams move: ESP-IDF docs change weekly, and the TRM sources run ahead of
the published PDFs. Refreshing is a manual re-run, and how much of it you re-run
depends on what moved.

**`--overwrite` recreates the whole table, not one corpus.** It is the right flag
for a from-scratch rebuild and the wrong flag for refreshing half the store,
because it will take the other half with it.

- **Both corpora moved, or you changed the chunker or the embedding model.**
  Re-run both pipelines end to end. Use `--overwrite` on the *first*
  `embed_and_store.py` call and plain append on the second. A model change also
  requires updating `schema.EMBEDDING_DIM` to match before the run — LanceDB
  fixes the vector width at table creation.

- **Only one corpus moved.** Delete that `doc_type`'s rows, then append the new
  ones:

  ```bash
  just delete-corpus idf        # prompts, then prints the remaining row count
  just embed-idf-append
  ```

  The delete is a few lines of LanceDB if you would rather run it directly:

  ```bash
  uv run python - <<'PY'
  import lancedb
  t = lancedb.connect("./esp_docs.lancedb").open_table("chunks")
  t.delete("doc_type = 'idf'")
  print(t.count_rows(), "rows remain")
  PY

  uv run embed_and_store.py idf_chunks.jsonl --doc-type idf --source-repo "$IDF_PATH"
  ```

  Substitute `'trm'`, `trm_chunks.jsonl` and `"$TRM_PATH"` for the other
  direction. Confirm the remaining row count matches the corpus you meant to
  keep *before* embedding anything.

- **Only provenance is wrong** — rows embedded without `--source-repo`.
  `backfill_provenance.py <source-repo>` stamps them without recomputing a single
  embedding, which is seconds instead of hours. It has a `--dry-run`; use it.

  ```bash
  # or: just backfill-provenance "$IDF_PATH" --doc-type idf --dry-run
  uv run backfill_provenance.py "$IDF_PATH" --doc-type idf --dry-run
  ```

  It stamps one constant revision across the rows it touches, which is only
  correct for a table that came from **one** ingest. **On a table that mixes
  corpora, pass `--doc-type`** — that is the guard, and it refuses rather than
  stamping half the table with the wrong upstream:

  ```
  11157 rows have a different doc_type and would be stamped with the wrong revision.
  Refusing. Stamp each corpus before mixing them, or extend this script.
  ```

  That is the tool working correctly, not a failure. On a mixed table the fix is
  to delete the unstamped `doc_type`'s rows and re-embed that corpus with
  `--source-repo`.

Whatever you re-run, re-run the checkers afterwards, and re-check
`chips.yaml` if a new target has appeared upstream — its header records exactly
which upstream sources each field is verified against.
