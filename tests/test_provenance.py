"""Tests for upstream-revision recording.

Every chunk carries the version and commit of the repo it was derived from, so
a register-level claim can be checked against a specific upstream state. The
important property is that reading it never raises: an ingest from a tarball, an
export, or a directory that simply is not a git checkout must still produce
chunks -- honestly marked "unknown" rather than aborting the run or, worse,
inheriting a version from somewhere else.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from provenance import UNKNOWN, source_revision


def test_a_non_git_directory_yields_unknown_rather_than_raising(tmp_path):
    revision = source_revision(tmp_path)

    assert revision.version == UNKNOWN
    assert revision.commit == UNKNOWN


def test_a_path_that_does_not_exist_yields_unknown(tmp_path):
    revision = source_revision(tmp_path / "no-such-checkout")

    assert revision.version == UNKNOWN
    assert revision.commit == UNKNOWN


def test_a_file_rather_than_a_directory_yields_unknown(tmp_path):
    target = tmp_path / "a-tarball.tar.gz"
    target.write_bytes(b"")

    assert source_revision(target).commit == UNKNOWN


def test_a_string_path_is_accepted(tmp_path):
    """embed_and_store passes --source-repo straight through from the CLI, so
    the argument is not always a Path."""
    assert source_revision(str(tmp_path)).commit == UNKNOWN


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_real_checkout_yields_a_commit_and_a_version(tmp_path):
    """A repo with no tags -- as the TRM sources are -- must still describe,
    which is what `describe --always` is for. Built here rather than read from
    an existing checkout so the test never depends on ambient repo state."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
    }
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "seed"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)

    revision = source_revision(repo)

    assert revision.commit != UNKNOWN
    assert len(revision.commit) == 40
    assert revision.version != UNKNOWN
    # No tags, so describe falls back to the short commit.
    assert revision.commit.startswith(revision.version)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_dirty_checkout_is_marked_as_such(tmp_path):
    """A locally-modified checkout is not the upstream revision it claims to be,
    and a corpus built from one is not reproducible from that commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
    }
    (repo / "f.txt").write_text("one\n")
    for args in (["init", "-q"], ["add", "f.txt"], ["commit", "-q", "-m", "seed"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)
    (repo / "f.txt").write_text("two\n")

    assert source_revision(Path(repo)).version.endswith("-dirty")
