"""Schema for a single retrievable chunk of ESP documentation."""

from __future__ import annotations

from lancedb.pydantic import LanceModel, Vector
from pydantic import field_validator

# Must match embedder.MODEL_NAME's output dimension. LanceDB fixes the vector
# width at table creation, so changing this means recreating the table
# (embed_and_store.py --overwrite) and re-embedding everything.
EMBEDDING_DIM = 2560  # Qwen3-Embedding-4B output dimension
DOC_TYPES = ("trm", "idf", "src")


class Chunk(LanceModel):
    """A single retrievable unit of ESP documentation.

    Attributes:
        vector: The embedding vector for `text`.
        text: The chunk's content, prefixed with its heading path for context.
        source_doc: Human-readable name of the originating document.
        doc_type: Which corpus this chunk came from. One of DOC_TYPES:
            "trm" -- a chip Technical Reference Manual (Espressif's LaTeX sources).
            "idf" -- the built ESP-IDF guides and API reference.
            "src" -- an ESP-IDF SoC header (components/soc/**/soc/*.h), the C
            counterpart to the TRM: where the manual describes
            I2C_SCL_LOW_PERIOD_REG, the header defines its address and bitmasks.
            "src" rows are chip-scoped through `chips` like "idf" rows, not
            through `chip`, and carry no `revisions`.
        chip: Chip variant this chunk applies to, e.g. "esp32p4". Empty if chip-agnostic.
            Authoritative for TRM chunks (one file = one chip, known from the source folder),
            and deliberately empty for both IDF and src chunks, which use `chips`.
        chips: Every chip whose documentation build produced this exact chunk, or --
            for src chunks -- whose soc/ directory the header lives under, all of
            them for a header outside any per-chip directory. Populated for IDF and
            src chunks, and authoritative rather than best-effort: the
            docs are built once per target with `only::` branches resolved and
            constants substituted, so a chunk's presence in a target's build is proof
            it applies to that chip. Deduplication then collapses byte-identical
            chunks across targets into one row carrying every chip it appeared under.
            Content common to all chips therefore lists all of them -- there is no
            empty-list "chip-agnostic" case to special-case, so a chip-scoped query is
            a plain `array_contains(chips, 'X')`. A chunk whose text differs between
            targets by even one substituted constant stays a separate, correctly
            narrower row.
        section_path: Heading hierarchy joined with " > ", e.g. "I2S > Clock Configuration".
        file_refs: Source file paths referenced via :component_file:/:project_file:/:example:
            (e.g. "bt/esp_ble_mesh/core/access.c"), for "which chunks mention this file" queries.
        doc_refs: Other doc pages/sections referenced via :ref:/:doc: (e.g. "arch-concurrency"),
            for doc-graph style queries. Raw target identifiers, not resolved to titles.
        symbol_refs: C/C++ symbol names referenced via :cpp:type:/:c:func:/etc.
            (e.g. "soc_root_clk_t"), for "which chunks mention this symbol" queries.
            For src chunks these are the symbols the chunk *declares* -- #define
            names, typedefs, tags, enum members, prototypes -- which is what
            esp32_docs_find_symbol matches on exactly.
        file_path: Path to the source file on disk, for traceability.
        chunk_index: Position of this chunk within its source file.
        revisions: Silicon-revision manual variants this chunk applies to. TRM only;
            empty for IDF and src, which have no revision axis. Espressif publishes more than
            one manual for some chips -- ESP32-P4 has a mainline TRM and a separate
            "Chip Revision v1.3" one -- and the register sets genuinely diverge in
            both directions (48 registers only in v1.3, 60 only in mainline). Ingesting
            one variant silently loses the other's registers, so both are ingested and
            deduplicated: content common to both lists both, content that differs stays
            separate and correctly narrower. Same mechanism as `chips`, one axis over.
            Because it is empty for every non-TRM row, a revision filter must be
            scoped to doc_type = 'trm' rather than applied across all rows.
        source_version: Upstream revision the chunk was built from, e.g.
            "v6.1-dev-6485-g055ba9d3f9c", or a short commit where the repo has no tags.
            A "-dirty" suffix means the checkout had uncommitted changes.
        source_commit: Full commit SHA of that source repo. Both corpora track moving
            upstreams -- ESP-IDF docs change weekly, and the TRM sources are explicitly
            ahead of the published PDFs -- so a register-level claim without a revision
            attached cannot be checked or reproduced. See provenance.py.
    """

    vector: Vector(EMBEDDING_DIM)
    text: str
    source_doc: str
    doc_type: str
    chip: str = ""
    chips: list[str] = []
    revisions: list[str] = []
    section_path: str = ""
    file_refs: list[str] = []
    doc_refs: list[str] = []
    symbol_refs: list[str] = []
    file_path: str
    chunk_index: int
    source_version: str = ""
    source_commit: str = ""

    @field_validator("doc_type")
    @classmethod
    def _check_doc_type(cls, v: str) -> str:
        if v not in DOC_TYPES:
            raise ValueError(f"doc_type must be one of {DOC_TYPES}, got {v!r}")
        return v