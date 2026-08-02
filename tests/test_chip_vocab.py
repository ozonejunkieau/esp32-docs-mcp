"""Tests for the verified chip vocabulary.

chips.yaml is not a convenience list: it validates the MCP server's `chip` and
`revision` parameters, drives the revision filter, and maps TRM folder names to
canonical chip names during ingest. A wrong entry surfaces as a search that
returns nothing, or as TRM content mislabelled with the wrong silicon revision.
"""

from __future__ import annotations

import pytest

from chip_vocab import load_chip_vocabulary


@pytest.fixture(scope="module")
def vocab():
    return load_chip_vocabulary()


def test_chips_yaml_loads_and_validates(vocab):
    assert vocab.chips, "chips.yaml produced an empty vocabulary"


def test_every_chip_declares_at_least_one_revision(vocab):
    """Chips with a single manual get ["mainline"] rather than an empty list, so
    array_contains works uniformly -- leaving one empty reintroduces the
    empty-list special case that `chips` was deliberately designed away from."""
    empty = [name for name, info in vocab.chips.items() if not info.revisions]

    assert empty == []


class TestCoverage:
    """idf_docs and trm_folder are independent facts, and both gaps are real.

    esp32c61/esp32h2 have a published TRM but are absent from ESP-IDF's
    `idf_targets`; esp32s31 is the reverse. Neither may be inferred from the
    other, and asserting the counts here catches an edit that quietly conflates
    them.
    """

    def test_nine_chips_have_an_idf_docs_build(self, vocab):
        assert len(vocab.idf_doc_targets()) == 9

    def test_ten_chips_have_a_published_trm(self, vocab):
        assert sum(1 for info in vocab.chips.values() if info.trm_folder) == 10

    @pytest.mark.parametrize("chip", ["esp32c61", "esp32h2"])
    def test_trm_without_idf_docs(self, vocab, chip):
        assert vocab.chips[chip].trm_folder is not None
        assert vocab.chips[chip].idf_docs is False

    def test_idf_docs_without_a_trm(self, vocab):
        assert vocab.chips["esp32s31"].idf_docs is True
        assert vocab.chips["esp32s31"].trm_folder is None


class TestRevisions:
    """ESP32-P4 is the only chip with more than one published manual."""

    def test_known_revisions_is_the_union_across_chips(self, vocab):
        assert vocab.known_revisions() == {"mainline", "v1.3"}

    def test_latest_is_never_a_published_label(self, vocab):
        """"latest" is the internal name of the LaTeX tag (ESP32-P4-latest), not
        a label Espressif publishes, and it would rot the moment new silicon
        ships. The mainline manual carries no revision qualifier at all."""
        assert "latest" not in vocab.known_revisions()

    def test_esp32p4_publishes_two_revisions(self, vocab):
        assert vocab.revisions_for("esp32p4") == ["mainline", "v1.3"]

    def test_a_single_manual_chip_publishes_only_mainline(self, vocab):
        assert vocab.revisions_for("esp32c3") == ["mainline"]

    def test_an_unknown_chip_has_no_revisions(self, vocab):
        assert vocab.revisions_for("esp32nonesuch") == []


class TestTrmFolderMapping:
    """Folder-name -> chip lookup, used once per directory by the TRM ingest."""

    def test_esp8684_codename_maps_to_esp32c2(self):
        """The TRM repo names the C2's directory with its pre-release codename.
        Same silicon, different convention between the two repos -- miss this
        and one whole manual is ingested under no chip at all."""
        assert load_chip_vocabulary().chip_for_trm_folder("ESP8684") == "esp32c2"

    @pytest.mark.parametrize(
        ("folder", "chip"),
        [("ESP32", "esp32"), ("ESP32-C3", "esp32c3"), ("ESP32-P4", "esp32p4"), ("ESP32-C61", "esp32c61")],
    )
    def test_ordinary_folder_names(self, vocab, folder, chip):
        assert vocab.chip_for_trm_folder(folder) == chip

    def test_an_unknown_folder_returns_none(self, vocab):
        assert vocab.chip_for_trm_folder("00-shared") is None

    def test_the_mapping_round_trips_for_every_chip_with_a_trm(self, vocab):
        """chip_for_trm_folder scans linearly for the first match, so a
        duplicated trm_folder value would silently shadow one chip."""
        for name, info in vocab.chips.items():
            if info.trm_folder:
                assert vocab.chip_for_trm_folder(info.trm_folder) == name
