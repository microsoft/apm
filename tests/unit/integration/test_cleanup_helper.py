"""Unit tests for ``apm_cli.integration.cleanup.remove_stale_deployed_files``.

The helper is the single safety gate guarding APM's intra-package and
local-package stale-file deletion. These tests pin its invariants:

* path validation rejects unmanaged prefixes
* directory entries are refused (defeats poisoned-lockfile rmtree)
* recorded-hash mismatch skips deletion (treats as user-edited)
* missing recorded hash falls through (back-compat with legacy lockfiles)
* unlink failures are retained for retry on next install
"""

from pathlib import Path

import pytest

from apm_cli.integration.cleanup import (
    CleanupResult,
    remove_stale_deployed_files,
)
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.diagnostics import DiagnosticCollector
from apm_cli.core.command_logger import CommandLogger


@pytest.fixture
def project_root(tmp_path):
    return tmp_path


@pytest.fixture
def diagnostics():
    return DiagnosticCollector(verbose=False)


@pytest.fixture
def logger():
    return CommandLogger("install", verbose=False)


def _make_managed_file(project_root: Path, rel: str, content: str = "hi\n") -> Path:
    p = project_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_happy_path_deletes_under_known_prefix(project_root, diagnostics, logger):
    target = _make_managed_file(project_root, ".github/prompts/old.prompt.md")
    result = remove_stale_deployed_files(
        [".github/prompts/old.prompt.md"], project_root,
        dep_key="pkg", targets=None,
        diagnostics=diagnostics, logger=logger,
    )
    assert result.deleted == [".github/prompts/old.prompt.md"]
    assert not result.failed
    assert not result.skipped_unmanaged
    assert not target.exists()


def test_path_traversal_rejected(project_root, diagnostics, logger):
    """validate_deploy_path rejects '..' segments."""
    result = remove_stale_deployed_files(
        ["../escape.md"], project_root,
        dep_key="pkg", targets=None,
        diagnostics=diagnostics, logger=logger,
    )
    assert result.deleted == []
    assert result.skipped_unmanaged == ["../escape.md"]


def test_unmanaged_prefix_rejected(project_root, diagnostics, logger):
    """A file outside any integration prefix is refused."""
    rel = "src/main.py"
    _make_managed_file(project_root, rel)
    result = remove_stale_deployed_files(
        [rel], project_root,
        dep_key="pkg", targets=None,
        diagnostics=diagnostics, logger=logger,
    )
    assert result.deleted == []
    assert rel in result.skipped_unmanaged
    assert (project_root / rel).exists()


def test_directory_entry_refused(project_root, diagnostics, logger):
    """A lockfile entry that resolves to a directory is refused outright.

    This is the lockfile-poisoning blocker: an attacker writes
    '.github/instructions/' (a directory under a known prefix) into the
    lockfile and expects the next install to rmtree the user's whole
    instructions folder. APM only deploys individual files, so it must
    only delete individual files.
    """
    (project_root / ".github" / "instructions").mkdir(parents=True)
    (project_root / ".github" / "instructions" / "user.md").write_text(
        "user-authored", encoding="utf-8",
    )
    result = remove_stale_deployed_files(
        [".github/instructions"], project_root,
        dep_key="pkg", targets=None,
        diagnostics=diagnostics, logger=logger,
    )
    assert result.deleted == []
    assert ".github/instructions" in result.skipped_unmanaged
    # Subtree intact.
    assert (project_root / ".github" / "instructions" / "user.md").exists()
    # Diagnostic recorded so user knows.
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("Refused to remove directory entry" in m for m in msgs)


def test_missing_file_treated_as_already_clean(project_root, diagnostics, logger):
    result = remove_stale_deployed_files(
        [".github/prompts/gone.prompt.md"], project_root,
        dep_key="pkg", targets=None,
        diagnostics=diagnostics, logger=logger,
    )
    assert result.deleted == []
    assert result.failed == []
    assert result.skipped_unmanaged == []  # missing != unmanaged


def test_hash_mismatch_skips_user_edited_file(project_root, diagnostics, logger):
    rel = ".github/prompts/edited.prompt.md"
    _make_managed_file(project_root, rel, "user has edited this\n")
    # Pretend APM recorded a different hash at deploy time (i.e. user
    # has since edited the file).
    fake_recorded = {rel: "sha256:" + "0" * 64}
    result = remove_stale_deployed_files(
        [rel], project_root,
        dep_key="pkg", targets=None,
        diagnostics=diagnostics, logger=logger,
        recorded_hashes=fake_recorded,
    )
    assert result.deleted == []
    assert result.skipped_user_edit == [rel]
    assert (project_root / rel).exists()
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("edited" in m.lower() for m in msgs)


def test_hash_match_deletes_file(project_root, diagnostics, logger):
    rel = ".github/prompts/match.prompt.md"
    target = _make_managed_file(project_root, rel, "untouched\n")
    recorded = {rel: compute_file_hash(target)}
    result = remove_stale_deployed_files(
        [rel], project_root,
        dep_key="pkg", targets=None,
        diagnostics=diagnostics, logger=logger,
        recorded_hashes=recorded,
    )
    assert result.deleted == [rel]
    assert not target.exists()


def test_no_recorded_hashes_falls_through_to_delete(project_root, diagnostics, logger):
    """Backward compat with legacy lockfiles -- no hash means delete."""
    rel = ".github/prompts/legacy.prompt.md"
    target = _make_managed_file(project_root, rel)
    result = remove_stale_deployed_files(
        [rel], project_root,
        dep_key="pkg", targets=None,
        diagnostics=diagnostics, logger=logger,
        recorded_hashes=None,
    )
    assert result.deleted == [rel]
    assert not target.exists()


def test_unlink_failure_is_retained_for_retry(project_root, diagnostics, logger, monkeypatch):
    rel = ".github/prompts/cant-delete.prompt.md"
    _make_managed_file(project_root, rel)

    def _raise(*_a, **_kw):
        raise PermissionError("simulated")

    monkeypatch.setattr(Path, "unlink", _raise)
    result = remove_stale_deployed_files(
        [rel], project_root,
        dep_key="pkg", targets=None,
        diagnostics=diagnostics, logger=logger,
    )
    assert result.deleted == []
    assert result.failed == [rel]
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("retry on next" in m.lower() for m in msgs)


def test_result_dataclass_defaults():
    r = CleanupResult()
    assert r.deleted == []
    assert r.failed == []
    assert r.skipped_user_edit == []
    assert r.skipped_unmanaged == []
    assert r.deleted_targets == []
