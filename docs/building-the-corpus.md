# Building the corpus

There is no prebuilt index to download. The store is derived data — every row
records the upstream commit it came from — so building it locally is what makes
it trustworthy rather than merely available.

Two independent pipelines write into one LanceDB table:

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
                                                    ▼
                                            esp_docs.lancedb ◀── mcp_server.py
```

Each stage writes a file the next stage reads, so any stage can be re-run without
redoing the ones before it. Embedding is by far the most expensive step; it is
last, and its input is reproducible.

## Cost, before you start

- **ESP-IDF docs build: roughly 20 minutes per chip target**, three in parallel by
  default.
- **Chunking and dedup: minutes.**
- **Embedding: hours.** Measured throughput for the 4B model on this corpus is
  0.52 chunks/s, which length-sorted batching brings to around three hours for
  the ESP-IDF half. Start it when you do not need the machine.
- **Disk:** the ESP-IDF docs build output, two JSONL corpora, and a LanceDB store
  of 21,672 vectors at 2560 dimensions each.

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
# 1. Build the docs to docutils XML (3 targets in parallel).
#    Output: $IDF_PATH/docs/_build/en/<target>/xml/
export IDF_PATH=~/git/esp-idf
./build_idf_docs.sh esp32 esp32s2 esp32s3 esp32s31 esp32c2 esp32c3 esp32c5 esp32c6 esp32p4

# 2. Chunk each target's build.
for chip in esp32 esp32s2 esp32s3 esp32s31 esp32c2 esp32c3 esp32c5 esp32c6 esp32p4; do
    uv run ingest_sphinx_xml.py "$IDF_PATH/docs/_build/en/$chip/xml" \
        --chip "$chip" --out-path "chunks_$chip.jsonl"
done

# 3. Collapse chunks identical across chips into one row each.
uv run dedup_chunks.py chunks_*.jsonl --out-path idf_chunks.jsonl

# 4. Embed and store. --overwrite recreates the table -- see the warning below.
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
uv run ingest_trm.py --out-path trm_chunks.jsonl
uv run embed_and_store.py trm_chunks.jsonl --doc-type trm --source-repo ./trm_latex
```

**No `--overwrite` on that second command** — the TRM rows append alongside the
ESP-IDF ones in the same table.

There is no dedup step: one manual per chip, so `dedup_chunks.py` does not apply.
`ingest_trm.py` does deduplicate internally across *silicon revisions*, parsing
each chip's chapters once per revision variant it publishes and merging the
results, so a chunk appears once carrying every revision it applies to. Current
output: **10,515 chunks across ten manuals**, with ESP32-P4 splitting into 2,394
chunks common to both revisions, 162 mainline-only and 142 v1.3-only.

## Verify before you trust it

Aggregate counts do not catch silent content loss — both real content-loss bugs
in this project were invisible in totals and were found only by comparing
extracted text against the source. Run the checkers after every ingest.

```bash
# Store-level: row count, doc_type breakdown, samples, search round-trip.
uv run validate_store.py

# ESP-IDF: files that produced suspiciously few chunks, or captured a low share
# of their source's words.
uv run check_thin_files.py xml chunks_esp32p4.jsonl \
    --xml-root "$IDF_PATH/docs/_build/en/esp32p4/xml"

# TRM: same idea, plus the register census.
uv run check_thin_files.py latex trm_chunks.jsonl
uv run register_census.py source                  # source-side census only
uv run register_census.py check trm_chunks.jsonl  # census vs. chunk output
```

`register_census.py source` currently reports **13,707** registers across 379
chapters — a raw count of `\begin{register}` before tag selection. The parser
emits 13,597 after selecting `\iftagged` branches; the 110 difference is
unselected branches and is correct. A count near **11,539** is the real alarm:
that means includes are being filtered by `__EN` filename, which drops 1,476
registers living in lowercase `_en.tex` files.

`register_census.py check` reports **8 registers missing** on a good ingest and
that is the expected reading, not a regression. Six sit inside `\iffalse` blocks
and two are tagged for a different chip; the parser is right to exclude them and
the checker does not evaluate those constructs. Details in [../LATEX.md](../LATEX.md).

The checkers themselves are testable — `make_trm_fixture.py` synthesises a
faithful corpus and a deliberately broken one from the real LaTeX, and both
checkers must tell them apart. Regenerate and re-run both after changing either
checker; see [../CLAUDE.md](../CLAUDE.md) for the measured discrimination.

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
  ones. No script wraps this; it is two lines of LanceDB:

  ```bash
  uv run python -c "
  import lancedb
  t = lancedb.connect('./esp_docs.lancedb').open_table('chunks')
  t.delete(\"doc_type = 'idf'\")
  print(t.count_rows(), 'rows remain')
  "
  uv run embed_and_store.py idf_chunks.jsonl --doc-type idf --source-repo \"\$IDF_PATH\"
  ```

  Substitute `'trm'` and `trm_chunks.jsonl` for the other direction. Confirm the
  remaining row count matches the corpus you meant to keep *before* embedding
  anything.

- **Only provenance is wrong** — rows embedded without `--source-repo`.
  `backfill_provenance.py <source-repo>` stamps them without recomputing a single
  embedding, which is seconds instead of hours. It has a `--dry-run`.

  It works on a table that came from **one** ingest. On a table that already
  mixes both corpora it refuses, because the two have different upstreams and one
  constant expression cannot stamp them differently:

  ```
  11157 rows have a different doc_type and would be stamped with the wrong revision.
  Refusing. Stamp each corpus before mixing them, or extend this script.
  ```

  That is the tool working correctly, not a failure. On a mixed table, delete the
  unstamped `doc_type`'s rows and re-embed that corpus with `--source-repo`.

Whatever you re-run, re-run the checkers afterwards, and re-check
`chips.yaml` if a new target has appeared upstream — its header records exactly
which upstream sources each field is verified against.
