# Working on esp32-docs-mcp

The command surface (`just`), the test suite, and the fixtures that keep the
corpus checkers honest. For what the pipelines do and why, see
[architecture.md](architecture.md); for running them, see
[building-the-corpus.md](building-the-corpus.md).

Python 3.12+, `uv` only — never `pip` or `uv pip`. Flat module layout, no `src/`.

## `just` — the command surface

Every long-form command in the documentation has a recipe. The recipes are the
authoritative spelling of each invocation: several flags are load-bearing, and
one of them destroys both corpora.

```bash
just              # list every recipe, grouped
just --list
```

Requires `just` >= 1.43 (the justfile asserts this on load; `[script]`,
`[group]` and `[default]` attributes are used). Two variables are resolved once
at the top and exported to every recipe:

- `IDF_PATH` — `$IDF_PATH`, else `~/git/esp-idf`.
- `TRM_PATH` — `$TRM_PATH`, else the gitignored `./trm_latex` symlink if it
  exists, else `~/git/esp-technical-reference-manual-latex`. This mirrors the
  resolution order the Python tools implement.

`just paths` prints both and says whether they exist. Run it first when
something cannot find its input.

| Group | Recipes |
|---|---|
| `setup` | `install`, `paths` |
| `serve` | `serve`, `register-command` |
| `test` | `test *ARGS` |
| `corpus-idf` | `build-idf-docs`, `chunk-idf`, `dedup-idf`, `embed-idf-fresh`, `embed-idf-append` |
| `corpus-trm` | `chunk-trm`, `embed-trm` |
| `corpus` | `delete-corpus`, `backfill-provenance` |
| `verify` | `validate`, `check-idf-thin`, `check-trm-thin`, `census-source`, `census-check`, `latex-coverage`, `verify-trm` |
| `fixtures` | `fixture-good`, `fixture-broken`, `fixtures-verify` |

`just --list` carries a one-line description of each, including the expected
reading of the checks — so the recipe list doubles as the reference for what a
healthy result looks like.

### The two destructive recipes

**`embed-idf-fresh` is guarded twice, and the reason is that `--overwrite`
recreates the LanceDB table rather than replacing one corpus.** It therefore
destroys the *Technical Reference Manual* rows as well as the ESP-IDF ones, with
no undo, and re-embedding is hours. It is correct only for a from-scratch
rebuild where `just embed-trm` follows immediately.

The guards are a `[confirm(...)]` prompt and a required literal argument:

```bash
just embed-idf-fresh confirm=rebuild-whole-store
```

Without the argument the recipe refuses and points at `embed-idf-append`, which
is the same embed with no `--overwrite`. The recipe also runs `validate_store.py`
first, so the row count you are about to destroy is on screen before you answer
the prompt.

**`delete-corpus <idf|trm>`** is destructive but scoped: it removes one
`doc_type`'s rows so that half can be re-embedded without touching the other.
It also prompts, rejects any `doc_type` other than `idf` or `trm`, and prints
the remaining row count — confirm that number matches the corpus you meant to
keep *before* embedding anything.

## Tests

```bash
uv run pytest          # or: just test
uv run pytest -m 'not slow'
```

77 tests, of which 16 are marked `slow`, all passing. Nothing in the suite loads
an embedding model, writes to `./esp_docs.lancedb`, or embeds anything.

Prefer asserting behaviour over generated SQL. Three tests once pinned
`_build_where`'s exact output and broke the moment the revision clause was
correctly widened from `doc_type = 'idf'` to `doc_type != 'trm'` — they were
asserting a bug. The tests that run a real query against a throwaway LanceDB
table never broke, because what matters is which rows come back.

| File | What it pins |
|---|---|
| `test_chunking.py` | The three behaviours the shared chunker rests on: keep a fitting subtree whole, split only when oversized, never cut an atomic block. |
| `test_chip_vocab.py` | `chips.yaml` loads, every chip declares at least one revision, TRM folder names map back to canonical chip names. |
| `test_provenance.py` | Reading the upstream revision never raises — a non-checkout yields "unknown" rather than aborting an ingest or inheriting a version from elsewhere. |
| `test_mcp_server_filters.py` | `_build_where`, `_format_result`, `_revision_scope`. Pure functions, but they decide what a caller can see, and a too-narrow filter fails quietly as a confidently empty result. Two of these are regressions of bugs that actually occurred. |
| `test_corpus_invariants.py` | `slow`. The ESP-IDF ingest is byte-identical (SHA-1 of one target's JSONL), and the TRM register census is exact. |

### The `slow` marker

`slow` means "needs a locally built corpus that a fresh clone does not have" —
the ESP-IDF Sphinx XML build at `$IDF_PATH/docs/_build/en/<chip>/xml`, or the
TRM LaTeX checkout. It does not mean "flaky" or "optional".

**They skip cleanly rather than failing when the inputs are absent.**
`tests/conftest.py` resolves each corpus with the same cascade the shipping
tools use and calls `pytest.skip` with the path it looked for, so a fresh clone
gets a legible "you don't have the build" instead of a traceback that reads like
a code defect. `-m 'not slow'` deselects them unconditionally.

The byte-identity test is the backstop for the shared chunker: `chunking.py` is
used by both ingests, so a change made for the TRM path silently reshapes the
ESP-IDF corpus, and a reshaped corpus is otherwise only detectable by
re-embedding it. Pinning the SHA-1 catches it in a minute. If it fails after a
deliberate chunker change, the pinned digest is what needs updating — but
confirm the reshape was intended first.

## Fixtures — proving the checkers still discriminate

`make_trm_fixture.py` synthesises two corpora from the real LaTeX: `--mode good`
expands the include graph, `--mode broken` uses only each chapter's own body,
simulating unresolved includes — the exact failure that would strip out the
register content living in included files.

```bash
just fixtures-verify     # regenerate both, run both checkers against each
```

The good fixture must pass both checkers cleanly and the broken one must fail
both loudly; `just --list` records the expected readings. Regenerate and re-run
after changing either checker.

The fixture must cover **all** chips, which is the default: `register_census.py`
compares against the whole corpus, so a single-chip fixture makes its total
meaningless and reads as a catastrophic shortfall when nothing is wrong.

## Conventions

- Conventional Commits.
- Never `git add -A` or `git add .` — stage specific paths.
- Generated artifacts (`esp_docs.lancedb/`, `*.jsonl`, `trm_latex`, fixtures) are
  gitignored, so `git status` is source-only.
