"""Heading-aware chunk assembly, independent of the source document format.

Chunking is hierarchical: a section's whole subtree (its own text plus every
descendant) is kept as a single chunk as long as it fits under
MAX_WORDS_PER_CHUNK. Only when a subtree is too big do we split it -- emitting
the section's own content separately and recursing into each child. A follow-up
pass merges any chunk left under MIN_WORDS_PER_CHUNK into a neighbour, so a
short section like "3.1 Features" never ends up as a near-empty standalone
chunk.

This module knows nothing about where the section tree came from. Callers parse
their own format into SectionNode/Block and hand it over: sphinx_xml.py for the
ESP-IDF docs build, latex_parser.py for TRM LaTeX. Keeping the split here means
the two corpora chunk identically, so a retrieved passage has the same shape
and size distribution whichever manual it came from.
"""

from __future__ import annotations

from pydantic import BaseModel

MAX_WORDS_PER_CHUNK = 400
MIN_WORDS_PER_CHUNK = 50


class Refs(BaseModel):
    """Reference targets collected from a piece of content, split by kind."""

    file_refs: list[str] = []
    doc_refs: list[str] = []
    symbol_refs: list[str] = []
    chips: list[str] = []

    def merged_with(self, other: "Refs") -> "Refs":
        return Refs(
            file_refs=list(dict.fromkeys(self.file_refs + other.file_refs)),
            doc_refs=list(dict.fromkeys(self.doc_refs + other.doc_refs)),
            symbol_refs=list(dict.fromkeys(self.symbol_refs + other.symbol_refs)),
            chips=list(dict.fromkeys(self.chips + other.chips)),
        )


class RawChunk(BaseModel):
    """A chunk of text extracted from a source document, not yet embedded.

    Attributes:
        text: The chunk's content, prefixed with its heading path.
        section_path: Heading hierarchy joined with " > ".
        file_refs: Source file paths referenced by this chunk
            (e.g. "components/esp_driver_ledc/src/ledc.c") -- for
            "which chunks mention this file" queries.
        doc_refs: Other doc pages/sections referenced by this chunk
            (e.g. "api-guides/startup") -- for doc-graph style queries.
        symbol_refs: C/C++ symbol names referenced or declared by this chunk
            (e.g. "ledc_timer_config_t") -- for "which chunks mention this
            symbol" queries.
        chips: Chips this chunk applies to. Populated per source: the IDF path
            fills it during deduplication, from which targets' builds produced
            the chunk (see dedup_chunks.py); the TRM path knows the single chip
            from the source manual.
        file_path: Identity of the source document, relative to its corpus root.
        chunk_index: Position of this chunk within its source document.
    """

    text: str
    section_path: str
    file_refs: list[str] = []
    doc_refs: list[str] = []
    symbol_refs: list[str] = []
    chips: list[str] = []
    file_path: str
    chunk_index: int


class Block(BaseModel):
    """One rendered block-level element (paragraph, table, code block, ...) with its refs.

    `atomic` marks a block that is an indivisible record and must never be cut
    by the oversized-block splitter, however long it gets. A TRM `register` is
    the motivating case: name, address, bitfield layout and per-field
    descriptions only mean anything together, and the line-grouping heuristic in
    _split_oversized_block would happily sever one mid-bitfield, leaving fields
    stranded from the register name that identifies them. Doxygen API `desc`
    blocks on the IDF side have the same property.

    Atomicity constrains splitting only. Atomic blocks still group with
    neighbours here and still merge in merge_undersized_chunks -- registers sit
    at a p50 of 36 words, well under MIN_WORDS_PER_CHUNK, and grouping the
    consecutive registers of one peripheral is exactly what we want. Merge yes,
    split no.
    """

    text: str
    refs: Refs = Refs()
    atomic: bool = False


class SectionNode(BaseModel):
    """A section's own content plus its nested subsections, before chunking."""

    title: str
    blocks: list[Block] = []
    children: list["SectionNode"] = []


SectionNode.model_rebuild()


def _word_count(blocks: list[Block]) -> int:
    return sum(len(b.text.split()) for b in blocks)


def _subtree_word_count(node: SectionNode) -> int:
    total = _word_count(node.blocks)
    for child in node.children:
        total += len(child.title.split()) + _subtree_word_count(child)
    return total


def _render_blocks(blocks: list[Block]) -> tuple[str, Refs]:
    text = "\n\n".join(b.text for b in blocks)
    refs = Refs()
    for b in blocks:
        refs = refs.merged_with(b.refs)
    return text, refs


def _render_subtree(node: SectionNode) -> tuple[str, Refs]:
    """Render a section and everything beneath it into one block of text."""
    own_text, refs = _render_blocks(node.blocks)
    parts = [own_text] if own_text else []

    for child in node.children:
        child_text, child_refs = _render_subtree(child)
        if child_text:
            parts.append(f"{child.title}\n\n{child_text}" if child.title else child_text)
        elif child.title:
            parts.append(child.title)
        refs = refs.merged_with(child_refs)

    return "\n\n".join(p for p in parts if p), refs


def _split_oversized_block(block: Block, max_words: int) -> list[tuple[str, Refs]]:
    """Split a single block's text into word-capped pieces, grouping whole lines together.

    Used when one block (a huge table, a huge literal/code block -- e.g. an
    auto-generated efuse table dump) is larger than max_words on its own.
    Grouping by line rather than a flat word-count window keeps each
    line-based entry (e.g. one eFuse field's name + description) intact
    instead of severing it mid-sentence at an arbitrary word boundary.
    Falls back to a raw word window only for a single line that's itself
    too long. Refs can't be attributed to a specific line range, so every
    piece gets the full set -- over-attaching is safer than silently dropping.
    """
    lines = block.text.splitlines()
    pieces: list[tuple[str, Refs]] = []
    current: list[str] = []
    current_words = 0

    for line in lines:
        line_words = len(line.split())

        if line_words > max_words:
            if current:
                pieces.append(("\n".join(current), block.refs))
                current, current_words = [], 0
            words = line.split()
            pieces.extend((" ".join(words[i : i + max_words]), block.refs) for i in range(0, len(words), max_words))
            continue

        if current and current_words + line_words > max_words:
            pieces.append(("\n".join(current), block.refs))
            current, current_words = [], 0
        current.append(line)
        current_words += line_words

    if current:
        pieces.append(("\n".join(current), block.refs))

    return pieces


def _split_paragraphs_with_refs(blocks: list[Block], max_words: int) -> list[tuple[str, Refs]]:
    """Group blocks into word-capped pieces. Fallback for an oversized leaf section.

    An oversized atomic block is emitted whole and alone rather than split: the
    cap is a retrieval-quality heuristic, whereas cutting an indivisible record
    is outright content corruption, so the cap is what gives way.
    """
    pieces: list[tuple[str, Refs]] = []
    current: list[Block] = []
    current_words = 0

    for block in blocks:
        block_words = len(block.text.split())

        if block_words > max_words:
            if current:
                pieces.append(_render_blocks(current))
                current, current_words = [], 0
            if block.atomic:
                pieces.append((block.text, block.refs))
            else:
                pieces.extend(_split_oversized_block(block, max_words))
            continue

        if current and current_words + block_words > max_words:
            pieces.append(_render_blocks(current))
            current, current_words = [], 0
        current.append(block)
        current_words += block_words

    if current:
        pieces.append(_render_blocks(current))

    return pieces


def _emit(out: list[RawChunk], path: list[str], text: str, refs: Refs, file_path: str, idx: list[int]) -> None:
    if not text.strip():
        return
    section_path = " > ".join(path)
    prefixed = f"{section_path}\n\n{text}" if section_path else text
    out.append(
        RawChunk(
            text=prefixed,
            section_path=section_path,
            file_refs=refs.file_refs,
            doc_refs=refs.doc_refs,
            symbol_refs=refs.symbol_refs,
            chips=refs.chips,
            file_path=file_path,
            chunk_index=idx[0],
        )
    )
    idx[0] += 1


def build_chunks(
    node: SectionNode, path: list[str], max_words: int, out: list[RawChunk], file_path: str, idx: list[int]
) -> None:
    """Keep a section's whole subtree as one chunk if it fits; split only if it doesn't."""
    if _subtree_word_count(node) <= max_words:
        text, refs = _render_subtree(node)
        _emit(out, path, text, refs, file_path, idx)
        return

    if not node.children:
        # Oversized leaf section with no subheadings left to split on.
        for piece_text, piece_refs in _split_paragraphs_with_refs(node.blocks, max_words):
            _emit(out, path, piece_text, piece_refs, file_path, idx)
        return

    for piece_text, piece_refs in _split_paragraphs_with_refs(node.blocks, max_words):
        _emit(out, path, piece_text, piece_refs, file_path, idx)
    for child in node.children:
        build_chunks(child, path + [child.title], max_words, out, file_path, idx)


def _content_word_count(chunk: RawChunk) -> int:
    """Word count of a chunk's real content, excluding its repeated breadcrumb prefix.

    Using len(chunk.text.split()) directly would inflate deeply-nested chunks
    (a long "A > B > C" prefix adds several words on its own), letting small
    sections escape the min_words merge simply by being nested deep enough.
    """
    return len(chunk.text.split()) - len(chunk.section_path.split())


def merge_undersized_chunks(chunks: list[RawChunk], min_words: int) -> list[RawChunk]:
    """Fold any chunk under min_words into a neighbour, then reassign chunk_index."""
    if len(chunks) <= 1:
        return chunks

    def _merge_refs_lists(a_file, a_doc, a_sym, a_chips, b_file, b_doc, b_sym, b_chips):
        return (
            list(dict.fromkeys(a_file + b_file)),
            list(dict.fromkeys(a_doc + b_doc)),
            list(dict.fromkeys(a_sym + b_sym)),
            list(dict.fromkeys(a_chips + b_chips)),
        )

    merged: list[RawChunk] = [chunks[0]]
    for chunk in chunks[1:]:
        if _content_word_count(chunk) < min_words:
            prev = merged[-1]
            file_refs, doc_refs, symbol_refs, chips = _merge_refs_lists(
                prev.file_refs, prev.doc_refs, prev.symbol_refs, prev.chips,
                chunk.file_refs, chunk.doc_refs, chunk.symbol_refs, chunk.chips,
            )
            merged[-1] = prev.model_copy(
                update={
                    "text": f"{prev.text}\n\n{chunk.text}",
                    "file_refs": file_refs,
                    "doc_refs": doc_refs,
                    "symbol_refs": symbol_refs,
                    "chips": chips,
                }
            )
        else:
            merged.append(chunk)

    if len(merged) > 1 and _content_word_count(merged[0]) < min_words:
        first, second = merged[0], merged[1]
        file_refs, doc_refs, symbol_refs, chips = _merge_refs_lists(
            first.file_refs, first.doc_refs, first.symbol_refs, first.chips,
            second.file_refs, second.doc_refs, second.symbol_refs, second.chips,
        )
        merged[1] = second.model_copy(
            update={
                "text": f"{first.text}\n\n{second.text}",
                "file_refs": file_refs,
                "doc_refs": doc_refs,
                "symbol_refs": symbol_refs,
                "chips": chips,
            }
        )
        merged = merged[1:]

    return [c.model_copy(update={"chunk_index": i}) for i, c in enumerate(merged)]
