"""Real CLI coverage for prune deployment-ledger reconciliation."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_ALPHA_KEY = "apm-fixture-org/alpha-kit"
_BETA_KEY = "apm-fixture-org/beta-kit"


def _hook(command: str) -> dict[str, object]:
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }


def _assert_exit(result: CommandResult, expected: int = 0) -> None:
    assert result.returncode == expected, (
        f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _hook_commands(settings_path: Path) -> set[str]:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    return {
        str(handler["command"])
        for entry in settings.get("hooks", {}).get("PreToolUse", [])
        for handler in entry.get("hooks", [])
        if isinstance(handler, dict) and "command" in handler
    }


def test_prune_cascades_dependency_state_and_audit_sees_no_ghost(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    environment = isolated.subprocess_env()
    package_factory = LocalPackageFactory(isolated.package_root)

    alpha = package_factory.create("alpha-kit", targets=("claude",))
    package_factory.add_instruction(
        alpha,
        "alpha",
        "---\napplyTo: '**'\n---\n# Alpha rule\n",
    )
    package_factory.add_hook(alpha, "alpha", _hook("echo alpha-hook"))

    beta = package_factory.create("beta-kit", targets=("claude",))
    package_factory.add_instruction(
        beta,
        "beta",
        "---\napplyTo: '**'\n---\n# Beta rule\n",
    )
    package_factory.add_skill(
        beta,
        "beta",
        "---\nname: beta\n---\n# Beta skill\n",
    )
    package_factory.add_hook(beta, "beta", _hook("echo beta-hook"))

    repository_factory = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    alpha_repo = repository_factory.create("alpha-kit", source_tree=alpha.root)
    beta_repo = repository_factory.create("beta-kit", source_tree=beta.root)
    alpha_commit = repository_factory.commit(alpha_repo, message="seed alpha")
    beta_commit = repository_factory.commit(beta_repo, message="seed beta")
    alpha_url = "https://github.com/apm-fixture-org/alpha-kit.git"
    beta_url = "https://github.com/apm-fixture-org/beta-kit.git"
    repository_factory.install_url_rewrite(alpha_repo, alpha_url)
    repository_factory.install_url_rewrite(beta_repo, beta_url)

    consumer = package_factory.create(
        "consumer",
        dependencies=(
            {"git": alpha_url, "ref": alpha_commit.sha, "alias": "alpha-kit"},
            {"git": beta_url, "ref": beta_commit.sha, "alias": "beta-kit"},
        ),
        targets=("claude",),
    )
    settings_path = consumer.root / ".claude/settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Read"]},
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo manual-hook",
                                }
                            ],
                        }
                    ]
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    runner = ApmLifecycleRunner((str(apm_binary_path),))
    install = runner.run(
        ("install", "--target", "claude", "--no-policy"),
        scenario_id="prune-ledger-install",
        cwd=consumer.root,
        env=environment,
    )
    _assert_exit(install)

    manifest = load_yaml(consumer.manifest_path)
    manifest["dependencies"]["apm"] = [
        dependency
        for dependency in manifest["dependencies"]["apm"]
        if dependency.get("alias") != "beta-kit"
    ]
    dump_yaml(manifest, consumer.manifest_path)

    prune = runner.run(
        ("prune",),
        scenario_id="prune-ledger-prune",
        cwd=consumer.root,
        env=environment,
    )
    _assert_exit(prune)

    regular_report = consumer.root / "reports" / "regular.json"
    regular_audit = runner.run(
        (
            "audit",
            "--no-policy",
            "--format",
            "json",
            "--output",
            str(regular_report),
        ),
        scenario_id="prune-ledger-regular-audit",
        cwd=consumer.root,
        env=environment,
    )
    _assert_exit(regular_audit)
    regular_payload = json.loads(regular_report.read_text(encoding="utf-8"))
    assert regular_payload["passed"] is True
    assert regular_payload["exit_code"] == 0

    ci_report = consumer.root / "reports" / "ci.json"
    ci_audit = runner.run(
        (
            "audit",
            "--ci",
            "--no-policy",
            "--no-drift",
            "--format",
            "json",
            "--output",
            str(ci_report),
        ),
        scenario_id="prune-ledger-ci-audit",
        cwd=consumer.root,
        env=environment,
    )
    _assert_exit(ci_audit)
    assert json.loads(ci_report.read_text(encoding="utf-8"))["passed"] is True

    snapshot = LifecycleStateSnapshot.capture(consumer.root, targets=("claude",))
    owners = {
        owner
        for record in snapshot.deployment_records
        for owner in (*record.owners, record.active_owner)
    }

    assert _BETA_KEY not in owners
    assert _ALPHA_KEY in owners
    assert not (consumer.root / "apm_modules" / "apm-fixture-org" / "beta-kit").exists()
    assert (consumer.root / "apm_modules" / "apm-fixture-org" / "alpha-kit").is_dir()
    assert not (consumer.root / ".claude/rules/beta.md").exists()
    assert not (consumer.root / ".claude/skills/beta").exists()
    assert (consumer.root / ".claude/rules/alpha.md").is_file()
    assert _hook_commands(settings_path) == {"echo alpha-hook", "echo manual-hook"}

    before_second_prune = LifecycleStateSnapshot.capture(
        consumer.root,
        targets=("claude",),
    )
    second_prune = runner.run(
        ("prune",),
        scenario_id="prune-ledger-second-prune",
        cwd=consumer.root,
        env=environment,
    )
    _assert_exit(second_prune)
    after_second_prune = LifecycleStateSnapshot.capture(
        consumer.root,
        targets=("claude",),
    )
    assert after_second_prune.lockfile_bytes == before_second_prune.lockfile_bytes
    assert after_second_prune.files == before_second_prune.files
    assert after_second_prune.semantic_bytes == before_second_prune.semantic_bytes


def test_audit_prune_audit_repairs_injected_ghost_without_deleting_bytes(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    environment = isolated.subprocess_env()
    consumer = LocalPackageFactory(isolated.package_root).create(
        "consumer",
        targets=("claude",),
    )
    survivor_path = ".claude/rules/local.md"
    ghost_path = ".claude/rules/ghost.md"
    survivor = consumer.root / survivor_path
    survivor.parent.mkdir(parents=True)
    survivor.write_text("# Local rule\n", encoding="utf-8")
    sentinel = consumer.root / ghost_path
    sentinel.write_text("# Manual sentinel\n", encoding="utf-8")

    survivor_locator = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="claude",
        value=survivor_path,
        runtime=None,
        scope="project",
    )
    ghost_locator = DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="claude",
        value=ghost_path,
        runtime=None,
        scope="project",
    )
    lockfile = LockFile()
    DeploymentLedgerCodec.apply_to_lockfile(
        DeploymentLedger(
            records={
                survivor_locator.key: DeploymentRecord(
                    locator=survivor_locator,
                    owners=(".",),
                    active_owner=".",
                    content_hash=None,
                ),
                ghost_locator.key: DeploymentRecord(
                    locator=ghost_locator,
                    owners=(_BETA_KEY,),
                    active_owner=_BETA_KEY,
                    content_hash=f"sha256:{'a' * 64}",
                ),
            }
        ),
        lockfile,
    )
    lockfile_path = consumer.root / "apm.lock.yaml"
    lockfile.write(lockfile_path)

    runner = ApmLifecycleRunner((str(apm_binary_path),))
    before_regular = consumer.root / "reports" / "before-regular.json"
    regular_audit = runner.run(
        (
            "audit",
            "--no-policy",
            "--no-drift",
            "--format",
            "json",
            "--output",
            str(before_regular),
        ),
        scenario_id="ghost-before-regular-audit",
        cwd=consumer.root,
        env=environment,
    )
    _assert_exit(regular_audit, expected=1)
    regular_report = json.loads(before_regular.read_text(encoding="utf-8"))
    assert regular_report["findings"][0]["category"] == "deployment-owner"

    before_ci = consumer.root / "reports" / "before-ci.json"
    ci_audit = runner.run(
        (
            "audit",
            "--ci",
            "--no-policy",
            "--no-drift",
            "--format",
            "json",
            "--output",
            str(before_ci),
        ),
        scenario_id="ghost-before-ci-audit",
        cwd=consumer.root,
        env=environment,
    )
    _assert_exit(ci_audit, expected=1)
    ci_report = json.loads(before_ci.read_text(encoding="utf-8"))
    checks = {check["name"]: check for check in ci_report["checks"]}
    assert checks["ref-consistency"]["passed"] is True
    assert checks["deployment-ledger-owners"]["passed"] is False

    prune = runner.run(
        ("prune",),
        scenario_id="ghost-metadata-repair",
        cwd=consumer.root,
        env=environment,
    )
    _assert_exit(prune)
    assert sentinel.read_text(encoding="utf-8") == "# Manual sentinel\n"
    assert "without deleting untrusted bytes" in f"{prune.stdout}\n{prune.stderr}"

    repaired = LockFile.read(lockfile_path)
    assert repaired is not None
    assert DeploymentLedgerCodec.owner_reference_violations(repaired) == ()
    assert {record.locator.value for record in repaired.deployment_ledger.records.values()} == {
        survivor_path
    }

    after_regular = consumer.root / "reports" / "after-regular.json"
    clean_regular = runner.run(
        (
            "audit",
            "--no-policy",
            "--no-drift",
            "--format",
            "json",
            "--output",
            str(after_regular),
        ),
        scenario_id="ghost-after-regular-audit",
        cwd=consumer.root,
        env=environment,
    )
    _assert_exit(clean_regular)
    assert json.loads(after_regular.read_text(encoding="utf-8"))["exit_code"] == 0

    after_ci = consumer.root / "reports" / "after-ci.json"
    clean_ci = runner.run(
        (
            "audit",
            "--ci",
            "--no-policy",
            "--no-drift",
            "--format",
            "json",
            "--output",
            str(after_ci),
        ),
        scenario_id="ghost-after-ci-audit",
        cwd=consumer.root,
        env=environment,
    )
    _assert_exit(clean_ci)
    assert json.loads(after_ci.read_text(encoding="utf-8"))["passed"] is True
