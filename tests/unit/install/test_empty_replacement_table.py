"""Empty-graph intent uses lock persistence without deployment or source I/O."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import LockFile
from apm_cli.install.helpers.no_work import write_empty_lockfile
from apm_cli.install.pipeline import run_install_pipeline
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.replacement import parse_replace_block, replacement_rule_to_dict

pytestmark = pytest.mark.component

_RULE = {"for": {"git": "https://github.com/example/base.git"}, "with": {"path": "../absent"}}


@pytest.mark.parametrize("declared", [None, [], [_RULE]])
@pytest.mark.parametrize("lockfile_only", [False, True], ids=["install", "lock"])
def test_empty_pipeline_persists_intent_without_running_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared: list[dict] | None,
    lockfile_only: bool,
) -> None:
    for name in ("get_deploy_root", "get_source_root", "get_apm_dir"):
        monkeypatch.setattr(f"apm_cli.core.scope.{name}", lambda scope: tmp_path)
    unexpected = Mock(side_effect=AssertionError("Empty graph must not run deployment phases"))
    monkeypatch.setattr("apm_cli.install.pipeline._run_phase", unexpected)
    monkeypatch.setattr(
        "apm_cli.core.target_detection.resolve_effective_target_decision", unexpected
    )
    rules = None if declared is None else parse_replace_block(declared)
    package = APMPackage(name="empty", version="1.0.0", replace_rules=rules)

    run_install_pipeline(package, lockfile_only=lockfile_only)

    lock_path = tmp_path / "apm.lock.yaml"
    assert lock_path.exists() is (declared is not None or lockfile_only)
    if lock_path.exists():
        locked = LockFile.read(lock_path)
        assert locked is not None
        assert locked.dependencies == {}
        assert locked.replacements == (
            None if rules is None else [replacement_rule_to_dict(rule) for rule in rules]
        )
        before = lock_path.read_bytes()
        run_install_pipeline(package, lockfile_only=lockfile_only)
        assert lock_path.read_bytes() == before
    unexpected.assert_not_called()
    assert not (tmp_path / "apm_modules").exists()


def test_table_only_writer_preserves_receipts_and_service_metadata(tmp_path: Path) -> None:
    locator = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="codex",
        value=".agents/skills/manual/SKILL.md",
        runtime=None,
        scope="project",
    )
    record = DeploymentRecord(
        locator=locator,
        owners=("bundle:manual",),
        active_owner="bundle:manual",
        content_hash="sha256:" + "a" * 64,
    )
    lock_path = tmp_path / "apm.lock.yaml"
    LockFile(
        replacements=[],
        deployment_ledger=DeploymentLedger({locator.key: record}),
        mcp_servers=["mcp-fixture"],
        mcp_configs={"claude": {"mcp-fixture": {"command": "fixture"}}},
        lsp_servers=["lsp-fixture"],
        lsp_configs={"copilot": {"lsp-fixture": {"command": "fixture"}}},
    ).save(lock_path)
    before = LockFile.read(lock_path)
    package = APMPackage(name="empty", version="1.0.0", replace_rules=parse_replace_block([_RULE]))

    write_empty_lockfile(tmp_path, apm_package=package, lockfile_only=False)

    after = LockFile.read(lock_path)
    assert before is not None and after is not None
    assert after.replacements == [_RULE]
    after.replacements = before.replacements
    assert after.is_semantically_equivalent(before)
    assert after.deployment_ledger.records == before.deployment_ledger.records


@pytest.mark.parametrize("target", [None, "claude"], ids=["no-target", "explicit-target"])
def test_bundle_only_pipeline_persists_intent_without_running_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str | None
) -> None:
    for name in ("get_deploy_root", "get_source_root", "get_apm_dir"):
        monkeypatch.setattr(f"apm_cli.core.scope.{name}", lambda scope: tmp_path)
    unexpected = Mock(side_effect=AssertionError("Bundle receipts are not install cleanup work"))
    monkeypatch.setattr("apm_cli.install.pipeline._run_phase", unexpected)
    monkeypatch.setattr(
        "apm_cli.core.target_detection.resolve_effective_target_decision", unexpected
    )
    lock_path = tmp_path / "apm.lock.yaml"
    lockfile = LockFile(replacements=[])
    bundled = ".agents/skills/bundled/SKILL.md"
    DeploymentLedgerCodec.record_local_bundle_files(
        lockfile, [bundled], {bundled: "sha256:" + "b" * 64}
    )
    lockfile.save(lock_path)
    before = LockFile.read(lock_path)
    package = APMPackage(name="empty", version="1.0.0", replace_rules=parse_replace_block([_RULE]))

    run_install_pipeline(package, target=target)

    after = LockFile.read(lock_path)
    assert before is not None and after is not None
    assert after.replacements == [_RULE]
    after.replacements = before.replacements
    assert after.is_semantically_equivalent(before)
    unexpected.assert_not_called()
    assert not (tmp_path / "apm_modules").exists()


def test_table_write_failure_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failure = Mock(side_effect=PermissionError("lock is read-only"))
    monkeypatch.setattr(LockFile, "save", failure)
    package = APMPackage(name="empty", version="1.0.0", replace_rules=parse_replace_block([_RULE]))

    with pytest.raises(PermissionError, match="lock is read-only"):
        write_empty_lockfile(tmp_path, apm_package=package, lockfile_only=False)
    failure.assert_called_once()
