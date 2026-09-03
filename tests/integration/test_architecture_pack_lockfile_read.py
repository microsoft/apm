"""Architecture guard for read-only bundle lockfile resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.inventory import build_inventory
from scripts.architecture_linter.registry import load_registry
from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "contracts-tooling-lockfile-read"
OWNER = "src/apm_cli/deps/lockfile.py"


def test_bundle_lockfile_reads_have_one_registered_owner() -> None:
    """The registry and executable rule agree on the read-only path owner."""
    registry = load_registry(
        ROOT / ".apm/architecture/owners",
        build_inventory(ROOT).files,
    )
    owner = next(entry for entry in registry.owners if entry.id == "read-only-lockfile-path")
    rule = next(entry for entry in registered_rules() if entry.id == RULE_ID)

    report = run_selected_rules(ROOT, (RULE_ID,))

    assert owner.selectors == (OWNER,)
    assert RULE_ID in owner.guards
    assert rule.guard_ids == (RULE_ID,)
    assert report.failures == ()
    assert report.violations == ()


def test_lockfile_read_rule_rejects_disabled_read_only_guard() -> None:
    """The registered rule catches a migration restored under read-only mode."""
    source = (ROOT / OWNER).read_text(encoding="utf-8")
    mutated = source.replace("    if read_only:\n", "    if False and read_only:\n", 1)
    assert mutated != source

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={OWNER: mutated},
    )

    assert report.failures == ()
    assert any(violation.rule_id == RULE_ID for violation in report.violations)


def test_lockfile_read_rule_rejects_installed_path_fallback_duplication() -> None:
    """The installed-path reader cannot bypass and re-derive the owner."""
    source = (ROOT / OWNER).read_text(encoding="utf-8")
    delegated = "lockfile_path = resolve_lockfile_path_for_read(project_root, read_only=True)"
    duplicated = """lockfile_path = get_lockfile_path(project_root)
            if not lockfile_path.exists():
                legacy_path = project_root / LEGACY_LOCKFILE_NAME
                if legacy_path.exists():
                    lockfile_path = legacy_path"""
    mutated = source.replace(delegated, duplicated, 1)
    assert mutated != source

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={OWNER: mutated},
    )

    assert report.failures == ()
    assert any(violation.rule_id == RULE_ID for violation in report.violations)


def test_lockfile_read_rule_accepts_reformatted_consumer_call() -> None:
    """AST routing survives a harmless multiline formatter change."""
    consumer = "src/apm_cli/bundle/packer.py"
    source = (ROOT / consumer).read_text(encoding="utf-8")
    one_line = "resolve_lockfile_path_for_read(project_root, read_only=dry_run)"
    multiline = """resolve_lockfile_path_for_read(
        project_root,
        read_only=dry_run,
    )"""
    mutated = source.replace(one_line, multiline, 1)
    assert mutated != source

    report = run_selected_rules(
        ROOT,
        (RULE_ID,),
        source_overrides={consumer: mutated},
    )

    assert report.failures == ()
    assert report.violations == ()
