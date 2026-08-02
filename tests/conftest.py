"""Shared fixtures, and the corpus locators the slow tests skip on.

Neither corpus exists in a fresh clone: the ESP-IDF half needs a full toolchain
install plus a ~20-minute-per-chip docs build, and the TRM half needs a separate
LaTeX checkout. A test that fails for want of that data is worse than no test at
all, so everything that touches a corpus resolves its input here and skips with
the path it looked for when the input is absent -- a legible "you don't have the
build" rather than a traceback that reads like a code defect.

Path resolution mirrors the shipping tools exactly rather than inventing its
own: `$IDF_PATH` defaulting to ~/git/esp-idf is build_idf_docs.sh's cascade, and
the TRM lookup is trm_verify.resolve_trm_root's ($TRM_PATH, ./trm_latex,
~/git/esp-technical-reference-manual-latex). The one adjustment is anchoring the
relative ./trm_latex symlink at the repo root, since pytest's working directory
is not guaranteed to be it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The target whose ingest is pinned byte-for-byte; see test_corpus_invariants.
IDF_REFERENCE_CHIP = "esp32p4"


def _idf_path() -> Path:
    """The ESP-IDF checkout, per build_idf_docs.sh's `${IDF_PATH:-$HOME/git/esp-idf}`."""
    return Path(os.environ.get("IDF_PATH") or Path.home() / "git" / "esp-idf")


def find_idf_xml_root(chip: str = IDF_REFERENCE_CHIP) -> Path | None:
    """The Sphinx XML builder output for one target, or None if it hasn't been built."""
    candidate = _idf_path() / "docs" / "_build" / "en" / chip / "xml"
    return candidate if candidate.is_dir() else None


def find_trm_root() -> Path | None:
    """The TRM LaTeX checkout, or None. Same cascade as trm_verify.resolve_trm_root."""
    candidates: list[Path] = []
    if env := os.environ.get("TRM_PATH"):
        candidates.append(Path(env))
    candidates.append(REPO_ROOT / "trm_latex")
    candidates.append(Path.home() / "git" / "esp-technical-reference-manual-latex")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return None


@pytest.fixture(scope="session")
def idf_xml_root() -> Path:
    """The esp32p4 Sphinx XML build, skipping the test if it is not present."""
    root = find_idf_xml_root()
    if root is None:
        pytest.skip(
            f"no ESP-IDF esp32p4 XML build at "
            f"{_idf_path() / 'docs/_build/en' / IDF_REFERENCE_CHIP / 'xml'} -- "
            f"build it with ./build_idf_docs.sh {IDF_REFERENCE_CHIP}, or set $IDF_PATH"
        )
    return root


@pytest.fixture(scope="session")
def trm_root() -> Path:
    """The TRM LaTeX checkout, skipping the test if it is not present."""
    root = find_trm_root()
    if root is None:
        pytest.skip(
            "no TRM LaTeX checkout found (tried $TRM_PATH, ./trm_latex, "
            "~/git/esp-technical-reference-manual-latex) -- clone "
            "https://github.com/espressif/esp-technical-reference-manual-latex "
            "and set $TRM_PATH"
        )
    return root
