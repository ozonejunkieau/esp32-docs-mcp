"""Loader for the verified chip vocabulary (chips.yaml).

Shared by the MCP server (validating the `chip` search parameter and reporting
per-corpus coverage) and the TRM ingest path (folder-name -> chip lookup).
Single source of truth so both stay in sync -- see chips.yaml's header for
where this vocabulary came from and how to re-verify it.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

DEFAULT_CHIPS_PATH = Path(__file__).parent / "chips.yaml"


class ChipInfo(BaseModel):
    """What's known about one chip in the verified vocabulary.

    Attributes:
        idf_docs: Whether ESP-IDF builds per-target documentation for this chip
            (i.e. it's in conf_common.py's `idf_targets`). False means the IDF
            corpus cannot contain chip-specific content for it at all.
        trm_folder: The TRM repo's folder name for this chip, or None if no
            TRM has been published yet (a real, valid state -- not missing data).
        revisions: Silicon-revision manual variants published for this chip,
            usually just ["mainline"]. Held per chip rather than globally because
            "revision-specific" only means anything relative to what a chip
            actually has: a TRM chunk listing ["mainline"] covers all of esp32c3's
            documentation but only half of esp32p4's.

    idf_docs and trm_folder are independent: a chip may have a TRM but no docs
    build, or a docs build but no TRM. Neither may be inferred from the other.
    """

    idf_docs: bool = False
    trm_folder: str | None = None
    revisions: list[str] = ["mainline"]


class ChipVocabulary(BaseModel):
    """The full set of known chips, keyed by canonical name (e.g. "esp32c3")."""

    chips: dict[str, ChipInfo]

    def known_tokens(self) -> set[str]:
        """Every recognized chip name, whatever its documentation coverage."""
        return set(self.chips)

    def idf_doc_targets(self) -> list[str]:
        """Chips ESP-IDF builds docs for -- the targets the IDF ingest can cover."""
        return sorted(name for name, info in self.chips.items() if info.idf_docs)

    def known_revisions(self) -> set[str]:
        """Every revision label in use, for validating a search's `revision` filter."""
        return {revision for info in self.chips.values() for revision in info.revisions}

    def revisions_for(self, chip: str) -> list[str]:
        """The revision variants published for one chip; empty if the chip is unknown."""
        info = self.chips.get(chip)
        return list(info.revisions) if info else []

    def trm_folder_for(self, chip: str) -> str | None:
        """The TRM folder name for a canonical chip name, or None if not found/no TRM yet."""
        info = self.chips.get(chip)
        return info.trm_folder if info else None

    def chip_for_trm_folder(self, folder_name: str) -> str | None:
        """Reverse lookup: TRM folder name -> canonical chip name."""
        for name, info in self.chips.items():
            if info.trm_folder == folder_name:
                return name
        return None


def load_chip_vocabulary(path: Path = DEFAULT_CHIPS_PATH) -> ChipVocabulary:
    """Load and validate chips.yaml."""
    data = yaml.safe_load(path.read_text())
    return ChipVocabulary.model_validate(data)


if __name__ == "__main__":
    vocab = load_chip_vocabulary()
    print(f"{len(vocab.chips)} known chips ({len(vocab.idf_doc_targets())} with an IDF docs build)")
    for name, info in sorted(vocab.chips.items()):
        print(f"  {name:12s} idf_docs={str(info.idf_docs):5s} trm_folder={info.trm_folder}")