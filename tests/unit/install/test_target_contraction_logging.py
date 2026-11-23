"""Observability regression traps for the target-contraction deletion path.

Deleting a dropped target's deployed file is a destructive operation in the
user's tracked workspace. The canonical contraction owner
(``reconcile_target_deployed_files``) MUST surface every such deletion --
and every user-edit skip -- through the install logger at default verbosity,
exactly like the other callers of the cleanup chokepoint
(``install/phases/cleanup.py``). Without this the file vanishes silently.
"""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.manifest_reconcile import (
    SelectedCleanupOutcome,
    reconcile_target_deployed_files,
)
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.diagnostics import DiagnosticCollector
from tests.utils.deployment_evidence import receipted_evidence

pytestmark = pytest.mark.component


class _RecordingLogger:
    """Minimal InstallLogger stand-in that records visibility calls."""

    def __init__(self):
        self.stale_cleanup_calls: list[tuple[str, int]] = []
        self.user_edit_calls: list[tuple[str, str]] = []

    def stale_cleanup(self, dep_key: str, cleanup) -> None:
        self.stale_cleanup_calls.append((dep_key, len(cleanup.announced_paths)))

    def cleanup_skipped_user_edit(self, rel_path: str, dep_key: str) -> None:
        self.user_edit_calls.append((rel_path, dep_key))


def _seed_lockfile(tmp_path):
    claude_rel = ".claude/rules/scope.md"
    cursor_rel = ".cursor/rules/scope.mdc"
    claude_abs = tmp_path / claude_rel
    cursor_abs = tmp_path / cursor_rel
    for path, body in ((claude_abs, "# claude rule\n"), (cursor_abs, "# cursor rule\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url="https://github.com/acme/pkg",
            resolved_ref="1.0.0",
            deployed_files=[claude_rel, cursor_rel],
            deployed_file_hashes={
                claude_rel: compute_file_hash(claude_abs),
                cursor_rel: compute_file_hash(cursor_abs),
            },
        )
    )
    return lockfile, claude_rel, cursor_rel, claude_abs, cursor_abs


def test_contraction_deletion_is_surfaced_at_default_verbosity(tmp_path):
    """Narrowing to claude MUST report the dropped cursor file deletion via
    ``logger.stale_cleanup`` so the destructive change is visible without
    ``--verbose``; the regression trap fails if the ``on_cleanup`` wiring is
    dropped from the contraction owner."""
    lockfile, _claude_rel, _cursor_rel, _claude_abs, cursor_abs = _seed_lockfile(tmp_path)
    logger = _RecordingLogger()

    changed = reconcile_target_deployed_files(
        project_root=tmp_path,
        lockfile=lockfile,
        active_targets=[KNOWN_TARGETS["claude"]],
        declared_targets=[KNOWN_TARGETS["claude"]],
        diagnostics=DiagnosticCollector(),
        evidence=receipted_evidence(tmp_path),
        logger=logger,
    )

    assert changed is True
    assert not cursor_abs.exists()
    assert sum(count for _key, count in logger.stale_cleanup_calls) == 1, (
        "the single dropped-target file deletion MUST be surfaced via stale_cleanup"
    )
    assert logger.user_edit_calls == [], "no user edit occurred, so no skip should be reported"


def test_contraction_user_edit_skip_is_surfaced(tmp_path):
    """A user-edited dropped-target file MUST be kept AND reported via
    ``logger.cleanup_skipped_user_edit`` so the user learns APM preserved it."""
    lockfile, _claude_rel, cursor_rel, _claude_abs, cursor_abs = _seed_lockfile(tmp_path)
    evidence = receipted_evidence(tmp_path)
    cursor_abs.write_text("# cursor rule\nUSER EDIT\n", encoding="utf-8")
    logger = _RecordingLogger()

    reconcile_target_deployed_files(
        project_root=tmp_path,
        lockfile=lockfile,
        active_targets=[KNOWN_TARGETS["claude"]],
        declared_targets=[KNOWN_TARGETS["claude"]],
        diagnostics=DiagnosticCollector(),
        evidence=evidence,
        logger=logger,
    )

    assert cursor_abs.exists(), "a user-edited file MUST be preserved, not deleted"
    assert cursor_rel in {path for path, _key in logger.user_edit_calls}, (
        "the preserved user-edited file MUST be surfaced via cleanup_skipped_user_edit"
    )
    assert sum(count for _key, count in logger.stale_cleanup_calls) == 0, (
        "nothing was deleted, so no stale_cleanup count should be reported"
    )


def test_contraction_without_logger_still_deletes(tmp_path):
    """Positive control: the logger is optional -- omitting it MUST NOT change
    the deletion behavior (surfacing is additive, not load-bearing for safety)."""
    lockfile, _claude_rel, _cursor_rel, _claude_abs, cursor_abs = _seed_lockfile(tmp_path)

    changed = reconcile_target_deployed_files(
        project_root=tmp_path,
        lockfile=lockfile,
        active_targets=[KNOWN_TARGETS["claude"]],
        declared_targets=[KNOWN_TARGETS["claude"]],
        diagnostics=DiagnosticCollector(),
        evidence=receipted_evidence(tmp_path),
    )

    assert changed is True
    assert not cursor_abs.exists()


def test_contraction_removes_only_selected_removed_target_ownership(tmp_path):
    """A removed target's cleanup must not depend on surviving target selection."""
    survivor_rel = ".config/opencode/agents/survivor.md"
    removed_rel = ".config/opencode/agents/orphan.md"
    survivor_abs = tmp_path / survivor_rel
    removed_abs = tmp_path / removed_rel
    for path, body in ((survivor_abs, "# survivor\n"), (removed_abs, "# orphan\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url="https://github.com/acme/survivor",
            deployed_files=[survivor_rel],
            deployed_file_hashes={survivor_rel: compute_file_hash(survivor_abs)},
        )
    )
    removed = LockedDependency(
        repo_url="https://github.com/acme/removed",
        deployed_files=[removed_rel],
        deployed_file_hashes={removed_rel: compute_file_hash(removed_abs)},
    )
    lockfile.add_dependency(removed)

    reconcile_target_deployed_files(
        project_root=tmp_path,
        lockfile=lockfile,
        active_targets=[KNOWN_TARGETS["agent-skills"]],
        declared_targets=[KNOWN_TARGETS["agent-skills"]],
        diagnostics=DiagnosticCollector(),
        evidence=receipted_evidence(tmp_path),
        dependency_keys={removed.get_unique_key()},
        remove_selected_ownership=True,
        user_scope=True,
    )

    assert survivor_abs.exists()
    assert not removed_abs.exists()


def test_selected_ownership_cleanup_keeps_a_file_shared_with_project_content(tmp_path):
    """A file the project's own content still claims survives its co-owner's removal (#4703)."""
    shared_rel = ".config/opencode/agents/shared.md"
    shared_abs = tmp_path / shared_rel
    shared_abs.parent.mkdir(parents=True, exist_ok=True)
    shared_abs.write_text("# shared\n", encoding="utf-8")

    lockfile = LockFile()
    lockfile.local_deployed_files = [shared_rel]
    removed = LockedDependency(
        repo_url="https://github.com/acme/removed",
        deployed_files=[shared_rel],
        deployed_file_hashes={shared_rel: compute_file_hash(shared_abs)},
    )
    lockfile.add_dependency(removed)

    reconcile_target_deployed_files(
        project_root=tmp_path,
        lockfile=lockfile,
        active_targets=[KNOWN_TARGETS["opencode"]],
        declared_targets=[KNOWN_TARGETS["opencode"]],
        diagnostics=DiagnosticCollector(),
        evidence=receipted_evidence(tmp_path),
        dependency_keys={removed.get_unique_key()},
        remove_selected_ownership=True,
        user_scope=True,
    )

    assert shared_abs.read_text(encoding="utf-8") == "# shared\n"


def test_selected_ownership_cleanup_removes_unique_active_target_file(tmp_path):
    """Removing one owner deletes its file even when the target remains active."""
    survivor_rel = ".config/opencode/agents/survivor.md"
    removed_rel = ".config/opencode/agents/orphan.md"
    survivor_abs = tmp_path / survivor_rel
    removed_abs = tmp_path / removed_rel
    for path, body in ((survivor_abs, "# survivor\n"), (removed_abs, "# orphan\n")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    lockfile = LockFile()
    survivor = LockedDependency(
        repo_url="https://github.com/acme/survivor",
        deployed_files=[survivor_rel],
        deployed_file_hashes={survivor_rel: compute_file_hash(survivor_abs)},
    )
    removed = LockedDependency(
        repo_url="https://github.com/acme/removed",
        deployed_files=[removed_rel],
        deployed_file_hashes={removed_rel: compute_file_hash(removed_abs)},
    )
    lockfile.add_dependency(survivor)
    lockfile.add_dependency(removed)

    reconcile_target_deployed_files(
        project_root=tmp_path,
        lockfile=lockfile,
        active_targets=[KNOWN_TARGETS["opencode"]],
        declared_targets=[KNOWN_TARGETS["opencode"]],
        diagnostics=DiagnosticCollector(),
        evidence=receipted_evidence(tmp_path),
        dependency_keys={removed.get_unique_key()},
        remove_selected_ownership=True,
        user_scope=True,
    )

    assert survivor_abs.exists()
    assert not removed_abs.exists()


def test_selected_cleanup_outcome_keeps_lock_truthful_after_partial_removal(tmp_path):
    """A refused removal records only the files still on disk (#3656)."""
    target = KNOWN_TARGETS["opencode"]
    kept_rel = ".config/opencode/agents/kept.md"
    gone_rel = ".config/opencode/agents/gone.md"
    kept_abs = tmp_path / kept_rel
    gone_abs = tmp_path / gone_rel
    for path in (kept_abs, gone_abs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# managed\n", encoding="utf-8")
    removed = LockedDependency(
        repo_url="https://github.com/acme/removed",
        deployed_files=[kept_rel, gone_rel],
        deployed_file_hashes={
            kept_rel: compute_file_hash(kept_abs),
            gone_rel: compute_file_hash(gone_abs),
        },
    )
    lockfile = LockFile()
    lockfile.add_dependency(removed)
    evidence = receipted_evidence(tmp_path)
    kept_abs.write_text("# edited\n", encoding="utf-8")
    key = removed.get_unique_key()
    outcome = SelectedCleanupOutcome()

    reconcile_target_deployed_files(
        project_root=tmp_path,
        lockfile=lockfile,
        active_targets=[target],
        declared_targets=[target],
        diagnostics=DiagnosticCollector(),
        evidence=evidence,
        dependency_keys={key},
        remove_selected_ownership=True,
        selected_outcome=outcome,
        user_scope=True,
    )

    assert kept_abs.exists() and not gone_abs.exists()
    assert outcome.retained_paths == {kept_rel}
    assert lockfile.get_dependency(key).deployed_files == [kept_rel, gone_rel]
    outcome.apply_to(lockfile)
    dependency = lockfile.get_dependency(key)
    assert dependency.deployed_files == [kept_rel]
    assert set(dependency.deployed_file_hashes) == {kept_rel}


def test_selected_cleanup_outcome_keeps_preserved_profile_rows(tmp_path):
    """Rows another profile keeps stay claimed when the removal is refused."""
    target = KNOWN_TARGETS["opencode"]
    kept_rel = ".config/opencode/agents/kept.md"
    shared_rel = ".config/opencode/agents/shared.md"
    for rel in (kept_rel, shared_rel):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("# managed\n", encoding="utf-8")
    removed = LockedDependency(
        repo_url="https://github.com/acme/removed",
        deployed_files=[kept_rel, shared_rel],
        deployed_file_hashes={
            rel: compute_file_hash(tmp_path / rel) for rel in (kept_rel, shared_rel)
        },
    )
    lockfile = LockFile()
    lockfile.add_dependency(removed)
    evidence = receipted_evidence(tmp_path)
    (tmp_path / kept_rel).write_text("# edited\n", encoding="utf-8")
    key = removed.get_unique_key()
    outcome = SelectedCleanupOutcome()

    reconcile_target_deployed_files(
        project_root=tmp_path,
        lockfile=lockfile,
        active_targets=[target],
        declared_targets=[target],
        diagnostics=DiagnosticCollector(),
        evidence=evidence,
        dependency_keys={key},
        remove_selected_ownership=True,
        selected_outcome=outcome,
        user_scope=True,
        preserved_selected_values=frozenset({shared_rel}),
    )

    assert (tmp_path / shared_rel).exists()
    assert outcome.retained_paths == {kept_rel}
    outcome.apply_to(lockfile)
    dependency = lockfile.get_dependency(key)
    assert sorted(dependency.deployed_files) == sorted([kept_rel, shared_rel])
    assert set(dependency.deployed_file_hashes) == {kept_rel, shared_rel}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_selected_removal_keeps_a_skill_member_a_surviving_dependency_deploys(tmp_path):
    """#4750: the departing owner's skill directory holds a survivor's member; it stays."""
    skill = ".agents/skills/alpha"
    own_rel, shared_rel = f"{skill}/SKILL.md", f"{skill}/shared.md"
    own_abs, shared_abs = tmp_path / own_rel, tmp_path / shared_rel
    own_abs.parent.mkdir(parents=True)
    own_abs.write_text("# alpha\n", encoding="utf-8")
    shared_abs.write_bytes(b"# shared by the survivor\n")
    hashes = {own_rel: compute_file_hash(own_abs), shared_rel: compute_file_hash(shared_abs)}

    lockfile = LockFile()
    survivor = LockedDependency(
        repo_url="https://github.com/acme/survivor",
        deployed_files=[shared_rel],
        deployed_file_hashes={shared_rel: hashes[shared_rel]},
    )
    removed = LockedDependency(
        repo_url="https://github.com/acme/removed",
        deployed_files=[skill],
        deployed_file_hashes=hashes,
    )
    lockfile.add_dependency(survivor)
    lockfile.add_dependency(removed)

    reconcile_target_deployed_files(
        project_root=tmp_path,
        lockfile=lockfile,
        active_targets=[KNOWN_TARGETS["copilot"]],
        declared_targets=[KNOWN_TARGETS["copilot"]],
        diagnostics=DiagnosticCollector(),
        evidence=receipted_evidence(tmp_path),
        dependency_keys={removed.get_unique_key()},
        remove_selected_ownership=True,
    )

    assert shared_abs.read_bytes() == b"# shared by the survivor\n"
    assert not own_abs.exists()
    assert lockfile.dependencies[survivor.get_unique_key()].deployed_files == [shared_rel]


def test_selected_removal_keeps_a_member_edited_between_check_and_unlink(tmp_path):
    """#4750: each member is rechecked immediately before its unlink; a late edit stays."""
    skill = ".agents/skills/alpha"
    own_rel, late_rel, shared_rel = (f"{skill}/{name}" for name in ("SKILL.md", "notes.md", "s.md"))
    for rel, body in ((own_rel, "# alpha\n"), (late_rel, "notes\n"), (shared_rel, "shared\n")):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body, encoding="utf-8")
    hashes = {rel: compute_file_hash(tmp_path / rel) for rel in (own_rel, late_rel, shared_rel)}
    lockfile = LockFile()
    survivor = LockedDependency(
        repo_url="https://github.com/acme/survivor",
        deployed_files=[shared_rel],
        deployed_file_hashes={shared_rel: hashes[shared_rel]},
    )
    removed = LockedDependency(
        repo_url="https://github.com/acme/removed",
        deployed_files=[skill],
        deployed_file_hashes=hashes,
    )
    lockfile.add_dependency(survivor)
    lockfile.add_dependency(removed)
    evidence = receipted_evidence(tmp_path)
    checked = evidence.verdict

    def scan_then_user_edits(claim, physical, **kwargs):
        verdict = checked(claim, physical, **kwargs)
        if claim == late_rel and physical.read_text(encoding="utf-8") == "notes\n":
            physical.write_text("edited after the check\n", encoding="utf-8")
        return verdict

    evidence.verdict = scan_then_user_edits  # the user's edit lands right after the check

    reconcile_target_deployed_files(
        project_root=tmp_path,
        lockfile=lockfile,
        active_targets=[KNOWN_TARGETS["copilot"]],
        declared_targets=[KNOWN_TARGETS["copilot"]],
        diagnostics=DiagnosticCollector(),
        evidence=evidence,
        dependency_keys={removed.get_unique_key()},
        remove_selected_ownership=True,
    )

    assert (tmp_path / late_rel).read_text(encoding="utf-8") == "edited after the check\n"
    assert (tmp_path / shared_rel).read_text(encoding="utf-8") == "shared\n"
    assert not (tmp_path / own_rel).exists()


def _shared_skill_lock(tmp_path, own_files, survivor_claim):
    """A departing owner claims the skill directory; a survivor claims one member."""
    skill = ".agents/skills/alpha"
    hashes = {rel: compute_file_hash(tmp_path / rel) for rel in own_files}
    lockfile = LockFile()
    survivor = LockedDependency(
        repo_url="https://github.com/acme/survivor",
        deployed_files=[survivor_claim],
        deployed_file_hashes={},
    )
    removed = LockedDependency(
        repo_url="https://github.com/acme/removed",
        deployed_files=[skill],
        deployed_file_hashes=hashes,
    )
    lockfile.add_dependency(survivor)
    lockfile.add_dependency(removed)
    return lockfile, removed


def _remove_selected(tmp_path, lockfile, removed, evidence):
    reconcile_target_deployed_files(
        project_root=tmp_path,
        lockfile=lockfile,
        active_targets=[KNOWN_TARGETS["copilot"]],
        declared_targets=[KNOWN_TARGETS["copilot"]],
        diagnostics=DiagnosticCollector(),
        evidence=evidence,
        dependency_keys={removed.get_unique_key()},
        remove_selected_ownership=True,
    )


def test_selected_removal_keeps_a_survivor_member_claimed_in_another_spelling(tmp_path):
    """#4750: Shared.md and shared.md are one file on a case-insensitive filesystem."""
    skill = tmp_path / ".agents/skills/alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (skill / "shared.md").write_text("shared\n", encoding="utf-8")
    one_file = (skill / "SHARED.md").exists()
    if not one_file:
        (skill / "Shared.md").write_text("survivor\n", encoding="utf-8")
    own = [".agents/skills/alpha/SKILL.md", ".agents/skills/alpha/shared.md"]
    lockfile, removed = _shared_skill_lock(tmp_path, own, ".agents/skills/alpha/Shared.md")

    _remove_selected(tmp_path, lockfile, removed, receipted_evidence(tmp_path))

    assert not (skill / "SKILL.md").exists()
    if one_file:
        # The survivor's claim names this very file: it stays byte-identical.
        assert (skill / "shared.md").read_text(encoding="utf-8") == "shared\n"
    else:
        # Distinct files: the departing owner's goes, the survivor's stays.
        assert not (skill / "shared.md").exists()
        assert (skill / "Shared.md").read_text(encoding="utf-8") == "survivor\n"


def test_selected_removal_keeps_a_member_whose_parent_became_a_link(tmp_path):
    """#4750: a parent swapped for a link after the check never routes the unlink outside."""
    skill = tmp_path / ".agents/skills/alpha"
    (skill / "refs").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (skill / "refs" / "x.md").write_text("ref\n", encoding="utf-8")
    (skill / "s.md").write_text("shared\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_text("ref\n", encoding="utf-8")
    own = [".agents/skills/alpha/SKILL.md", ".agents/skills/alpha/refs/x.md"]
    lockfile, removed = _shared_skill_lock(tmp_path, own, ".agents/skills/alpha/s.md")
    evidence = receipted_evidence(tmp_path)
    checked = evidence.verdict

    def check_then_swap(claim, physical, **kwargs):
        verdict = checked(claim, physical, **kwargs)
        refs = skill / "refs"
        if claim.endswith("refs/x.md") and not refs.is_symlink():
            refs.rename(tmp_path / "moved-refs")
            try:
                refs.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                pytest.skip("symbolic links are unavailable")
        return verdict

    evidence.verdict = check_then_swap

    _remove_selected(tmp_path, lockfile, removed, evidence)

    assert (outside / "x.md").read_text(encoding="utf-8") == "ref\n"
    assert (skill / "s.md").read_text(encoding="utf-8") == "shared\n"
