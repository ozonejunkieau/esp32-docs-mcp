# Architecture and design record

Why this project is shaped the way it is. `CLAUDE.md` states the rules an agent
has to follow; this document is the reasoning behind them, plus the history of
what was tried and deleted, so that neither gets rediscovered the expensive way.

For the mechanics of running a build see
[building-the-corpus.md](building-the-corpus.md); for the TRM LaTeX ingest
specifically see [trm-latex.md](trm-latex.md).

## Shape of the system

Each stage writes a file the next stage reads, so any stage can be re-run
without redoing the ones before it. Embedding is by far the most expensive step;
it is last, and its input is reproducible.

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

Both corpora chunk through the same module, `chunking.py`
(`build_chunks`, `merge_undersized_chunks`). It knows nothing about XML or
LaTeX. That is deliberate: a retrieved passage has the same shape whichever
corpus it came from, so a caller does not have to learn two result formats. The
cost is that a change made for one corpus reshapes the other, which is why the
ESP-IDF ingest is pinned byte-for-byte by a test — see
[development.md](development.md).

## Why the ESP-IDF docs must be *built*, not parsed

This is the single most important decision in the codebase. **Never chunk
ESP-IDF `.rst` sources directly.** ESP-IDF documentation is a build, not a set
of standalone files:

- `{IDF_TARGET_SOC_CPU_CORES_NUM}` and its siblings are substituted at build
  time from `soc_caps` headers and Kconfig. Parsing `.rst` leaves the literal
  placeholder text in the embedding.
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

**doxygen XML and Sphinx XML share a directory.** esp-docs writes doxygen's XML
into the same tree the XML builder targets, so pages are identified by their
`<document>` root via `sphinx_xml.is_sphinx_page()` — never by extension, and
never by assuming every `.xml` is a page.

### The rule does not transfer to the TRM

The Technical Reference Manuals are parsed from LaTeX source, and that is not a
contradiction. ESP-IDF needed a build because its content dependencies are
*deep* — constants from C headers, Kconfig values, doxygen output. The TRM's are
*shallow*: file inclusion, a handful of `\def`'d values, and one genuinely
content-gating conditional (`\iftagged`). Running the real toolchain would
produce PDFs, which carry less structure than the source, not more.
[trm-latex.md](trm-latex.md) sets this out in full so it does not get "fixed"
back.

## Chip applicability: proof, not inference

**`chips` is authoritative, not heuristic.** A chunk's presence in a target's
build *proves* it applies to that chip. Dedup collapses byte-identical chunks
across targets into one row listing every chip it appeared under. Content common
to all chips lists all of them, so there is no empty-list "chip-agnostic" case
to special-case: a chip-scoped query is a plain `array_contains(chips, 'X')`.

TRM chunks use the singular `chip` column instead — one manual per chip. The two
are different columns, which is why `mcp_server._build_where` scopes each
`doc_type` separately rather than OR-ing conditions across all rows.

**Dedup is exact-match only, deliberately.** Where targets differ by even one
substituted constant ("2 cores" vs "1 core"), the chunks must stay separate and
chip-scoped — that difference is exactly what a chip-specific question needs.
Normalizing text to raise the collapse rate would destroy the most valuable
content in the corpus. It is not to be "improved" into fuzzy matching.

The dedup key is `(file_path, text)`, not `text` alone: identical text on two
different pages is real duplication within one target's docs, and each location
stays independently citable.

**The collapse rate is bimodal, and that is the evidence the design is right.**
Across the nine targets, 2,669 chunks apply to all nine chips and 5,811 to
exactly one. Of those single-chip chunks, 81% are per-chip variants of the
*same* passage — same page, same section, different text; the "About" page
stating different radios and core counts per chip is the archetype. Collapsing
those would be a correctness bug, not a saving.

## Silicon revisions are a second applicability axis

Espressif publishes more than one manual for some chips: ESP32-P4 has a mainline
TRM *and* a separate "Chip Revision v1.3" one, and their register sets diverge
in both directions — 48 registers only in v1.3, 60 only in mainline. Picking one
variant silently loses the other's registers.

So both are ingested and deduplicated, exactly as `chips` handles targets, into
a `revisions` list column. Labels are `v1.3` and `mainline`, **never "latest"** —
that is the LaTeX tag's internal name, not a published one, and it would rot the
moment new silicon ships. Because `revisions` is empty for every non-TRM row, a
revision filter must be scoped to `doc_type = 'trm'`, the same way `chip` is.

The reasoning, the rejected two-pseudo-chips alternative, and how the variant is
derived from the sources are in [trm-latex.md](trm-latex.md).

## Provenance is per row, on purpose

Both corpora track moving upstreams — ESP-IDF docs change weekly, the TRM
sources run explicitly ahead of the published PDFs — so a register-level claim
with no revision attached cannot be checked or reproduced. `provenance.py` reads
the source repo's git description and commit at ingest time and
`embed_and_store.py --source-repo <path>` stamps them onto every row.

Per row rather than per table so a single search result can cite "per ESP-IDF
v6.1-dev-6485" without a second lookup. A column of one distinct value costs
almost nothing under Lance's dictionary encoding.

Provenance is constant across an ingest, so it can be added to an existing table
without recomputing embeddings — `backfill_provenance.py <source-repo>`. That is
seconds; re-embedding for metadata is hours of GPU time.

## The embedding model, and what it costs

`embedder.py` uses `mlx-community/Qwen3-Embedding-4B-4bit-DWQ`, 2560-dimensional,
asymmetric: queries get an instruction prefix, documents do not.

**Changing the model means changing `schema.EMBEDDING_DIM` to match** (4B is
2560, 8B 4096, 0.6B 1024) and recreating the table — LanceDB fixes the vector
width at creation. Do it before an embedding run, never after.

Measured throughput on this corpus: 8B **0.30 chunks/s (~10.3 h)**, 4B
**0.52 chunks/s (~5.9 h)** — roughly proportional to parameter count, so the
model is the dominant cost. `embed_and_store.py` then sorts chunks by length
before batching, which removes a measured 2.18x padding tax (a batch costs its
longest member times its size, and chunk lengths here span two orders of
magnitude) for a further ~1.8x, landing near 3 h.

If throughput still bites, tune `BATCH_SIZE` before reaching for the 0.6B — its
quality drop is much steeper (~6 MTEB points versus ~1).

## Verify output, don't trust counts

Two silent-loss bugs in this codebase were invisible in aggregate totals and
surfaced only by comparing extracted text against the source:

- `field_list` was skipped as "page metadata" — true in RST, false in Sphinx API
  output, where it holds every function's Parameters/Returns documentation.
  4,222 nodes silently dropped.
- `literal_strong` was treated as block-level, breaking parameter names away
  from their descriptions.

`check_thin_files.py` found the first by flagging one page at 60% capture while
the corpus total looked healthy. The general method — compare per-page word
counts against the source's own text, and treat anything well below 100% as
suspect — is why the checkers exist and why they are run after every ingest.
(Above 100% is normal: breadcrumb prefixes repeat the section path.)

The TRM ingest met the same class of bug three more times, all invisible in word
counts: a dropped `&` collapsing every table row into one cell, a `\newcommand`
body that opened an environment without closing it and made pylatexenc swallow
128,896 characters and 221 registers *without raising*, and `lstlisting` bodies
being parsed as LaTeX. See [trm-latex.md](trm-latex.md).

Two corollaries worth stating separately:

- **Confirm a check before believing its finding.** Register names here
  routinely contain spaces and parentheses (`AES_KEY_n_REG (n: 0-7)`), so a
  pattern like `Register (AES_[^\s]+) at address` silently matches none of the
  ~1,156 parameterised registers and reports them as lost when they render
  perfectly. Validate any missing-content check against an item known to be
  present before concluding anything about the parser.
- **Test the checkers themselves.** A safety net nobody has fallen into is
  untested equipment, and thresholds chosen against no data are guesses.
  `make_trm_fixture.py` exists for this; see [development.md](development.md).

## Project history — what was tried and deleted

Kept short, and kept because each of these has been proposed more than once.

| Approach | Why it is gone |
|---|---|
| Chunking ESP-IDF `.rst` sources directly | 3,040 chunks, 133 with unresolved placeholders, no API reference at all. Replaced by building the docs per target. |
| Downloading the compiled TRM PDFs (`download_trms.py`, `trm_pdfs/`) | Espressif publishes the LaTeX sources, which carry structure a PDF does not. Both the script and the PDF directory were removed. |
| Two pseudo-chips (`esp32p4`, `esp32p4-v1.3`) for the revision axis | Duplicates ~2,873 identical registers in the largest manual, returns every P4 result twice, and puts something that is not a chip into `esp32_docs_list_chips`. Replaced by the `revisions` column. |
| Sphinx `.doctrees` pickles as ingest input | Written before post-transforms, so `only::` is unevaluated; also pins this project to the doc build's exact library versions. |
