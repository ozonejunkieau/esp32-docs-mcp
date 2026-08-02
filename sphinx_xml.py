"""Extract chunks from Sphinx's docutils-XML output for one built ESP-IDF target.

Why XML and not the .rst sources: ESP-IDF docs are a *build*, not a set of
standalone files. `{IDF_TARGET_SOC_CPU_CORES_NUM}`-style constants come from
soc_caps headers and Kconfig read by esp-docs at build time, and `only::`
branches are per-target. Parsing raw .rst leaves those unresolved -- the
placeholder text survives verbatim into the embedding, and mutually exclusive
per-chip branches get merged into one contradictory chunk.

Why XML and not the pickled .doctrees: Sphinx writes .doctree at the end of
the *read* phase, before post-transforms run, so `only::` nodes are still
present and unevaluated there. The XML builder serializes post-resolution
trees -- branches already pruned for the target being built. It also avoids
unpickling docutils/Sphinx/esp_docs node classes, which would pin this
project to the doc build's exact library versions.

One page of XML in, a list of RawChunks out. The heading-based chunk assembly
itself lives in chunking.py and is shared with the TRM corpus; this module only
turns XML into the SectionNode/Block tree that expects.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from chunking import (
    MAX_WORDS_PER_CHUNK,
    MIN_WORDS_PER_CHUNK,
    Block,
    RawChunk,
    Refs,
    SectionNode,
    build_chunks,
    merge_undersized_chunks,
)

# Inline elements are concatenated with no separator; anything else is a block
# and gets a blank line between siblings. Mirrors docutils' nodes.Inline check,
# which we can't use here since we're walking XML rather than a node tree.
_INLINE_TAGS = frozenset(
    {
        "emphasis",
        "strong",
        "literal",
        "reference",
        "inline",
        "title_reference",
        "abbreviation",
        "acronym",
        "subscript",
        "superscript",
        "footnote_reference",
        "citation_reference",
        "substitution_reference",
        "problematic",
        "math",
        "raw",
        # Sphinx renders a documented parameter's name as literal_strong inside
        # its field body; treating it as a block would break the name away from
        # the description that defines it.
        "literal_strong",
        "literal_emphasis",
        "desc_sig_name",
        "desc_sig_space",
        "desc_sig_punctuation",
        "desc_sig_operator",
        "desc_sig_keyword",
        "desc_sig_keyword_type",
        "desc_sig_literal_number",
        "desc_sig_literal_string",
        "desc_name",
        "desc_addname",
        "desc_type",
        "desc_annotation",
        "desc_parameter",
        "desc_parameterlist",
        "desc_returns",
    }
)

# Dropped entirely: build diagnostics, invisible targets/indexes, and the
# navigation scaffolding (toctree compounds) that carries no retrievable prose.
_SKIP_TAGS = frozenset(
    {
        "system_message",
        "comment",
        "substitution_definition",
        "target",
        "index",
        "docinfo",
        "toctree",
        "meta",
        "colspec",
    }
)

# Sections whose entire subtree is navigation, not content.
_SKIP_SECTION_IDS = frozenset({"indices-and-tables"})

_GITHUB_PATH_RE = re.compile(r"https://github\.com/espressif/esp-idf/(?:blob|tree|raw)/[^/]+/([^#?]+)")
_XREF_CLASS_RE = re.compile(r"\bxref\b")
_WHITESPACE_RE = re.compile(r"\s+")

# Content whose internal whitespace is significant (source listings, API
# signatures) and must survive verbatim.
_PRESERVE_TAGS = frozenset({"literal_block", "doctest_block", "line_block"})
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# Sphinx's C/C++ domain mangles symbol anchors as _CPPv4<len><name>...; these
# are symbol identities, not page names.
_CPP_ANCHOR_RE = re.compile(r"^_CPP")


def _canonical_docname(refuri: str, base_dir: str) -> str:
    """Resolve a page-relative refuri into a docname from the language root.

    Sphinx writes internal links relative to the referring page
    ("../security/flash-encryption"), which makes the same target look
    different depending on where it was linked from. Normalizing against the
    referring page's directory keeps doc_refs comparable across pages -- the
    whole point of storing them.
    """
    if not refuri or refuri.startswith(("http://", "https://", "mailto:")):
        return ""
    return posixpath.normpath(posixpath.join(base_dir, refuri)).lstrip("./") if base_dir else refuri.strip("/")


def _is_block(tag: str) -> bool:
    return tag not in _INLINE_TAGS


def _text_of(element: ET.Element, preserve: bool = False) -> str:
    """Recursively render an element's visible text.

    Handles XML mixed content (an element's .text plus each child's .tail) and
    inserts a blank line between block-level siblings so adjacent paragraphs
    and lists don't run together. Images contribute their alt text.

    Sphinx pretty-prints its XML, so the indentation between block-level
    elements is present as real mixed-content whitespace. It has to be dropped
    structurally rather than by collapsing the finished string: code inside a
    literal_block is significant whitespace that a global normalization would
    destroy. Around block children the whitespace is always layout, so it goes;
    around inline children it may be a real word separator, so it collapses to
    a single space instead.
    """
    if element.tag in _SKIP_TAGS:
        return ""
    if element.tag == "image":
        return (element.get("alt") or "").strip()
    if element.tag == "desc_parameterlist":
        # Without explicit punctuation the parameter list abuts the function
        # name and the parameters run into each other, so a signature reads
        # "esp_rom_software_reset_cpuint cpu_no" rather than
        # "esp_rom_software_reset_cpu(int cpu_no)".
        params = [_text_of(p).strip() for p in element.findall("desc_parameter")]
        return "(" + ", ".join(p for p in params if p) + ")"
    if element.tag == "field":
        # A documented API field ("Parameters", "Returns", "Note"). Pairing the
        # label with its body on one line keeps "Returns: ESP_OK on success"
        # intact; the default block rendering would strand the label alone.
        name = element.find("field_name")
        body = element.find("field_body")
        label = _text_of(name).strip() if name is not None else ""
        content = _text_of(body).strip() if body is not None else ""
        return f"{label}: {content}" if label and content else label or content

    preserve = preserve or element.tag in _PRESERVE_TAGS or element.get(_XML_SPACE) == "preserve"

    def fragment(text: str, next_to_block: bool) -> str:
        """Normalize one run of mixed-content text."""
        if preserve:
            return text
        if not text.strip():
            # Layout whitespace between blocks is noise; between inline
            # siblings it separates words.
            return "" if next_to_block else " "
        return _WHITESPACE_RE.sub(" ", text)

    children = list(element)
    parts: list[str] = []

    if element.text:
        first_is_block = bool(children) and _is_block(children[0].tag)
        parts.append(fragment(element.text, first_is_block))

    for child in children:
        rendered = _text_of(child, preserve)
        child_is_block = _is_block(child.tag)
        if rendered:
            if any(p.strip() for p in parts) and child_is_block:
                parts.append("\n\n")
            parts.append(rendered)
        if child.tail:
            parts.append(fragment(child.tail, child_is_block))

    return "".join(parts).replace("\x00", "")


def _render_table(table: ET.Element) -> str:
    """Render a table as delimited 'Header: value' rows.

    Flattening a table with plain text extraction destroys the row/column
    association that makes register and pin-mapping tables worth retrieving
    at all -- pairing each cell with its header keeps it recoverable.
    """
    tgroup = table.find("tgroup")
    if tgroup is None:
        return _text_of(table).strip()

    thead = tgroup.find("thead")
    headers: list[str] = []
    if thead is not None:
        header_row = thead.find("row")
        if header_row is not None:
            headers = [_text_of(entry).strip() for entry in header_row.findall("entry")]

    tbody = tgroup.find("tbody")
    if tbody is None:
        return _text_of(table).strip()

    lines: list[str] = []
    caption = table.find("title")
    if caption is not None:
        caption_text = _text_of(caption).strip()
        if caption_text:
            lines.append(caption_text)

    for row in tbody.findall("row"):
        cells = [_text_of(entry).strip() for entry in row.findall("entry")]
        if not any(cells):
            continue
        if headers:
            pairs = [f"{headers[i]}: {cell}" if i < len(headers) and headers[i] else cell for i, cell in enumerate(cells)]
            lines.append(" | ".join(p for p in pairs if p.strip(": ")))
        else:
            lines.append(" | ".join(c for c in cells if c))
    return "\n".join(lines)


def _collect_refs(element: ET.Element, base_dir: str = "") -> Refs:
    """Collect resolved cross-reference targets within an element, split by kind.

    Post-resolution Sphinx XML expresses references very differently from the
    raw roles in the .rst: `:component_file:` has become an absolute GitHub
    URL, `:ref:`/`:doc:` an internal reference with a refuri/refid, and
    `:cpp:func:` a literal carrying an "xref cpp cpp-func" class. Their
    original role names are gone, so kind is inferred from the resolved shape.

    Args:
        element: Node to search.
        base_dir: Directory of the containing page, used to resolve the
            page-relative refuris Sphinx emits into canonical docnames.
    """
    file_refs: list[str] = []
    doc_refs: list[str] = []
    symbol_refs: list[str] = []

    for ref in element.iter("reference"):
        refuri = ref.get("refuri", "")
        github = _GITHUB_PATH_RE.match(refuri)
        if github:
            path = github.group(1).rstrip("/")
            if path and path not in file_refs:
                file_refs.append(path)
            continue
        if ref.get("internal") != "True":
            continue
        # Internal refs point at "<page>#<anchor>" or a bare local anchor.
        if refuri:
            target = _canonical_docname(refuri.split("#")[0], base_dir)
        else:
            target = ref.get("refid", "")
        # C/C++ domain anchors (_CPPv4..., _CPPv2...) identify a symbol, not a
        # page; the symbol itself is captured from the literal/desc_name below.
        if target and not _CPP_ANCHOR_RE.match(target) and target not in doc_refs:
            doc_refs.append(target)

    for literal in element.iter("literal"):
        if _XREF_CLASS_RE.search(literal.get("classes", "")):
            symbol = _text_of(literal).strip()
            if symbol and symbol not in symbol_refs:
                symbol_refs.append(symbol)

    # An API declaration block defines a symbol rather than referencing one,
    # but for "which chunks mention this symbol" both are worth indexing.
    for desc_name in element.iter("desc_name"):
        symbol = _text_of(desc_name).strip()
        if symbol and symbol not in symbol_refs:
            symbol_refs.append(symbol)

    return Refs(file_refs=file_refs, doc_refs=doc_refs, symbol_refs=symbol_refs, chips=[])


def _own_body_text(section: ET.Element, base_dir: str) -> list[Block]:
    """Extract a section's direct content, excluding its nested subsections.

    Each block-level child becomes one Block, so the chunk splitter can later
    break an oversized section on block boundaries rather than mid-sentence.
    """
    blocks: list[Block] = []
    for child in section:
        if child.tag in _SKIP_TAGS or child.tag in ("title", "section", "subtitle"):
            continue
        text = _render_table(child) if child.tag == "table" else _text_of(child).strip()
        if text:
            blocks.append(Block(text=text, refs=_collect_refs(child, base_dir)))
    return blocks


def _build_tree(element: ET.Element, base_dir: str, title: str = "") -> SectionNode:
    """Build a SectionNode tree mirroring the page's heading hierarchy."""
    children: list[SectionNode] = []
    for child in element.findall("section"):
        if child.get("ids", "") in _SKIP_SECTION_IDS:
            continue
        title_element = child.find("title")
        child_title = _text_of(title_element).strip() if title_element is not None else ""
        children.append(_build_tree(child, base_dir, child_title))
    return SectionNode(title=title, blocks=_own_body_text(element, base_dir), children=children)


def is_sphinx_page(path: Path) -> bool:
    """Whether an XML file is a Sphinx page rather than one of doxygen's.

    esp-docs writes doxygen's XML into the same tree, so the two are mixed
    together on disk; only Sphinx pages have a <document> root.
    """
    try:
        with path.open("rb") as f:
            head = f.read(400)
    except OSError:
        return False
    return b"<document" in head


def chunk_sphinx_xml(
    path: Path,
    docname: str,
    max_words: int = MAX_WORDS_PER_CHUNK,
    min_words: int = MIN_WORDS_PER_CHUNK,
) -> list[RawChunk]:
    """Chunk one page of Sphinx XML along its heading boundaries.

    Args:
        path: Path to the page's XML file.
        docname: Page identity relative to the language source root, without
            extension (e.g. "api-reference/peripherals/ledc"). Recorded as the
            chunk's file_path -- target-independent, so the same page from two
            targets is recognisably the same document during deduplication.
        max_words: Soft cap on words per chunk before splitting a section further.
        min_words: Chunks smaller than this get merged into a neighbour.

    Returns:
        Ordered list of chunks for this page.
    """
    root = ET.parse(path).getroot()
    base_dir = posixpath.dirname(docname)

    # A page's single top-level section holds the page title; keeping the
    # <document> wrapper as an extra tree level would prefix every chunk with
    # an empty heading.
    top_sections = root.findall("section")
    if len(top_sections) == 1:
        title_element = top_sections[0].find("title")
        title = _text_of(title_element).strip() if title_element is not None else ""
        tree = _build_tree(top_sections[0], base_dir, title)
    else:
        tree = _build_tree(root, base_dir)

    chunks: list[RawChunk] = []
    build_chunks(tree, [tree.title] if tree.title else [], max_words, chunks, docname, [0])
    return merge_undersized_chunks(chunks, min_words)


if __name__ == "__main__":
    import sys

    target_path = Path(sys.argv[1])
    for chunk in chunk_sphinx_xml(target_path, target_path.stem):
        print(
            f"--- chunk {chunk.chunk_index} [{chunk.section_path}] "
            f"file_refs={chunk.file_refs} doc_refs={chunk.doc_refs[:4]} symbol_refs={chunk.symbol_refs[:4]} ---"
        )
        print(chunk.text)
        print()
