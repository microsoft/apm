"""Mutation traps for the local rendered-write authority extension."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
RULE = "install-deployment-provenance-state"


def test_receipt_guard_accepts_current_owner_routing() -> None:
    report = run_selected_rules(ROOT, {RULE})
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "commands/uninstall/cli.py",
            "else:\n        if integration_cleanup.materialization_session is not None:",
            "finally:\n        if integration_cleanup.materialization_session is not None:",
        ),
        (
            "commands/uninstall/engine.py",
            "materialization_session=materialization_session",
            "materialization_session=None",
        ),
        (
            "commands/uninstall/engine.py",
            "materialization_session=materialization_session,",
            "materialization_session=None,",
        ),
        (
            "commands/uninstall/engine.py",
            "removed_paths=removed_skill_paths",
            "removed_paths=None",
        ),
        (
            "integration/skill_integrator.py",
            "removed_paths.append(target)",
            "pass",
        ),
        (
            "integration/skill_support.py",
            "removed_paths.append(skill_subdir)",
            "pass",
        ),
        (
            "install/phases/cleanup.py",
            "DeploymentMaterializationSession.retire_removed(",
            "parallel_retirement(",
        ),
        (
            "integration/cleanup.py",
            "receipt_retirements.extend(retired)",
            "pass",
        ),
        (
            "core/deployment_ledger.py",
            "and self.baseline(path, targets) is not None",
            "and True",
        ),
        (
            "core/deployment_ledger.py",
            "and record.active_owner == owner",
            "and True",
        ),
        (
            "integration/base_integrator.py",
            "and not (requires_write is not None and requires_write(target_file))",
            "and True",
        ),
        (
            "integration/skill_integrator.py",
            "session.requires_target_write(path, targets, owner)",
            "False",
        ),
        (
            "core/deployment_ledger.py",
            "value = deployed_path_entry(path, self.root, targets)",
            "value = DeploymentLedgerCodec.legacy_value(locator)",
        ),
        (
            "core/deployment_ledger.py",
            "deployed_path_entry(path, self.root, targets),",
            "DeploymentLedgerCodec.legacy_value(locator),",
        ),
        (
            "core/deployment_ledger.py",
            "self._written[key] = value",
            "self._written[key] = DeploymentLedgerCodec.legacy_value(record.locator)",
        ),
        (
            "core/deployment_ledger.py",
            "create_only=not self._state_present",
            "create_only=False",
        ),
        (
            "core/deployment_ledger.py",
            "self._bindings.get(key) != [record.active_owner, self._claim_signature(value)]",
            "False",
        ),
        (
            "install/pipeline.py",
            "materialization_session.bind_committed_claims(",
            "parallel_claim_binding(",
        ),
        (
            "core/deployment_ledger.py",
            "not self._matches_root_identity(data)",
            "False",
        ),
        (
            "core/deployment_ledger.py",
            'data["installation"] != self._installation',
            "False",
        ),
        (
            "core/deployment_ledger.py",
            'return data["root_identity"] == [metadata.st_dev, metadata.st_ino]',
            "return True",
        ),
        (
            "core/deployment_ledger.py",
            'return data["root_identity"] == self._root_identity()',
            "return True",
        ),
        (
            "core/deployment_ledger.py",
            '"deployment-receipts-v2"',
            '"deployment-receipts"',
        ),
        (
            "core/deployment_ledger.py",
            '"volume": darwin_volume_uuid(self.root)',
            '"volume": str(metadata.st_dev)',
        ),
        (
            "core/deployment_ledger.py",
            "self.baseline(path, [target]) != record.content_hash",
            "False",
        ),
        (
            "core/deployment_ledger.py",
            "compute_file_hash(path) != record.content_hash",
            "False",
        ),
        (
            "core/deployment_ledger.py",
            "if snapshot[key] == self._legacy_snapshot.get(key):",
            "if False:",
        ),
        (
            "core/deployment_ledger.py",
            "record.active_owner not in self._valid_owners",
            "False",
        ),
        (
            "integration/skill_integrator.py",
            "on_member_written=written.append",
            "on_member_written=None",
        ),
        (
            "integration/skill_integrator.py",
            "session.baseline(path, targets)",
            "self._skill_recorded_hashes.get(entry(path))",
        ),
        (
            "integration/skill_integrator.py",
            "materialization_session.flush()",
            "pass",
        ),
        (
            "integration/cleanup.py",
            "DeploymentMaterializationSession.retire_removed(",
            "parallel_retirement(",
        ),
        (
            "install/drift.py",
            "from __future__ import annotations",
            "from __future__ import annotations\n"
            "from apm_cli.core.deployment_ledger import DeploymentMaterializationSession",
        ),
        (
            "install/drift.py",
            "from __future__ import annotations",
            'from __future__ import annotations\nLOCAL_BASELINE = ".apm-local"',
        ),
    ],
)
def test_receipt_guard_rejects_bypass_and_parallel_authority(
    path: str, before: str, after: str
) -> None:
    path = f"src/apm_cli/{path}"
    source = (ROOT / path).read_text(encoding="utf-8")
    assert before in source
    mutated = source.replace(before, after, 1)
    ast.parse(mutated)
    report = run_selected_rules(ROOT, {RULE}, source_overrides={path: mutated})
    assert report.failures == ()
    assert any(item.rule_id == RULE for item in report.violations)
