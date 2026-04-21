"""Unit tests for ``detect_stale_files`` in ``apm_cli.drift``.

The helper returns the set of deployed-file paths that were produced by a
previous install but are no longer produced by the current install, for a
single package.  It is purely set-difference semantics -- the caller owns
filesystem side effects.
"""

from src.apm_cli.drift import detect_stale_files


def test_empty_old_and_new_returns_empty_set():
    """First install: no previous deployment, nothing is stale."""
    assert detect_stale_files([], []) == set()


def test_identical_lists_returns_empty_set():
    """Unchanged deployment: nothing is stale."""
    assert detect_stale_files(["a.md", "b.md"], ["a.md", "b.md"]) == set()


def test_renamed_file_flagged_as_stale():
    """Rename: the old path is stale; the new one is not."""
    assert detect_stale_files(["old.md"], ["new.md"]) == {"old.md"}


def test_removed_file_flagged_as_stale():
    """File dropped from the package: it is stale."""
    assert detect_stale_files(["a.md", "b.md"], ["a.md"]) == {"b.md"}


def test_added_file_never_flagged():
    """File added in the new set is never stale."""
    assert detect_stale_files(["a.md"], ["a.md", "b.md"]) == set()


def test_order_and_duplicates_are_irrelevant():
    """Set semantics: input order and duplicates do not affect the result."""
    assert detect_stale_files(
        ["b.md", "a.md", "a.md"],
        ["a.md", "a.md"],
    ) == {"b.md"}
