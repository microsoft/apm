"""Static mutation traps for file-level shared skill replacement."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

_ROOT = Path(__file__).resolve().parents[2]
_RULE = "install-deployment-base-integrator"


def test_skill_replacement_has_one_file_materialization_owner() -> None:
    report = run_selected_rules(_ROOT, {_RULE})
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/integration/skill_integrator.py",
            "            copied = rewriter._copy_skill_bundle(",
            "            shutil.rmtree(target)\n            copied = rewriter._copy_skill_bundle(",
        ),
        (
            "src/apm_cli/integration/skill_integrator.py",
            "copied = self.copy_directory_files(",
            "copied = parallel_materializer(",
        ),
        (
            "src/apm_cli/install/services.py",
            "for path in skill_result.deployed_files",
            "for path in tp.rglob('*')",
        ),
        (
            "src/apm_cli/integration/skill_integrator.py",
            "rel_path = rewriter._skill_path_entry(target, materialization_root)",
            "rel_path = target.name",
        ),
    ],
    ids=["blanket-deletion", "second-materializer", "destination-adoption", "parallel-locator"],
)
def test_skill_guard_rejects_split_materialization(path: str, old: str, new: str) -> None:
    source = (_ROOT / path).read_text(encoding="utf-8")
    assert old in source
    mutated = source.replace(old, new)
    ast.parse(mutated)
    report = run_selected_rules(_ROOT, {_RULE}, source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == _RULE for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/install/phases/cleanup.py",
            "surviving_files = surviving_deployed_paths(",
            "surviving_files = parallel_survivor_decision(",
        ),
        (
            "src/apm_cli/install/manifest_reconcile.py",
            "files, _ = union_preserving(",
            "files, _ = parallel_target_decision(",
        ),
        (
            "src/apm_cli/install/phases/cleanup.py",
            "if deployed_file not in surviving_files",
            "if True",
        ),
    ],
    ids=["second-survivor-owner", "second-target-owner", "orphan-survivor-bypass"],
)
def test_skill_cleanup_reuses_canonical_survivor_decisions(path: str, old: str, new: str) -> None:
    source = (_ROOT / path).read_text(encoding="utf-8")
    assert old in source
    mutated = source.replace(old, new, 1)
    ast.parse(mutated)
    rule = "install-deployment-provenance-state"
    report = run_selected_rules(_ROOT, {rule}, source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule for violation in report.violations)


@pytest.mark.parametrize(
    ("path", "old", "new", "rule"),
    [
        (
            "src/apm_cli/integration/base_integrator.py",
            "onerror=fail",
            "onerror=None",
            _RULE,
        ),
        (
            "src/apm_cli/integration/skill_integrator.py",
            "on_member_skipped=self._skill_member_skipped",
            "on_member_skipped=None",
            _RULE,
        ),
        (
            "src/apm_cli/integration/skill_integrator.py",
            "or plan.skill_discovery_complete is not True",
            "or False",
            _RULE,
        ),
        (
            "src/apm_cli/install/deployable_source_plan.py",
            "skill_discovery_complete: bool = False",
            "skill_discovery_complete: bool = True",
            "install-deployment-source-plan",
        ),
        (
            "src/apm_cli/integration/base_integrator.py",
            "if not destination_existed:",
            "if True:",
            _RULE,
        ),
        (
            "src/apm_cli/integration/skill_integrator.py",
            "written_hashes = {entry(path): compute_file_hash(path) for path in copied}",
            "written_hashes = {}",
            _RULE,
        ),
    ],
)
def test_completion_requires_explicit_discovery_and_outcomes(
    path: str, old: str, new: str, rule: str
) -> None:
    source = (_ROOT / path).read_text(encoding="utf-8")
    assert old in source
    mutated = source.replace(old, new, 1)
    ast.parse(mutated)
    report = run_selected_rules(_ROOT, {rule}, source_overrides={path: mutated})
    assert report.failures == ()
    assert any(violation.rule_id == rule for violation in report.violations)
