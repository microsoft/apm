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

from apm_cli.core.command_logger import CommandLogger
from apm_cli.integration.cleanup import (
    CleanupResult,
    remove_stale_deployed_files,
)
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.diagnostics import DiagnosticCollector


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
        [".github/prompts/old.prompt.md"],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
    )
    assert result.deleted == [".github/prompts/old.prompt.md"]
    assert not result.failed
    assert not result.skipped_unmanaged
    assert not target.exists()


def test_path_traversal_rejected(project_root, diagnostics, logger):
    """validate_deploy_path rejects '..' segments."""
    result = remove_stale_deployed_files(
        ["../escape.md"],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
    )
    assert result.deleted == []
    assert result.skipped_unmanaged == ["../escape.md"]


def test_unmanaged_prefix_rejected(project_root, diagnostics, logger):
    """A file outside any integration prefix is refused."""
    rel = "src/main.py"
    _make_managed_file(project_root, rel)
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
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
        "user-authored",
        encoding="utf-8",
    )
    result = remove_stale_deployed_files(
        [".github/instructions"],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
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
        [".github/prompts/gone.prompt.md"],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
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
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
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
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        recorded_hashes=recorded,
    )
    assert result.deleted == [rel]
    assert not target.exists()


def test_no_recorded_hashes_falls_through_to_delete(project_root, diagnostics, logger):
    """Backward compat with legacy lockfiles -- no hash means delete."""
    rel = ".github/prompts/legacy.prompt.md"
    target = _make_managed_file(project_root, rel)
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        recorded_hashes=None,
    )
    assert result.deleted == [rel]
    assert not target.exists()


def test_hash_read_failure_fails_closed(project_root, diagnostics, logger, monkeypatch):
    """Provenance gate must fail CLOSED on hash-read errors.

    Regression test for PR #762 review feedback: previously a
    ``compute_file_hash`` exception (e.g. ``PermissionError``) was
    swallowed and ``actual_hash`` became ``None``, allowing the file
    to be deleted even though a hash was recorded. With a hash
    recorded but unreadable the helper cannot prove the file is
    unmodified, so it must skip deletion.
    """
    rel = ".github/prompts/unreadable.prompt.md"
    target = _make_managed_file(project_root, rel, "could be edited, can't tell\n")
    recorded = {rel: "sha256:" + "1" * 64}

    def _boom(_path):
        raise PermissionError("simulated EACCES")

    # The helper imports compute_file_hash lazily inside the gate, so
    # patch the source module.
    monkeypatch.setattr("apm_cli.utils.content_hash.compute_file_hash", _boom)
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
        recorded_hashes=recorded,
    )
    assert result.deleted == []
    assert result.skipped_user_edit == [rel]
    assert target.exists()
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("could not verify" in m.lower() for m in msgs), (
        "Expected fail-closed warning mentioning the verification failure."
    )


def test_unlink_failure_is_retained_for_retry(project_root, diagnostics, logger, monkeypatch):
    rel = ".github/prompts/cant-delete.prompt.md"
    _make_managed_file(project_root, rel)

    def _raise(*_a, **_kw):
        raise PermissionError("simulated")

    monkeypatch.setattr(Path, "unlink", _raise)
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="pkg",
        targets=None,
        diagnostics=diagnostics,
    )
    assert result.deleted == []
    assert result.failed == [rel]
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("retry on next" in m.lower() for m in msgs)


def test_orphan_failure_message_does_not_promise_retry(
    project_root, diagnostics, logger, monkeypatch
):
    """failed_path_retained=False rewords the failure diagnostic.

    Orphan cleanup runs against a package that is no longer in the
    manifest, so the lockfile entry is being dropped entirely and a
    failed deletion can't be retried by APM. The user must remove the
    file manually -- the diagnostic must say so instead of promising
    a retry that will never happen.
    """
    rel = ".github/prompts/orphan-cant-delete.prompt.md"
    _make_managed_file(project_root, rel)
    monkeypatch.setattr(
        Path, "unlink", lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError("nope"))
    )
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="some/orphan-pkg",
        targets=None,
        diagnostics=diagnostics,
        failed_path_retained=False,
    )
    assert result.failed == [rel]
    msgs = [d.message for d in diagnostics._diagnostics]
    assert not any("will retry" in m.lower() for m in msgs)
    assert any("delete the file manually" in m.lower() for m in msgs)


def test_orphan_path_honours_hash_gate(project_root, diagnostics, logger):
    """Orphan cleanup must skip user-edited files just like stale cleanup.

    Regression guard for the security review of the #666 follow-up:
    earlier the orphan path bypassed the helper entirely and would have
    silently deleted a file the user edited after APM deployed it.
    """
    rel = ".github/prompts/edited-orphan.prompt.md"
    target = _make_managed_file(project_root, rel, "user has edited this\n")
    fake_recorded = {rel: "sha256:" + "0" * 64}
    result = remove_stale_deployed_files(
        [rel],
        project_root,
        dep_key="orphan-pkg",
        targets=None,
        diagnostics=diagnostics,
        recorded_hashes=fake_recorded,
        failed_path_retained=False,
    )
    assert result.deleted == []
    assert result.skipped_user_edit == [rel]
    assert target.exists()


def test_helper_signature_does_not_accept_logger():
    """Logger kwarg was dropped -- helper output goes through diagnostics
    plus caller-side InstallLogger methods (cleanup_skipped_user_edit /
    stale_cleanup / orphan_cleanup). Pin the SoC."""
    import inspect

    sig = inspect.signature(remove_stale_deployed_files)
    assert "logger" not in sig.parameters


def test_orphan_loop_uses_manifest_intent_not_integration_outcome():
    """Regression guard: the orphan-cleanup loop in install.py must derive
    'still-declared' from intended_dep_keys (manifest intent), NOT from
    package_deployed_files (integration outcome).

    Bug reproduced if the membership test reads package_deployed_files: a
    transient integration failure for a still-declared package leaves its
    key absent from package_deployed_files; the orphan loop then deletes
    that package's previously deployed files even though the package is
    still in apm.yml. Detected by the security re-review on commit 4b64c27.
    """
    import inspect

    from apm_cli.install.phases import cleanup as cleanup_mod

    src = inspect.getsource(cleanup_mod)
    orphan_marker = "# Orphan cleanup: remove deployed files for packages that were"
    assert orphan_marker in src, "Orphan cleanup block not found -- update marker."
    block_start = src.index(orphan_marker)
    block_end = src.index("# Stale-file cleanup:", block_start)
    orphan_block = src[block_start:block_end]
    # Strip comments so the banned-phrase check doesn't trip on the
    # cautionary comment that explains the bug we're guarding against.
    code_only_lines = [ln for ln in orphan_block.splitlines() if not ln.lstrip().startswith("#")]
    code_only = "\n".join(code_only_lines)
    # Must consult manifest intent.
    assert "intended_dep_keys" in code_only, (
        "Orphan loop must use intended_dep_keys (manifest intent). "
        "Using package_deployed_files.keys() (integration outcome) re-introduces "
        "silent deletion of files for still-declared packages on transient errors."
    )
    # Must NOT regress to the outcome set.
    assert "package_deployed_files.keys()" not in code_only, (
        "Orphan loop must not derive membership from package_deployed_files.keys() -- "
        "see test_orphan_loop_uses_manifest_intent_not_integration_outcome docstring."
    )


def test_hash_deployed_is_module_level_and_works(tmp_path):
    """Regression test for PR #762 review feedback.

    Previously ``_hash_deployed`` was an inner closure of
    ``_install_apm_dependencies`` but was *referenced* from
    ``_integrate_local_content`` (a sibling module-level function),
    which would raise ``NameError`` at runtime whenever the local
    ``.apm/`` persist path executed. Promoted to module scope so both
    call sites share one implementation. This test pins:

    1. The helper is module-importable (no NameError at import time).
    2. It accepts the new ``(rel_paths, project_root)`` signature.
    3. It returns ``{rel: "sha256:<hex>"}`` for regular files and
       silently omits symlinks / missing paths.
    """
    from apm_cli.commands.install import _hash_deployed

    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / "missing.txt")  # never created
    out = _hash_deployed(["a.txt", "missing.txt"], tmp_path)
    assert "a.txt" in out
    assert out["a.txt"].startswith("sha256:")
    assert "missing.txt" not in out
    # Empty input is safe.
    assert _hash_deployed([], tmp_path) == {}
    assert _hash_deployed(None, tmp_path) == {}


def test_result_dataclass_defaults():
    r = CleanupResult()
    assert r.deleted == []
    assert r.failed == []
    assert r.skipped_user_edit == []
    assert r.skipped_unmanaged == []
    assert r.deleted_targets == []


# ---------------------------------------------------------------------------
# Cowork cleanup tests (PR #926 -- remove_stale_deployed_files)
# ---------------------------------------------------------------------------


def test_cowork_stale_entry_deletes_real_file(tmp_path, diagnostics):
    """Happy path: cowork:// stale entry with a real file in a temp cowork
    root -> file deleted, lockfile entry removed (in result.deleted)."""
    from unittest.mock import patch

    cowork_root = tmp_path / "cowork-skills"
    cowork_root.mkdir()
    skill_md = cowork_root / "my-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# My Skill\n", encoding="ascii")
    assert skill_md.exists()

    project_root = tmp_path / "project"
    project_root.mkdir()

    with patch(
        "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
        return_value=cowork_root,
    ):
        result = remove_stale_deployed_files(
            ["cowork://skills/my-skill/SKILL.md"],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
        )

    assert not skill_md.exists(), "File should have been deleted"
    assert "cowork://skills/my-skill/SKILL.md" in result.deleted
    assert not result.failed
    assert not result.skipped_unmanaged


def test_cowork_stale_entry_resolver_returns_none(tmp_path, diagnostics):
    """Resolver returns None -> file NOT deleted, lockfile entry retained
    in result.failed, one-time warning emitted."""
    from unittest.mock import patch

    project_root = tmp_path / "project"
    project_root.mkdir()

    with patch(
        "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
        return_value=None,
    ):
        result = remove_stale_deployed_files(
            ["cowork://skills/my-skill/SKILL.md"],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
        )

    assert result.deleted == []
    assert "cowork://skills/my-skill/SKILL.md" in result.failed
    # One-time warning about missing OneDrive path.
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("OneDrive path not detected" in m for m in msgs)
    assert any("APM_COPILOT_COWORK_SKILLS_DIR" in m for m in msgs)


def test_cowork_stale_entry_file_already_gone(tmp_path, diagnostics):
    """Cowork root resolves but file is already missing -> lockfile entry
    is removed (not in failed), no error. Idempotent cleanup."""
    from unittest.mock import patch

    cowork_root = tmp_path / "cowork-skills"
    cowork_root.mkdir()
    # Do NOT create the skill file -- it is already gone.

    project_root = tmp_path / "project"
    project_root.mkdir()

    with patch(
        "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
        return_value=cowork_root,
    ):
        result = remove_stale_deployed_files(
            ["cowork://skills/my-skill/SKILL.md"],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
        )

    # Not in deleted (nothing was on disk), not in failed (no error),
    # not in skipped_unmanaged (path was valid).
    assert result.deleted == []
    assert result.failed == []
    assert result.skipped_unmanaged == []


def test_cowork_stale_entry_from_lockfile_error_retains_in_failed(tmp_path, diagnostics):
    """from_lockfile_path raises after validation passes -> entry
    retained in result.failed, warning emitted."""
    from unittest.mock import patch

    cowork_root = tmp_path / "cowork-skills"
    cowork_root.mkdir()

    project_root = tmp_path / "project"
    project_root.mkdir()

    # Use a valid-looking cowork path so it passes validate_deploy_path.
    stale = "cowork://skills/bad-skill/SKILL.md"

    def _boom(_path, _root):
        raise ValueError("simulated resolution failure")

    with (
        patch(
            "apm_cli.integration.copilot_cowork_paths.resolve_copilot_cowork_skills_dir",
            return_value=cowork_root,
        ),
        patch(
            "apm_cli.integration.copilot_cowork_paths.from_lockfile_path",
            side_effect=_boom,
        ),
    ):
        result = remove_stale_deployed_files(
            [stale],
            project_root,
            dep_key="pkg",
            targets=None,
            diagnostics=diagnostics,
        )

    assert result.deleted == []
    assert stale in result.failed
    msgs = [d.message for d in diagnostics._diagnostics]
    assert any("failed path resolution" in m for m in msgs)
