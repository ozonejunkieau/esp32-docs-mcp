"""Record which upstream revision each chunk was derived from.

Both corpora are snapshots of moving repositories. ESP-IDF's docs change every
few days; the TRM sources are explicitly "ahead of the published PDF". A chunk
with no provenance is a claim about ESP32 hardware with no way to tell whether
it still holds, and no way to answer "which version says this?" -- which for
register-level content is the difference between a usable answer and a
dangerous one.

So every chunk carries the source repo's git description and commit. Read from
the checkout at ingest time rather than hand-maintained, because a version
string someone has to remember to update is a version string that will be wrong.

Deliberately per-row rather than a side table: it survives into search results,
so a citation can say "per ESP-IDF v6.1-dev-6485" without a second lookup. The
value repeats across every row of an ingest, which costs almost nothing -- Lance
dictionary-encodes a column of one distinct value.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel

UNKNOWN = "unknown"


class SourceRevision(BaseModel):
    """The upstream revision a set of chunks was built from.

    Attributes:
        version: Human-readable identity, e.g. "v6.1-dev-6485-g055ba9d3f9c".
            Falls back to the short commit where the repo publishes no tags,
            as the TRM repo does.
        commit: Full commit SHA -- the unambiguous identifier, and the one to
            use when reproducing an ingest.
    """

    version: str = UNKNOWN
    commit: str = UNKNOWN


def _git(repo: Path, *args: str) -> str | None:
    """Run a git command in repo, or None if it isn't a usable checkout."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None


def source_revision(repo: Path) -> SourceRevision:
    """Describe the checkout at `repo`.

    Never raises: an ingest from a tarball or a non-git copy should still
    produce chunks, just ones honestly marked "unknown" rather than silently
    inheriting a wrong version.
    """
    repo = Path(repo)
    commit = _git(repo, "rev-parse", "HEAD")
    # --always so a repo with no tags still yields something; --dirty because a
    # locally-modified checkout is not the upstream revision it claims to be.
    version = _git(repo, "describe", "--tags", "--always", "--dirty")
    return SourceRevision(version=version or UNKNOWN, commit=commit or UNKNOWN)


if __name__ == "__main__":
    import sys

    for path in sys.argv[1:] or ["."]:
        revision = source_revision(Path(path))
        print(f"{path}\n  version={revision.version}\n  commit={revision.commit}")
