# esp32-docs-mcp task runner.
#
# Every recipe here comes from docs/building-the-corpus.md, docs/usage.md or
# README.md. Read those before changing an invocation -- several of the flags
# are load-bearing, and one of them (`--overwrite`) destroys both corpora.

# `[default]`, `[script]` and `[group]` attributes are used below.
_ := assert(semver_matches(just_version(), ">=1.43.0") == "true", "this justfile requires just >= 1.43.0 (found " + just_version() + ")")

# ESP-IDF checkout, installed with the docs feature (./install.sh <target> --enable-docs).
export IDF_PATH := env("IDF_PATH", env("HOME") / "git/esp-idf")

# TRM LaTeX checkout. Mirrors the documented resolution order: $TRM_PATH, then
# the gitignored ./trm_latex symlink, then ~/git/esp-technical-reference-manual-latex.
export TRM_PATH := env("TRM_PATH", if path_exists(justfile_directory() / "trm_latex") == "true" { justfile_directory() / "trm_latex" } else { env("HOME") / "git/esp-technical-reference-manual-latex" })

# The nine ESP-IDF build targets the corpus currently covers.
idf_targets := "esp32 esp32s2 esp32s3 esp32s31 esp32c2 esp32c3 esp32c5 esp32c6 esp32p4"

[default]
[doc("List available recipes")]
help:
    @just --list --unsorted

# ---------- Setup and serving ----------

[doc("Install project dependencies")]
[group('setup')]
install:
    uv sync

[doc("Show the resolved IDF_PATH / TRM_PATH and whether they exist")]
[group('setup')]
[script]
paths:
    echo "IDF_PATH = {{ IDF_PATH }}"
    test -d "{{ IDF_PATH }}" && echo "  exists" || echo "  {{ RED }}missing{{ NORMAL }}"
    echo "TRM_PATH = {{ TRM_PATH }}"
    test -d "{{ TRM_PATH }}" && echo "  exists" || echo "  {{ RED }}missing{{ NORMAL }}"

[doc("Run the MCP server on stdio (Ctrl-C to stop)")]
[group('serve')]
serve:
    uv run mcp_server.py

[doc("Print the `claude mcp add` registration command for this checkout")]
[group('serve')]
@register-command:
    echo "claude mcp add esp32-docs -- uv run --directory {{ justfile_directory() }} mcp_server.py"

# ---------- Tests ----------

[doc("Run the unit tests")]
[group('test')]
test *ARGS:
    uv run pytest {{ ARGS }}

# ---------- Corpus: ESP-IDF half ----------

[doc("Build ESP-IDF docs to Sphinx XML. ~20 min per target, 3 in parallel. Default: all nine targets")]
[group('corpus-idf')]
build-idf-docs *targets:
    ./build_idf_docs.sh {{ if targets == "" { idf_targets } else { targets } }}

[doc("Chunk each built target into chunks_<chip>.jsonl. Default: all nine targets")]
[group('corpus-idf')]
[script]
chunk-idf *targets:
    for chip in {{ if targets == "" { idf_targets } else { targets } }}; do
        echo "{{ BOLD }}== $chip{{ NORMAL }}"
        uv run ingest_sphinx_xml.py "{{ IDF_PATH }}/docs/_build/en/$chip/xml" \
            --chip "$chip" --out-path "chunks_$chip.jsonl"
    done

[doc("Collapse chunks identical across targets into idf_chunks.jsonl")]
[group('corpus-idf')]
dedup-idf:
    uv run dedup_chunks.py chunks_*.jsonl --out-path idf_chunks.jsonl

# DESTRUCTIVE. --overwrite recreates the LanceDB table, so it discards the TRM
# rows as well as the ESP-IDF ones. Correct only for a from-scratch rebuild,
# where the TRM half will be re-embedded straight after with `just embed-trm`.
[confirm("--overwrite RECREATES the table: BOTH the ESP-IDF and the TRM corpora are destroyed, with no undo, and re-embedding is hours. Continue?")]
[doc("DESTRUCTIVE: embed ESP-IDF chunks into a freshly recreated table (drops TRM rows too). Requires confirm=rebuild-whole-store")]
[group('corpus-idf')]
[script]
embed-idf-fresh confirm="":
    if [ "{{ confirm }}" != "rebuild-whole-store" ]; then
        echo "{{ BOLD + RED }}refusing{{ NORMAL }}: this recipe passes --overwrite and destroys the whole" >&2
        echo "table, TRM rows included. Re-run as:" >&2
        echo "    just embed-idf-fresh confirm=rebuild-whole-store" >&2
        echo "To add ESP-IDF rows to an existing table instead, use 'just embed-idf-append'." >&2
        exit 1
    fi
    uv run validate_store.py || true
    uv run embed_and_store.py idf_chunks.jsonl --doc-type idf --overwrite \
        --source-repo "{{ IDF_PATH }}"

[doc("Append ESP-IDF chunks to the existing table (no --overwrite). Delete the old idf rows first")]
[group('corpus-idf')]
embed-idf-append:
    uv run embed_and_store.py idf_chunks.jsonl --doc-type idf --source-repo "{{ IDF_PATH }}"

# ---------- Corpus: TRM half ----------

[doc("Chunk every TRM manual into trm_chunks.jsonl (deduplicated across silicon revisions)")]
[group('corpus-trm')]
chunk-trm:
    uv run ingest_trm.py --out-path trm_chunks.jsonl

# Never --overwrite here: TRM rows append alongside the ESP-IDF ones in the same
# table. No --chip either: one JSONL spans all ten manuals and ingest_trm.py has
# already written the per-row chip.
[doc("Append TRM chunks to the table. Never passes --overwrite")]
[group('corpus-trm')]
embed-trm:
    uv run embed_and_store.py trm_chunks.jsonl --doc-type trm --source-repo "{{ TRM_PATH }}"

# ---------- Corpus: SoC headers ----------

# Register headers (components/soc/*/register/**) come in as one card per file --
# path, leading comment, declared symbols -- not full text. Chunking them as prose
# was measured at 29,588 chunks for content nobody retrieves semantically; cards
# keep the symbol_refs that esp32_docs_find_symbol needs for ~3.4K. --no-registers
# drops them, at the cost of TRM register resolution falling from 78.3% to 1.8%.
[doc("Chunk the ESP-IDF SoC headers into src_chunks.jsonl (register headers as cards)")]
[group('corpus-src')]
chunk-src *ARGS:
    uv run ingest_source.py ingest --out-path src_chunks.jsonl --idf-path "{{ IDF_PATH }}" {{ ARGS }}

# Never --overwrite here: src rows append alongside the ESP-IDF and TRM ones.
# No --chip either: one JSONL spans every target and ingest_source.py has already
# written the per-row chips list.
[doc("Append SoC header chunks to the table. Never passes --overwrite")]
[group('corpus-src')]
embed-src:
    uv run embed_and_store.py src_chunks.jsonl --doc-type src --source-repo "{{ IDF_PATH }}"

# What this measures: how many registers the Technical Reference Manuals document
# actually resolve to a #define in the matching chip's headers. It is the check
# that catches a broken symbol extraction, which no aggregate chunk count would.
# Expect 78.3% overall with cards on, and 1.8% with --no-registers.
[doc("Cross-corpus: share of TRM register names that resolve to a #define in the SoC headers")]
[group('verify')]
check-src-registers jsonl="src_chunks.jsonl":
    uv run ingest_source.py verify-registers {{ jsonl }}

# ---------- Refreshing one corpus ----------

# DESTRUCTIVE but scoped: deletes one doc_type's rows so that corpus can be
# re-embedded without touching the other half. Confirm the remaining row count
# before embedding anything.
[confirm("This deletes every row of one doc_type from the store. They can only come back by re-embedding (hours). Continue?")]
[doc("DESTRUCTIVE: delete all rows of one doc_type ('idf', 'trm' or 'src') from the store")]
[group('corpus')]
[script]
delete-corpus doc_type:
    # Whitelisted rather than passed through: this interpolates into a SQL
    # predicate, and a typo that matched nothing would look like success.
    case "{{ doc_type }}" in
        idf|trm|src) ;;
        *) echo "doc_type must be 'idf', 'trm' or 'src', got '{{ doc_type }}'" >&2; exit 1 ;;
    esac
    uv run python -c "
    import lancedb
    t = lancedb.connect('./esp_docs.lancedb').open_table('chunks')
    t.delete(\"doc_type = '{{ doc_type }}'\")
    print(t.count_rows(), 'rows remain')
    "

[doc("Stamp provenance onto rows embedded without --source-repo (no re-embedding). Pass --dry-run first")]
[group('corpus')]
backfill-provenance source_repo *ARGS:
    uv run backfill_provenance.py {{ source_repo }} {{ ARGS }}

# ---------- Verification ----------

[doc("Store row count, doc_type breakdown, samples, search round-trip")]
[group('verify')]
validate:
    uv run validate_store.py

[doc("ESP-IDF: flag pages that produced too few chunks or captured too few words")]
[group('verify')]
check-idf-thin chip="esp32p4":
    uv run check_thin_files.py xml chunks_{{ chip }}.jsonl \
        --xml-root "{{ IDF_PATH }}/docs/_build/en/{{ chip }}/xml"

[doc("TRM: flag chapters that produced too few chunks or captured too few words")]
[group('verify')]
check-trm-thin jsonl="trm_chunks.jsonl":
    uv run check_thin_files.py latex {{ jsonl }}

# The ESP-IDF chunker's output is byte-reproducible, and CLAUDE.md pins it: any
# change to chunking.py or sphinx_xml.py that moves this hash has changed the
# ESP-IDF corpus, whether or not that was the intent. Run it after touching
# anything shared between the corpora.
[doc("ESP-IDF chunking invariant: esp32p4 must give 4515 chunks, SHA-1 cb3e97a2b9f72a12c572aeb3bd202c802d019baf")]
[group('verify')]
[script]
check-idf-invariant:
    expected=cb3e97a2b9f72a12c572aeb3bd202c802d019baf
    uv run ingest_sphinx_xml.py "{{ IDF_PATH }}/docs/_build/en/esp32p4/xml" \
        --chip esp32p4 --out-path chunks_esp32p4.jsonl
    actual=$(shasum chunks_esp32p4.jsonl | cut -d' ' -f1)
    if [ "$actual" = "$expected" ]; then
        echo "{{ GREEN }}OK{{ NORMAL }}  SHA-1 $actual"
    else
        echo "{{ BOLD + RED }}CHANGED{{ NORMAL }}  expected $expected" >&2
        echo "          actual   $actual" >&2
        exit 1
    fi

[doc("Source-side register census. Expect ~13,707; near 11,539 means includes are being filtered by __EN")]
[group('verify')]
census-source:
    uv run register_census.py source

[doc("Census vs chunk output. Expect 10,092/10,092 and 'OK: 0.0% shortfall' -- a true 100%")]
[group('verify')]
census-check jsonl="trm_chunks.jsonl":
    uv run register_census.py check {{ jsonl }}

[doc("Report LaTeX macros/environments in the TRM sources that the parser does not yet handle")]
[group('verify')]
latex-coverage *ARGS:
    uv run latex_coverage_check.py "{{ TRM_PATH }}" {{ ARGS }}

[doc("Run every TRM check: thin files, source census, census vs chunks")]
[group('verify')]
verify-trm: check-trm-thin census-source census-check

# ---------- Fixtures: prove the checkers still discriminate ----------

[doc("Synthesise a faithful TRM fixture (all chips) at trm_fixture_good.jsonl")]
[group('fixtures')]
fixture-good:
    uv run make_trm_fixture.py trm_fixture_good.jsonl --mode good

[doc("Synthesise a deliberately broken TRM fixture at trm_fixture_broken.jsonl")]
[group('fixtures')]
fixture-broken:
    uv run make_trm_fixture.py trm_fixture_broken.jsonl --mode broken

# Expected: good -> 100.0% census, exit 0, median 92% capture, nothing flagged.
#           broken -> 2.2% census, exit 1, median 52% capture, 177 files flagged.
[doc("Regenerate both fixtures and run both checkers against each. Run after changing either checker")]
[group('fixtures')]
[script]
fixtures-verify: fixture-good fixture-broken
    echo "{{ BOLD }}== good fixture: expect census 100%, exit 0, nothing flagged{{ NORMAL }}"
    uv run register_census.py check trm_fixture_good.jsonl
    uv run check_thin_files.py latex trm_fixture_good.jsonl
    echo "{{ BOLD }}== broken fixture: expect census ~2%, exit 1, many files flagged{{ NORMAL }}"
    uv run register_census.py check trm_fixture_broken.jsonl \
        || echo "{{ GREEN }}(non-zero exit is the expected result for the broken fixture){{ NORMAL }}"
    uv run check_thin_files.py latex trm_fixture_broken.jsonl \
        || echo "{{ GREEN }}(non-zero exit is the expected result for the broken fixture){{ NORMAL }}"
