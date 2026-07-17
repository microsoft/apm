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
from apm_cli.install.manifest_reconcile import reconcile_target_deployed_files
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.diagnostics import DiagnosticCollector


class _RecordingLogger:
    """Minimal InstallLogger stand-in that records visibility calls."""

    def __init__(self):
        self.stale_cleanup_calls: list[tuple[str, int]] = []
        self.user_edit_calls: list[tuple[str, str]] = []

    def stale_cleanup(self, dep_key: str, count: int) -> None:
        self.stale_cleanup_calls.append((dep_key, count))

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
    cursor_abs.write_text("# cursor rule\nUSER EDIT\n", encoding="utf-8")
    logger = _RecordingLogger()

    reconcile_target_deployed_files(
        project_root=tmp_path,
        lockfile=lockfile,
        active_targets=[KNOWN_TARGETS["claude"]],
        declared_targets=[KNOWN_TARGETS["claude"]],
        diagnostics=DiagnosticCollector(),
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
    )

    assert changed is True
    assert not cursor_abs.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
