"""Tests for the MCP server's query-shaping helpers.

These are pure functions, but they are the ones that decide what a caller is
allowed to see, and getting them wrong fails quietly: a filter that is too
narrow returns a confidently empty result, which reads as "no such register"
rather than "your filter excluded it". Two of the behaviours pinned here are
regressions of bugs that actually occurred.

Importing mcp_server pulls in embedder (and MLX) but does not instantiate
Embedder -- the model is loaded in FastMCP's lifespan, which these tests never
enter. Nothing here loads a model or touches ./esp_docs.lancedb.
"""

from __future__ import annotations

import numpy as np
import pytest

from mcp_server import _build_where, _format_result, _list_field, _revision_scope


class TestBuildWhere:
    """Filter composition across the doc_type/chip/revision axes.

    The two corpora record chip applicability in different columns -- TRM rows
    carry a single authoritative `chip`, IDF rows the list of every target whose
    build produced them -- which is why the combined case scopes each doc_type
    separately instead of OR-ing conditions across all rows.
    """

    def test_no_filters_means_no_where_clause(self):
        assert _build_where(None, None, None) is None

    def test_doc_type_alone(self):
        assert _build_where("trm", None, None) == "doc_type = 'trm'"

    def test_chip_alone_covers_both_columns(self):
        where = _build_where(None, "esp32p4", None)

        assert "doc_type = 'trm' AND chip = 'esp32p4'" in where
        # idf and src rows are both scoped through the `chips` list, so they
        # share one clause; only the TRM corpus uses the singular column.
        assert "array_contains(chips, 'esp32p4')" in where
        assert "'idf'" in where and "'src'" in where
        assert " OR " in where

    def test_chip_with_trm_doc_type_uses_the_singular_column_only(self):
        where = _build_where("trm", "esp32p4", None)

        assert where == "(doc_type = 'trm' AND chip = 'esp32p4')"
        assert "array_contains(chips" not in where

    def test_chip_with_idf_doc_type_uses_the_list_column_only(self):
        where = _build_where("idf", "esp32p4", None)

        assert where == "(doc_type = 'idf' AND array_contains(chips, 'esp32p4'))"
        assert "chip = " not in where

    def test_all_three_filters_compose_with_and(self):
        where = _build_where("trm", "esp32p4", "v1.3")

        assert where.startswith("(doc_type = 'trm' AND chip = 'esp32p4') AND ")
        assert "array_contains(revisions, 'v1.3')" in where

    def test_revision_filter_exempts_rows_with_no_revision_axis(self):
        """Only TRM rows carry revisions; idf and src rows have an empty list, so
        a bare array_contains would filter every one of them out. Asking about
        v1.3 silicon should still surface the ESP-IDF guides and the SoC headers,
        which apply whatever the stepping.

        The exemption is keyed on the row's own empty `revisions` rather than on
        its doc_type, because two earlier spellings named a *corpus* instead of
        testing the *column* and each was wrong. `doc_type = 'idf'` silently
        dropped every src row once a third corpus existed. `doc_type != 'trm'`
        then exempted src rows wholesale -- but ESP32-P4's SoC register headers
        split into hw_ver1/hw_ver3, which map to v1.3/mainline, so 723 of them do
        carry revisions. That bug returned AES_PSEUDO_REG, a hw_ver3-only
        register, to a caller asking about v1.3 silicon.

        The cost is that an empty `revisions` reads as "no revision axis", so a
        TRM row that lost its revisions through an ingest bug would match every
        revision filter instead of none. That makes "TRM rows always carry
        revisions" an ingest invariant rather than a nicety.
        """
        where = _build_where(None, None, "v1.3")

        assert where == "(array_length(revisions) = 0 OR array_contains(revisions, 'v1.3'))"
        assert "doc_type" not in where


class TestRevisionScope:
    """The same `revisions` list means opposite things on different chips.

    ["mainline"] is the whole of esp32c3's documentation and only half of
    esp32p4's, because esp32p4 publishes a second "Chip Revision v1.3" manual
    whose register set genuinely diverges. Resolving that here is what lets a
    caller read one result and know whether it generalises -- applying a
    mainline-only register definition to v1.3 silicon is a hardware bug, not a
    retrieval nuisance.
    """

    @staticmethod
    def trm_row(chip: str, revisions: list[str]) -> dict:
        return {"doc_type": "trm", "chip": chip, "revisions": revisions}

    def test_mainline_is_the_whole_story_for_a_single_manual_chip(self):
        scope = _revision_scope(self.trm_row("esp32c3", ["mainline"]))

        assert scope == "all published revisions (mainline)"

    def test_the_same_list_is_revision_specific_for_esp32p4(self):
        scope = _revision_scope(self.trm_row("esp32p4", ["mainline"]))

        assert scope.startswith("ONLY revision mainline")
        assert "does not apply to other silicon revisions" in scope

    def test_esp32p4_content_common_to_both_manuals_reads_as_universal(self):
        scope = _revision_scope(self.trm_row("esp32p4", ["mainline", "v1.3"]))

        assert scope == "all published revisions (mainline, v1.3)"

    def test_idf_rows_have_no_revision_axis(self):
        assert _revision_scope({"doc_type": "idf", "chips": ["esp32p4"], "revisions": []}) is None

    def test_a_trm_row_with_no_revisions_says_so_rather_than_claiming_coverage(self):
        """An unlabelled TRM row predates the revision axis or was ingested
        wrong; claiming "all published revisions" for it would be a confident
        false statement about which silicon it applies to."""
        assert _revision_scope(self.trm_row("esp32p4", [])) == "unknown revision coverage"

    def test_a_chip_missing_from_the_vocabulary_is_not_claimed_universal(self):
        scope = _revision_scope(self.trm_row("esp32nonesuch", ["mainline"]))

        assert scope.startswith("ONLY revision mainline")


class TestListField:
    """Regression: `row.get(name) or []` raises on a multi-element numpy array.

    pandas hands list columns back as numpy arrays, and truth-testing one with
    more than one element raises "truth value of an array with more than one
    element is ambiguous". The one- and zero-element cases evaluate fine, so the
    bug is invisible until a chunk applies to two revisions or two chips -- i.e.
    exactly on the ESP32-P4 rows the revision axis exists for.
    """

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(np.array(["mainline", "v1.3"]), id="two-element-array"),
            pytest.param(np.array(["esp32", "esp32c3", "esp32p4"]), id="three-element-array"),
        ],
    )
    def test_multi_element_numpy_array_does_not_raise(self, value):
        assert _list_field({"revisions": value}, "revisions") == list(value)

    def test_single_element_array(self):
        assert _list_field({"revisions": np.array(["mainline"])}, "revisions") == ["mainline"]

    def test_empty_array_becomes_an_empty_list(self):
        assert _list_field({"revisions": np.array([], dtype=object)}, "revisions") == []

    def test_missing_column_becomes_an_empty_list(self):
        assert _list_field({}, "revisions") == []

    def test_none_becomes_an_empty_list(self):
        assert _list_field({"revisions": None}, "revisions") == []


VECTOR_DIM = 4


@pytest.fixture(scope="module")
def table(tmp_path_factory):
    """A four-row throwaway LanceDB table standing in for the real store.

    Built in tmp_path -- never ./esp_docs.lancedb, which holds 21,672 rows
    representing hours of embedding -- with a 4-dimensional vector and literal
    coordinates, so no embedding model is involved.
    """
    import lancedb
    from lancedb.pydantic import LanceModel, Vector

    class TinyChunk(LanceModel):
        """schema.Chunk's columns with a 4-dim vector instead of 2560."""

        vector: Vector(VECTOR_DIM)
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

    db = lancedb.connect(tmp_path_factory.mktemp("throwaway.lancedb"))
    tbl = db.create_table("chunks", schema=TinyChunk)
    tbl.add(
        [
            TinyChunk(
                vector=[1, 0, 0, 0],
                text="P4 register present in both manuals",
                source_doc="ESP32-P4 TRM",
                doc_type="trm",
                chip="esp32p4",
                revisions=["mainline", "v1.3"],
                file_path="ESP32-P4/01-I2C__EN.tex",
                chunk_index=0,
            ),
            TinyChunk(
                vector=[0, 1, 0, 0],
                text="P4 register only in the v1.3 manual",
                source_doc="ESP32-P4 TRM",
                doc_type="trm",
                chip="esp32p4",
                revisions=["v1.3"],
                file_path="ESP32-P4/02-AHBDMA__EN.tex",
                chunk_index=0,
            ),
            TinyChunk(
                vector=[0, 0, 1, 0],
                text="C3 register",
                source_doc="ESP32-C3 TRM",
                doc_type="trm",
                chip="esp32c3",
                revisions=["mainline"],
                file_path="ESP32-C3/01-I2C__EN.tex",
                chunk_index=0,
            ),
            TinyChunk(
                vector=[0, 0, 0, 1],
                text="ESP-IDF guide covering several targets",
                source_doc="api-guides/startup",
                doc_type="idf",
                chips=["esp32c3", "esp32p4"],
                revisions=[],
                file_path="api-guides/startup",
                chunk_index=0,
            ),
        ]
    )
    return tbl


class TestAgainstARealTable:
    """Exercise the filters through LanceDB itself, on a throwaway table.

    String assertions prove the clause was composed as intended; they cannot
    prove it is valid SQL, nor that it selects the rows meant.
    """

    @staticmethod
    def search(table, where):
        query = table.search([1.0] * VECTOR_DIM)
        if where:
            query = query.where(where)
        return query.limit(50).to_pandas()

    def test_chip_filter_returns_both_corpora_for_that_chip(self, table):
        rows = self.search(table, _build_where(None, "esp32p4", None))

        assert set(rows["text"]) == {
            "P4 register present in both manuals",
            "P4 register only in the v1.3 manual",
            "ESP-IDF guide covering several targets",
        }

    def test_revision_filter_narrows_trm_without_dropping_idf(self, table):
        """The regression that matters most: an over-eager revision filter would
        return TRM-only results and make the ESP-IDF guides look absent."""
        rows = self.search(table, _build_where(None, None, "v1.3"))

        texts = set(rows["text"])
        assert "ESP-IDF guide covering several targets" in texts
        assert "P4 register only in the v1.3 manual" in texts
        assert "C3 register" not in texts, "esp32c3 publishes no v1.3 manual"

    def test_format_result_survives_a_real_pandas_row(self, table):
        """End-to-end cover for the numpy truth-testing bug: `revisions` here is
        a genuine two-element numpy array off a pandas frame, which is the shape
        that used to raise inside _format_result."""
        rows = self.search(table, _build_where("trm", "esp32p4", None))
        row = next(r for r in rows.to_dict("records") if r["chunk_index"] == 0 and "both manuals" in r["text"])

        result = _format_result(row)

        assert result["revisions"] == ["mainline", "v1.3"]
        assert result["revision_scope"] == "all published revisions (mainline, v1.3)"
        assert result["chips"] == []
        assert isinstance(result["relevance_distance"], float)
