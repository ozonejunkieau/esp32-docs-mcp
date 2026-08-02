"""MCP server exposing semantic search over the ESP-IDF/TRM documentation store.

Persistent process: the embedding model and LanceDB connection are loaded
once at startup (via FastMCP's lifespan) and reused across every tool call,
not reloaded per-request -- model load is the expensive part, a query
embedding is milliseconds once it's resident.

Run with stdio transport (the default) for local use with Claude Code/Desktop:
    uv run mcp_server.py
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import lancedb
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, field_validator

from chip_vocab import load_chip_vocabulary
from embedder import Embedder

DB_PATH = Path("./esp_docs.lancedb")
TABLE_NAME = "chunks"

_CHIP_VOCAB = load_chip_vocabulary()
_KNOWN_CHIPS = _CHIP_VOCAB.known_tokens()
_KNOWN_REVISIONS = _CHIP_VOCAB.known_revisions()


@dataclass
class AppContext:
    """Resources loaded once at server startup, reused across every tool call."""

    embedder: Embedder
    table: lancedb.table.Table


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncGenerator[AppContext]:
    """Load the embedding model and open the LanceDB table once, for the server's lifetime.

    `server` is unused but required by FastMCP's lifespan protocol.
    """
    embedder = Embedder()
    db = lancedb.connect(DB_PATH)
    table = db.open_table(TABLE_NAME)
    yield AppContext(embedder=embedder, table=table)


mcp = FastMCP("esp32-docs-mcp", lifespan=app_lifespan)


def _build_where(doc_type: str | None, chip: str | None, revision: str | None = None) -> str | None:
    """Build a LanceDB filter expression for the given doc_type/chip/revision.

    The two corpora record chip applicability differently: a TRM chunk has one
    authoritative `chip` (one manual per chip), while an IDF chunk carries the
    list of every target whose build produced it. Both are exact, so each is a
    simple equality/membership test -- but they're different columns, which is
    why the combined case scopes each doc_type separately rather than OR-ing
    the two conditions across all rows.

    `revision` narrows the TRM side only. IDF rows have no revision axis and
    carry an empty list, so the clause lets them through unconditionally rather
    than filtering them all out -- asking for v1.3 silicon should still surface
    the ESP-IDF guides, which apply regardless of stepping.
    """
    clauses: list[str] = []

    if chip:
        trm_clause = f"(doc_type = 'trm' AND chip = '{chip}')"
        idf_clause = f"(doc_type = 'idf' AND array_contains(chips, '{chip}'))"
        if doc_type == "trm":
            clauses.append(trm_clause)
        elif doc_type == "idf":
            clauses.append(idf_clause)
        else:
            clauses.append(f"({trm_clause} OR {idf_clause})")
    elif doc_type:
        clauses.append(f"doc_type = '{doc_type}'")

    if revision:
        clauses.append(f"(doc_type = 'idf' OR array_contains(revisions, '{revision}'))")

    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else " AND ".join(clauses)


def _revision_scope(row: dict) -> str | None:
    """State plainly which silicon a TRM chunk applies to, or None for IDF rows.

    A bare `revisions` list can't be interpreted without knowing what the chip
    publishes: ["mainline"] is the whole story for esp32c3 and only half of it
    for esp32p4. Resolving that here means a caller reading one result can tell
    whether it is safe to generalise, without a second lookup and without having
    to reason about list membership -- which matters most for registers, where
    applying a mainline definition to v1.3 silicon is a real hardware bug.
    """
    if row["doc_type"] != "trm":
        return None
    applies = _list_field(row, "revisions")
    if not applies:
        return "unknown revision coverage"
    published = _CHIP_VOCAB.revisions_for(row.get("chip") or "")
    if published and set(applies) >= set(published):
        return f"all published revisions ({', '.join(sorted(applies))})"
    return f"ONLY revision {', '.join(sorted(applies))} -- does not apply to other silicon revisions"


def _list_field(row: dict, name: str) -> list:
    """Read a list column from a pandas row.

    Not `row.get(name) or []`: pandas hands these back as numpy arrays, and
    truth-testing an array of more than one element raises. That failure mode is
    invisible until a chunk applies to two revisions.
    """
    value = row.get(name)
    return [] if value is None else list(value)


def _format_result(row: dict) -> dict:
    """Shape one LanceDB result row for tool output -- drop the vector, keep everything citable."""
    return {
        "text": row["text"],
        "source_doc": row["source_doc"],
        "doc_type": row["doc_type"],
        "chip": row.get("chip") or None,
        "chips": _list_field(row, "chips"),
        "revisions": _list_field(row, "revisions"),
        "revision_scope": _revision_scope(row),
        "section_path": row.get("section_path") or None,
        "file_refs": list(row["file_refs"]),
        "doc_refs": list(row["doc_refs"]),
        "symbol_refs": list(row["symbol_refs"]),
        "file_path": row["file_path"],
        "chunk_index": row["chunk_index"],
        "source_version": row.get("source_version") or None,
        "relevance_distance": round(float(row["_distance"]), 4),
    }


class SearchInput(BaseModel):
    """Input for semantic search over the ESP documentation corpus."""

    query: str = Field(..., description="Natural-language search query, e.g. 'how does I2S clock configuration work'", min_length=1)
    doc_type: Literal["trm", "idf"] | None = Field(
        default=None, description="Restrict to 'trm' (chip Technical Reference Manuals) or 'idf' (ESP-IDF guides/API docs). Omit to search both."
    )
    chip: str | None = Field(
        default=None,
        description="Restrict to one chip, e.g. 'esp32p4' or 'esp32c3'. Call esp32_docs_list_chips for valid values. "
        "Documentation common to every chip still matches, since it's recorded as applying to all of them -- "
        "this narrows to what is true for the given chip, it doesn't exclude general content.",
    )
    revision: str | None = Field(
        default=None,
        description="Restrict Technical Reference Manual content to one silicon revision, e.g. 'v1.3' or "
        "'mainline'. Only ESP32-P4 currently has more than one revision; call esp32_docs_list_chips to see "
        "which chips do. Omit unless the target silicon revision is known -- omitting returns every revision, "
        "and each result states which it applies to. ESP-IDF content is unaffected by this filter, since it "
        "has no revision axis.",
    )
    k: int = Field(default=5, description="Number of results to return.", ge=1, le=20)

    @field_validator("chip")
    @classmethod
    def _validate_chip(cls, v: str | None) -> str | None:
        if v is not None and v not in _KNOWN_CHIPS:
            raise ValueError(f"'{v}' is not a known chip. Call esp32_docs_list_chips for valid values.")
        return v

    @field_validator("revision")
    @classmethod
    def _validate_revision(cls, v: str | None) -> str | None:
        # An unrecognised revision would filter every TRM row out and return a
        # confidently empty result, which reads as "no such register" rather
        # than "you asked for a revision that doesn't exist".
        if v is not None and v not in _KNOWN_REVISIONS:
            raise ValueError(f"'{v}' is not a known revision. Valid values: {sorted(_KNOWN_REVISIONS)}.")
        return v


@mcp.tool(
    name="esp32_docs_search",
    annotations=ToolAnnotations(
        title="Search ESP-IDF and TRM documentation",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def esp32_docs_search(params: SearchInput, ctx: Context) -> str:
    """Semantic search over ESP-IDF guides/API docs and chip Technical Reference Manuals.

    Embeds the query and returns the most relevant documentation chunks, each
    with its source file, heading path, and any referenced files/doc pages/
    C symbols for follow-up lookups.

    Args:
        params (SearchInput): query, optional doc_type/chip/revision filters, and
            result count.

    Returns:
        str: JSON array of result objects, each with:
            text, source_doc, doc_type, chip, chips, revisions, revision_scope,
            section_path, file_refs, doc_refs, symbol_refs, file_path,
            chunk_index, source_version, relevance_distance (lower = more relevant).

            `revision_scope` is the one to read before trusting a register: it is
            null for ESP-IDF content, "all published revisions (...)" for TRM
            content true of every stepping, and "ONLY revision X ..." for content
            that applies to some silicon and not the rest. Treating a
            revision-specific register definition as universal is a hardware bug,
            so state the revision when citing one.

            `source_version` is the upstream revision the chunk was built from,
            e.g. "v6.1-dev-6485-g055ba9d3f9c" -- worth quoting when precision
            matters, since both corpora track moving upstreams.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context

    query_vector = app_ctx.embedder.embed_query(params.query)
    search = app_ctx.table.search(query_vector)

    where = _build_where(params.doc_type, params.chip, params.revision)
    if where:
        search = search.where(where)

    results = search.limit(params.k).to_pandas()
    return json.dumps([_format_result(row) for row in results.to_dict("records")], indent=2)


@mcp.tool(
    name="esp32_docs_list_chips",
    annotations=ToolAnnotations(
        title="List known ESP32 chip identifiers",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def esp32_docs_list_chips() -> str:
    """List every chip identifier valid for esp32_docs_search's `chip` parameter.

    Returns:
        str: JSON array of {chip, has_trm, has_idf_docs, revisions} objects. Both
            coverage flags can be false independently, and a false value is a real
            state rather than missing data: has_trm is false for chips with no
            published Technical Reference Manual yet, and has_idf_docs is false
            for chips ESP-IDF builds no per-target documentation for. Filtering
            to a chip whose has_idf_docs is false will only return TRM content,
            and a chip with neither will return nothing chip-specific at all.

            `revisions` lists the silicon-revision manual variants published for
            the chip, and supplies the valid values for esp32_docs_search's
            `revision` parameter. Nearly every chip has just ["mainline"]; a chip
            with more than one has genuinely differing documentation between
            steppings, so the revision is worth establishing before relying on
            register-level detail for it.
    """
    return json.dumps(
        [
            {
                "chip": name,
                "has_trm": info.trm_folder is not None,
                "has_idf_docs": info.idf_docs,
                "revisions": list(info.revisions),
            }
            for name, info in sorted(_CHIP_VOCAB.chips.items())
        ],
        indent=2,
    )


if __name__ == "__main__":
    mcp.run()