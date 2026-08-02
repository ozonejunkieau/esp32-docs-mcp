"""Tests for the chunk assembly shared by both corpora.

`chunking.py` is the one module both ingests run through, which is exactly what
makes it dangerous: a change made for the TRM path silently reshapes the ESP-IDF
corpus, and vice versa. The byte-identity test in test_corpus_invariants.py is
the backstop for that, but it needs a built corpus; these run anywhere and pin
down the three behaviours the design rests on -- keep a fitting subtree whole,
split only when oversized, and never cut an atomic block.
"""

from __future__ import annotations

import pytest

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


def words(n: int, token: str = "word") -> str:
    return " ".join([token] * n)


def chunk(node: SectionNode, *, max_words: int = MAX_WORDS_PER_CHUNK) -> list[RawChunk]:
    """Run build_chunks over one root section, as both ingests do."""
    out: list[RawChunk] = []
    build_chunks(node, [node.title], max_words, out, "doc.tex", [0])
    return out


class TestSubtreeSizing:
    """Keep a subtree whole while it fits; split only when it doesn't.

    This is the whole reason chunking descends a tree rather than sliding a
    window: 91.5% of TRM heading units and the large majority of IDF sections
    already fit under the cap, so splitting is the exception, not the strategy.
    """

    def test_fitting_subtree_becomes_one_chunk(self):
        node = SectionNode(
            title="I2C",
            blocks=[Block(text=words(20))],
            children=[
                SectionNode(title="Overview", blocks=[Block(text=words(20))]),
                SectionNode(title="Clock", blocks=[Block(text=words(20))]),
            ],
        )
        chunks = chunk(node)

        assert len(chunks) == 1
        # The child headings must survive into the text, not just the tree: they
        # are the context that makes the child's prose interpretable.
        assert "Overview" in chunks[0].text
        assert "Clock" in chunks[0].text
        assert chunks[0].section_path == "I2C"

    def test_oversized_subtree_splits_at_its_children(self):
        node = SectionNode(
            title="I2C",
            blocks=[Block(text=words(100))],
            children=[
                SectionNode(title="Overview", blocks=[Block(text=words(300))]),
                SectionNode(title="Clock", blocks=[Block(text=words(300))]),
            ],
        )
        chunks = chunk(node)

        # Own content first, then one chunk per child -- each carrying the
        # heading path that says where in the manual it came from.
        assert [c.section_path for c in chunks] == ["I2C", "I2C > Overview", "I2C > Clock"]
        assert [c.chunk_index for c in chunks] == [0, 1, 2]

    def test_refs_from_the_whole_subtree_reach_the_chunk(self):
        """A chunk must carry the refs of everything merged into it, or a
        follow-up "which chunks mention this symbol" lookup misses the row that
        actually contains it."""
        node = SectionNode(
            title="LEDC",
            blocks=[Block(text=words(10), refs=Refs(symbol_refs=["ledc_timer_config_t"]))],
            children=[SectionNode(title="API", blocks=[Block(text=words(10), refs=Refs(file_refs=["ledc.c"]))])],
        )
        chunks = chunk(node)

        assert len(chunks) == 1
        assert chunks[0].symbol_refs == ["ledc_timer_config_t"]
        assert chunks[0].file_refs == ["ledc.c"]


class TestAtomicBlocks:
    """`Block.atomic` must survive any change to the splitter.

    Load-bearing, not decorative: 28 real TRM registers exceed the 400-word cap
    on their own (largest 518) and reach the splitter directly. A register is
    one indivisible record -- name, address, bitfield layout, per-field
    descriptions -- and the line-grouping heuristic in _split_oversized_block
    would happily sever one mid-bitfield, stranding fields from the name that
    gives them meaning. Each test here is paired with an atomic=False control on
    identical input, so a regression that quietly stops honouring the flag
    cannot pass by making both cases behave the same way.
    """

    @staticmethod
    def register_block(*, atomic: bool) -> Block:
        """A register-shaped block over the word cap, as ~28 real ones are."""
        header = "Register UART_CONF0_REG (n: 0-2) at address 0x0000"
        fields = [f"FIELD_{i} [bit {i}] " + words(12, "description") for i in range(40)]
        return Block(text="\n".join([header, *fields]), atomic=atomic)

    def test_oversized_atomic_block_is_emitted_whole(self):
        block = self.register_block(atomic=True)
        assert len(block.text.split()) > MAX_WORDS_PER_CHUNK, "control must actually be oversized"

        chunks = chunk(SectionNode(title="UART Registers", blocks=[block]))

        assert len(chunks) == 1
        # The point is not the count but the pairing: the register's name and
        # its last bitfield have to stay in the same chunk.
        assert "Register UART_CONF0_REG" in chunks[0].text
        assert "FIELD_39" in chunks[0].text

    def test_negative_control_same_block_without_atomic_is_split(self):
        chunks = chunk(SectionNode(title="UART Registers", blocks=[self.register_block(atomic=False)]))

        assert len(chunks) > 1
        header_chunk = next(c for c in chunks if "Register UART_CONF0_REG" in c.text)
        assert "FIELD_39" not in header_chunk.text, (
            "without atomic the splitter separates the register name from its last field -- "
            "this is the corruption the flag exists to prevent"
        )

    def test_atomic_still_groups_with_neighbours_when_it_fits(self):
        """Atomicity constrains splitting only. Registers sit at a p50 of 36
        words, and grouping the consecutive registers of one peripheral into a
        chunk is exactly what we want -- merge yes, split no."""
        node = SectionNode(
            title="AES Registers",
            blocks=[
                Block(text="Register AES_KEY_0_REG at address 0x0000 " + words(20), atomic=True),
                Block(text="Register AES_KEY_1_REG at address 0x0004 " + words(20), atomic=True),
            ],
        )
        chunks = chunk(node)

        assert len(chunks) == 1
        assert "AES_KEY_0_REG" in chunks[0].text
        assert "AES_KEY_1_REG" in chunks[0].text


class TestMergeUndersized:
    """A short section must not survive as a near-empty standalone chunk.

    "3.1 Features" with one sentence under it embeds to noise and crowds out a
    real answer, so anything under MIN_WORDS_PER_CHUNK folds into a neighbour.
    """

    @staticmethod
    def raw(text: str, index: int, section_path: str = "S") -> RawChunk:
        return RawChunk(text=text, section_path=section_path, file_path="doc.tex", chunk_index=index)

    def test_undersized_chunk_folds_into_the_previous_one(self):
        chunks = [self.raw(words(200), 0), self.raw("tiny tail", 1)]

        merged = merge_undersized_chunks(chunks, MIN_WORDS_PER_CHUNK)

        assert len(merged) == 1
        assert "tiny tail" in merged[0].text

    def test_undersized_first_chunk_folds_forward(self):
        """Nothing precedes chunk 0, so it merges into its successor rather than
        being left as the one undersized chunk the pass exists to remove."""
        chunks = [self.raw("tiny head", 0), self.raw(words(200), 1)]

        merged = merge_undersized_chunks(chunks, MIN_WORDS_PER_CHUNK)

        assert len(merged) == 1
        assert merged[0].text.startswith("tiny head")

    def test_chunk_index_is_reassigned_after_merging(self):
        chunks = [self.raw(words(200), 0), self.raw("tiny", 1), self.raw(words(200), 2)]

        merged = merge_undersized_chunks(chunks, MIN_WORDS_PER_CHUNK)

        assert [c.chunk_index for c in merged] == [0, 1]

    def test_large_chunks_are_left_alone(self):
        chunks = [self.raw(words(200), 0), self.raw(words(200), 1)]

        assert len(merge_undersized_chunks(chunks, MIN_WORDS_PER_CHUNK)) == 2

    def test_breadcrumb_prefix_does_not_count_toward_the_minimum(self):
        """_content_word_count excludes the repeated section path. Counting it
        would let a deeply-nested near-empty section escape the merge purely by
        virtue of having a long heading trail."""
        deep = "Peripherals > Serial > UART > Registers > Configuration"
        text = f"{deep}\n\n" + words(MIN_WORDS_PER_CHUNK - 5)
        chunks = [self.raw(words(200), 0), self.raw(text, 1, section_path=deep)]

        assert len(merge_undersized_chunks(chunks, MIN_WORDS_PER_CHUNK)) == 1

    def test_merging_unions_the_refs_of_both_chunks(self):
        big = self.raw(words(200), 0).model_copy(update={"file_refs": ["a.c"], "chips": ["esp32"]})
        small = self.raw("tiny", 1).model_copy(update={"file_refs": ["b.c"], "chips": ["esp32", "esp32c3"]})

        merged = merge_undersized_chunks([big, small], MIN_WORDS_PER_CHUNK)

        assert merged[0].file_refs == ["a.c", "b.c"]
        assert merged[0].chips == ["esp32", "esp32c3"]


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [("MAX_WORDS_PER_CHUNK", MAX_WORDS_PER_CHUNK, 400), ("MIN_WORDS_PER_CHUNK", MIN_WORDS_PER_CHUNK, 50)],
)
def test_word_caps_are_the_documented_values(name, value, expected):
    """Every measured figure in CLAUDE.md/LATEX.md is relative to these caps --
    "28 registers exceed the 400-word cap", "91.5% of heading units fit". Moving
    one silently invalidates the corpus figures and the byte-identity SHA-1, so
    it should be a deliberate edit here rather than a side effect elsewhere."""
    assert value == expected, f"{name} changed; the documented corpus figures no longer apply"
