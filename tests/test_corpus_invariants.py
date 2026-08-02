"""The two corpus invariants that have caught real breakage.

Both were previously protected only by someone remembering to check them by
hand, and both are the kind of regression that leaves every aggregate count
looking healthy:

1. The ESP-IDF ingest is *byte-identical*. `chunking.py` is shared between the
   two corpora, so a change made for the TRM path silently reshapes the ESP-IDF
   corpus -- and a reshaped corpus is only detectable by re-embedding it, which
   is hours. Pinning the SHA-1 of one target's JSONL catches it in a minute.
2. The TRM register census is exact. A `\\newcommand` body that opened an
   environment without closing it once made pylatexenc swallow 128,896
   characters and 221 registers *without raising* -- invisible in totals,
   because the prose was all still there.

Both need a locally built corpus that a fresh clone does not have, so both are
marked `slow` and skip with the path they looked for. See conftest.py.

Neither test writes to ./esp_docs.lancedb, embeds anything, or loads a model.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
IDF_REFERENCE_CHIP = "esp32p4"

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# 1. ESP-IDF ingest byte-identity
# ---------------------------------------------------------------------------

# `ingest_sphinx_xml.py <xml> --chip esp32p4 --out-path <f>` against the esp32p4
# Sphinx XML build, SHA-1 of the resulting JSONL file's bytes. Recorded in
# CLAUDE.md and LATEX.md, and reproduced on this machine. It covers the whole
# chain -- sphinx_xml.py's tag handling, chunking.py's tree descent and merge
# pass, and the JSONL field order -- because any difference anywhere moves it.
IDF_CHUNK_COUNT = 4515
IDF_JSONL_SHA1 = "cb3e97a2b9f72a12c572aeb3bd202c802d019baf"


@pytest.fixture(scope="module")
def idf_ingest(idf_xml_root, tmp_path_factory):
    """Run the real ingest CLI once, into tmp_path. Minutes, not seconds."""
    out_path = tmp_path_factory.mktemp("idf") / f"chunks_{IDF_REFERENCE_CHIP}.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "ingest_sphinx_xml.py"),
            str(idf_xml_root),
            "--chip",
            IDF_REFERENCE_CHIP,
            "--out-path",
            str(out_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"ingest failed:\n{result.stdout}\n{result.stderr}"
    return out_path


def test_idf_ingest_produces_the_expected_chunk_count(idf_ingest):
    """Checked separately from the hash so a failure distinguishes "the corpus
    changed shape" from "the same chunks render differently"."""
    with idf_ingest.open() as f:
        assert sum(1 for _ in f) == IDF_CHUNK_COUNT


def test_idf_ingest_is_byte_identical(idf_ingest):
    """The regression that caught a shared-chunking.py change.

    If this fails and the change was deliberate, re-measure and update the
    constant in the same commit -- and note that every embedded row derived from
    this pipeline is now stale, since the store cannot be diffed against it.
    """
    digest = hashlib.sha1(idf_ingest.read_bytes()).hexdigest()

    assert digest == IDF_JSONL_SHA1, (
        f"esp32p4 ingest changed: {digest} != {IDF_JSONL_SHA1}. "
        "Something in sphinx_xml.py or chunking.py altered the corpus."
    )


# ---------------------------------------------------------------------------
# 2. TRM register census
# ---------------------------------------------------------------------------

# Registers recovered by latex_parser through the resolved include graph, per
# chip directory. The total is CLAUDE.md's and LATEX.md's 13,597/13,597 "exact
# on all ten chips"; the per-chip split is measured here, since the documents
# record only the total. Held per chip because the failure modes are
# chip-specific -- the lowercase `_en.tex` filename convention on ESP32/ESP32-S2
# (1,476 registers), and `\iftagged` on ESP32-P4.
#
# A total near 11,539 is the specific alarm documented in CLAUDE.md: it means
# includes are being filtered by `__EN` filename.
TRM_REGISTERS_BY_CHIP = {
    "ESP32": 901,
    "ESP32-C3": 892,
    "ESP32-C5": 1688,
    "ESP32-C6": 1382,
    "ESP32-C61": 863,
    "ESP32-H2": 1185,
    "ESP32-P4": 3977,
    "ESP32-S2": 907,
    "ESP32-S3": 1313,
    "ESP8684": 489,
}
TRM_REGISTER_TOTAL = 13597

# ESP32-P4 resolved with the mainline tag active instead of the v1.3 default.
# The two manuals' register sets diverge in both directions, which is the whole
# justification for the `revisions` axis; if these two numbers ever converge,
# tag selection has stopped doing anything.
TRM_P4_MAINLINE_REGISTERS = 3990


@pytest.fixture(scope="module")
def trm_census(trm_root):
    """Resolve every chapter of every manual and count registers. Minutes."""
    from latex_parser import resolve_document
    from trm_verify import chapter_files, chip_dirs, count_register_envs

    census: dict[str, int] = {}
    missing: list[str] = []
    for chip_dir in chip_dirs(trm_root):
        total = 0
        for chapter in chapter_files(chip_dir):
            doc = resolve_document(chapter)
            total += count_register_envs(doc.text)
            missing.extend(doc.missing)
        census[chip_dir.name] = total
    return census, missing


def test_all_ten_manuals_are_present(trm_census):
    census, _ = trm_census

    assert sorted(census) == sorted(TRM_REGISTERS_BY_CHIP)


@pytest.mark.parametrize("chip_folder", sorted(TRM_REGISTERS_BY_CHIP))
def test_register_count_is_exact_for_each_chip(trm_census, chip_folder):
    """Per chip, not just in aggregate: a repo-wide total can absorb a whole
    chip's loss against another chip's gain, and the two known failure modes are
    each confined to particular manuals."""
    census, _ = trm_census

    assert census.get(chip_folder) == TRM_REGISTERS_BY_CHIP[chip_folder]


def test_register_census_totals_13597(trm_census):
    census, _ = trm_census
    total = sum(census.values())

    assert total == TRM_REGISTER_TOTAL, (
        f"census is {total}, expected {TRM_REGISTER_TOTAL}. "
        "A figure near 11,539 means includes are being filtered by `__EN` filename, "
        "dropping the ~2,000 registers in lowercase `_en.tex` files."
    )


def test_no_include_goes_unresolved(trm_census):
    """Register content lives exclusively in included files, so an unresolved
    include costs the corpus its most valuable content while the prose -- and
    therefore every word count -- still looks fine."""
    _, missing = trm_census

    assert missing == []


def test_esp32p4_tag_selection_changes_the_register_set(trm_census, trm_root):
    """The mainline manual and the v1.3 manual are genuinely different documents.

    Chapters resolve to the v1.3 base variant by default; activating the
    ESP32-P4-latest tag selects the mainline branches instead. The counts differ
    (3,990 vs 3,977) because registers exist in each that do not exist in the
    other -- 60 mainline-only, 48 v1.3-only. If this ever reads equal, tag
    selection has been flattened and P4 results are being labelled with a
    revision they do not have.
    """
    from latex_parser import resolve_document
    from trm_verify import chapter_files, count_register_envs

    census, _ = trm_census
    p4 = trm_root / "ESP32-P4"
    mainline = sum(
        count_register_envs(resolve_document(ch, extra_tags=frozenset({"ESP32-P4-latest"})).text)
        for ch in chapter_files(p4)
    )

    assert mainline == TRM_P4_MAINLINE_REGISTERS
    assert mainline != census["ESP32-P4"]
