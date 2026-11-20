"""Mutation proofs for each sub-check of the lockfile write-authority guard (#4086)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.contracts_lockfile_write_authority import (
    COMMIT_SITES,
    FLUSH_SITES,
)
from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_RULE_ID = "contracts-tooling-lockfile-write-authority"

# Shrink-only: a new lockfile commit site needs this baseline to grow, in review.
_COMMIT_SITE_BASELINE = frozenset(
    {
        ("src/apm_cli/install/bundle_admission.py", "record_unpacked_files"),
        ("src/apm_cli/commands/prune.py", "prune"),
        ("src/apm_cli/commands/uninstall/cli.py", "_abort_if_retained_target_cleanup_paths"),
        ("src/apm_cli/commands/uninstall/cli.py", "_persist_uninstall_lockfile"),
        ("src/apm_cli/commands/update.py", "_annotate_lockfile_revision_tags"),
        ("src/apm_cli/install/local_bundle_handler.py", "install_local_bundle"),
        ("src/apm_cli/install/local_bundle_hook_trust.py", "_drop_lock_rows"),
        ("src/apm_cli/install/lsp/integration.py", "run_owned_lsp_integration"),
        ("src/apm_cli/install/manifest_reconcile.py", "reconcile_project_deployed_state"),
        ("src/apm_cli/install/mcp/integration.py", "run_mcp_integration"),
        ("src/apm_cli/install/mcp/integration.py", "run_owned_mcp_integration"),
        ("src/apm_cli/install/phases/claude_plugins.py", "run"),
        ("src/apm_cli/install/phases/lockfile.py", "_reconcile_target_deployed_files"),
        ("src/apm_cli/install/phases/lockfile.py", "_write_if_changed"),
        ("src/apm_cli/install/phases/post_deps_local.py", "run"),
        ("src/apm_cli/integration/lsp_integrator.py", "update_lockfile"),
        ("src/apm_cli/integration/cleanup.py", "_render_locked"),
        ("src/apm_cli/integration/mcp_integrator.py", "update_lockfile"),
    }
)

# (sub-check, path, old, new, message fragment), sorted by sub-check.
_SUB_CHECK_MUTATIONS = (
    (
        "1-writer-vocabulary",
        "src/apm_cli/deps/lockfile_write_authority.py",
        '    COMPILE = "compile"\n',
        '    COMPILE = "compile"\n    EXPORT = "export"\n',
        "LockfileWriter members",
    ),
    (
        "2-entry-point-scope",
        "src/apm_cli/commands/compile/cli.py",
        "@lockfile_writer(LockfileWriter.COMPILE)\n",
        "",
        "_run_compilation must open lockfile_writer(LockfileWriter.COMPILE)",
    ),
    (
        "3-staging-route",
        "src/apm_cli/deps/lockfile.py",
        "        staged = has_staged_lockfile(path)\n",
        "        staged = None\n",
        "read must call has_staged_lockfile",
    ),
    (
        "4-closed-commit-sites",
        "src/apm_cli/commands/update.py",
        "def _annotate_lockfile_revision_tags(",
        "def _annotate_revision_tags(",
        "writes a lockfile outside the registered commit sites",
    ),
    (
        "5-removal-route",
        "src/apm_cli/commands/prune.py",
        "                    remove_lockfile(lockfile_path)\n",
        "                    lockfile_path.unlink(missing_ok=True)\n",
        "remove or restore lockfiles through remove_lockfile",
    ),
    (
        "6-claim-binding-after-commit",
        "src/apm_cli/install/pipeline.py",
        "                after_lockfile_commit(\n"
        "                    lambda: materialization_session.bind_committed_claims(\n"
        "                        LockFile.read(get_lockfile_path(ctx.apm_dir))\n"
        "                    )\n"
        "                )\n",
        "                materialization_session.bind_committed_claims(\n"
        "                    LockFile.read(get_lockfile_path(ctx.apm_dir))\n"
        "                )\n",
        "binds deployment claims before the lockfile commits",
    ),
    (
        "7-closed-flush-sites",
        "src/apm_cli/install/mcp/command.py",
        "    migrate_lockfile_if_needed(command.project_root)\n",
        "    migrate_lockfile_if_needed(command.project_root)\n    flush_lockfile_writes()\n",
        "only the registered flush sites may call flush_lockfile_writes",
    ),
)


def test_commit_sites_only_shrink() -> None:
    assert COMMIT_SITES <= _COMMIT_SITE_BASELINE


_FLUSH_SITE_BASELINE = frozenset(
    {
        ("src/apm_cli/commands/prune.py", "prune"),
        ("src/apm_cli/commands/uninstall/cli.py", "_abort_if_retained_target_cleanup_paths"),
        ("src/apm_cli/commands/uninstall/cli.py", "_persist_uninstall_lockfile"),
        ("src/apm_cli/core/lifecycle_scripts.py", "fire"),
        ("src/apm_cli/install/phases/claude_plugins.py", "run"),
    }
)


def test_flush_sites_only_shrink() -> None:
    assert FLUSH_SITES <= _FLUSH_SITE_BASELINE


def test_sub_check_mutations_are_sorted() -> None:
    names = [case[0] for case in _SUB_CHECK_MUTATIONS]
    assert names == sorted(names)


@pytest.mark.parametrize(
    ("path", "old", "new", "message"),
    [case[1:] for case in _SUB_CHECK_MUTATIONS],
    ids=[case[0] for case in _SUB_CHECK_MUTATIONS],
)
def test_each_sub_check_rejects_its_own_mutation(
    path: str, old: str, new: str, message: str
) -> None:
    source = (_ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(old, new, 1)
    assert mutated != source

    result = run_selected_rules(_ROOT, {_RULE_ID}, source_overrides={path: mutated})

    assert result.failures == ()
    assert result.violations, path
    assert all(message in violation.message for violation in result.violations), [
        violation.message for violation in result.violations
    ]
