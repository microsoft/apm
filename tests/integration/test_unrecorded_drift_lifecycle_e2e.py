"""Real installed-binary lifecycle proofs for unrecorded deployed files."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_INSTALL_ARGS = ("install", "--no-policy", "--parallel-downloads", "0")
_AUDIT_ARGS = (
    "audit",
    "--ci",
    "--no-policy",
    "--no-fail-fast",
    "--format",
    "json",
)
_SAFE_SKILL = ".claude/skills/repair-scope/SKILL.md"
_HIDDEN_SKILL = ".claude/skills/unicode-scope/SKILL.md"
_USER_SKILL = ".claude/skills/user-authored/SKILL.md"
_HOOK_CONFIG = ".claude/settings.json"
_HOOK_SIDECAR = ".claude/apm-hooks.json"


@dataclass(frozen=True)
class _Scenario:
    project: LocalPackage
    environment: dict[str, str]
    runner: ApmLifecycleRunner


def _skill(name: str, body: str = "Safe fixture content.") -> str:
    return (
        f"---\nname: {name}\ndescription: Hermetic lifecycle fixture {name}\n---\n"
        f"# {name}\n\n{body}\n"
    )


def _hook() -> dict[str, object]:
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo lifecycle-proof"}],
                }
            ]
        }
    }


def _new_scenario(
    root: Path,
    apm_binary_path: Path,
    *,
    skill_names: tuple[str, ...],
    with_hook: bool,
) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    sources = LocalPackageFactory(isolated.package_root)
    package = sources.create("unrecorded-source")
    for name in skill_names:
        sources.add_skill(package, name, _skill(name))
    if with_hook:
        sources.add_hook(package, "lifecycle", _hook())

    repositories = LocalGitRepositoryFactory(
        isolated.repository_root,
        env=environment,
    )
    repository = repositories.create("unrecorded-source", source_tree=package.root)
    commit = repositories.commit(repository, message="seed unrecorded source")
    remote_url = "https://github.com/apm-fixture-org/unrecorded-source"
    command_environment = repositories.url_rewrite_subprocess_env(repository, remote_url)
    project = LocalPackageFactory(isolated.work_root).create(
        "unrecorded-consumer",
        dependencies=(
            {
                "git": remote_url,
                "ref": commit.sha,
                "alias": "unrecorded-source",
            },
        ),
        targets=("claude",),
    )
    return _Scenario(
        project=project,
        environment=command_environment,
        runner=ApmLifecycleRunner(
            (str(apm_binary_path),),
            timeout_seconds=90,
            scenario_timeout_seconds=360,
        ),
    )


def _result_evidence(result: CommandResult) -> str:
    return (
        f"cwd={result.cwd!s}\n"
        f"command={result.command!r}\n"
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _run(
    scenario: _Scenario,
    args: tuple[str, ...],
    *,
    expected_returncode: int,
    scenario_id: str,
) -> CommandResult:
    result = scenario.runner.run(
        args,
        scenario_id=scenario_id,
        cwd=scenario.project.root,
        env=scenario.environment,
    )
    assert result.returncode == expected_returncode, _result_evidence(result)
    return result


def _audit(
    scenario: _Scenario,
    *,
    expected_returncode: int,
    scenario_id: str,
) -> tuple[CommandResult, dict[str, object]]:
    result = _run(
        scenario,
        _AUDIT_ARGS,
        expected_returncode=expected_returncode,
        scenario_id=scenario_id,
    )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return result, payload


def _capture(project_root: Path) -> LifecycleStateSnapshot:
    return LifecycleStateSnapshot.capture(
        project_root,
        targets=("claude",),
        config_paths=(
            PurePosixPath(_SAFE_SKILL),
            PurePosixPath(_HIDDEN_SKILL),
            PurePosixPath(_USER_SKILL),
            PurePosixPath(_HOOK_CONFIG),
            PurePosixPath(_HOOK_SIDECAR),
        ),
    )


def _assert_same_state(
    expected: LifecycleStateSnapshot,
    actual: LifecycleStateSnapshot,
) -> None:
    assert actual.manifest_bytes == expected.manifest_bytes
    assert actual.deployment_records == expected.deployment_records
    assert actual.lockfile_bytes == expected.lockfile_bytes
    assert actual.mcp_state_bytes == expected.mcp_state_bytes
    assert actual.lsp_state_bytes == expected.lsp_state_bytes
    assert actual.files == expected.files
    assert actual.semantic_bytes == expected.semantic_bytes


def _dependency_document(lock: dict[str, object]) -> dict[str, object]:
    dependencies = lock["dependencies"]
    assert isinstance(dependencies, list)
    assert len(dependencies) == 1
    dependency = dependencies[0]
    assert isinstance(dependency, dict)
    return dependency


def _replace_claim(
    project_root: Path,
    deployed_path: str,
    replacement: str | None,
    *,
    replacement_is_file: bool = False,
) -> None:
    lock_path = project_root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    assert isinstance(lock, dict)
    dependency = _dependency_document(lock)
    deployed_files = dependency["deployed_files"]
    deployed_hashes = dependency["deployed_file_hashes"]
    deployments = lock["deployments"]
    assert isinstance(deployed_files, list)
    assert isinstance(deployed_hashes, dict)
    assert isinstance(deployments, list)
    assert deployed_path in deployed_files
    recorded_hash = deployed_hashes.pop(deployed_path)
    deployed_files.remove(deployed_path)

    matching_rows = [
        row for row in deployments if isinstance(row, dict) and row.get("value") == deployed_path
    ]
    assert len(matching_rows) == 1
    row = matching_rows[0]
    if replacement is None:
        deployments.remove(row)
    else:
        deployed_files.append(replacement)
        row["value"] = replacement
        row["content_hash"] = recorded_hash if replacement_is_file else None
        if replacement_is_file:
            deployed_hashes[replacement] = recorded_hash
    dump_yaml(lock, lock_path)


def _remove_covering_claims(project_root: Path, deployed_path: str) -> None:
    """Remove every exact or directory claim that covers one deployed file."""
    lock_path = project_root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    assert isinstance(lock, dict)
    dependency = _dependency_document(lock)
    deployed_files = dependency["deployed_files"]
    deployed_hashes = dependency["deployed_file_hashes"]
    deployments = lock["deployments"]
    assert isinstance(deployed_files, list)
    assert isinstance(deployed_hashes, dict)
    assert isinstance(deployments, list)

    covering = {
        claim
        for claim in deployed_files
        if isinstance(claim, str)
        and (claim == deployed_path or deployed_path.startswith(claim.rstrip("/") + "/"))
    }
    assert deployed_path in covering
    dependency["deployed_files"] = [claim for claim in deployed_files if claim not in covering]
    for claim in covering:
        deployed_hashes.pop(claim, None)
    lock["deployments"] = [
        row
        for row in deployments
        if not (
            isinstance(row, dict) and isinstance(row.get("value"), str) and row["value"] in covering
        )
    ]
    dump_yaml(lock, lock_path)


def _remove_legacy_claim_only(project_root: Path, deployed_path: str) -> None:
    """Remove only compatibility fields while preserving the canonical row."""
    lock_path = project_root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    assert isinstance(lock, dict)
    dependency = _dependency_document(lock)
    deployed_files = dependency["deployed_files"]
    deployed_hashes = dependency["deployed_file_hashes"]
    assert isinstance(deployed_files, list)
    assert isinstance(deployed_hashes, dict)
    covering = {
        claim
        for claim in deployed_files
        if isinstance(claim, str)
        and (claim == deployed_path or deployed_path.startswith(claim.rstrip("/") + "/"))
    }
    assert deployed_path in covering
    dependency["deployed_files"] = [claim for claim in deployed_files if claim not in covering]
    for claim in covering:
        deployed_hashes.pop(claim, None)
    assert any(
        isinstance(row, dict) and row.get("value") == deployed_path for row in lock["deployments"]
    )
    dump_yaml(lock, lock_path)


def _replace_covering_claims_with_file(
    project_root: Path,
    deployed_path: str,
    file_claim: str,
) -> None:
    """Replace covering claims with one hashed file claim at a path prefix."""
    lock_path = project_root / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    assert isinstance(lock, dict)
    dependency = _dependency_document(lock)
    deployed_files = dependency["deployed_files"]
    deployed_hashes = dependency["deployed_file_hashes"]
    deployments = lock["deployments"]
    assert isinstance(deployed_files, list)
    assert isinstance(deployed_hashes, dict)
    assert isinstance(deployments, list)

    covering = {
        claim
        for claim in deployed_files
        if isinstance(claim, str)
        and (claim == deployed_path or deployed_path.startswith(claim.rstrip("/") + "/"))
    }
    assert deployed_path in covering
    recorded_hash = deployed_hashes[deployed_path]
    file_rows = [
        row for row in deployments if isinstance(row, dict) and row.get("value") == deployed_path
    ]
    assert len(file_rows) == 1
    replacement_row = dict(file_rows[0])
    replacement_row["value"] = file_claim
    replacement_row["content_hash"] = recorded_hash

    dependency["deployed_files"] = [claim for claim in deployed_files if claim not in covering] + [
        file_claim
    ]
    for claim in covering:
        deployed_hashes.pop(claim, None)
    deployed_hashes[file_claim] = recorded_hash
    lock["deployments"] = [
        row for row in deployments if not (isinstance(row, dict) and row.get("value") in covering)
    ] + [replacement_row]
    dump_yaml(lock, lock_path)


def _claim_paths(project_root: Path) -> set[str]:
    lock = LockFile.read(project_root / "apm.lock.yaml")
    assert lock is not None
    dependencies = lock.get_package_dependencies()
    assert len(dependencies) == 1
    return set(dependencies[0].deployed_files)


def _drift_entries(payload: dict[str, object]) -> list[dict[str, object]]:
    drift = payload.get("drift")
    assert isinstance(drift, dict)
    entries = drift.get("drift")
    assert isinstance(entries, list)
    assert all(isinstance(entry, dict) for entry in entries)
    return entries


def _check(payload: dict[str, object], name: str) -> dict[str, object]:
    checks = payload["checks"]
    assert isinstance(checks, list)
    matches = [check for check in checks if isinstance(check, dict) and check.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _commit_baseline(project_root: Path, environment: dict[str, str]) -> str:
    subprocess.run(
        ("git", "init", "--initial-branch=main"),
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    (project_root / ".gitignore").write_text("apm_modules/\n", encoding="ascii")
    for command in (("git", "add", "--all"), ("git", "commit", "-m", "capture baseline")):
        subprocess.run(
            command,
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def test_unrecorded_claim_fails_without_writes_and_install_repairs_idempotently(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Normal install is the one-command repair for an under-recorded lock."""
    scenario = _new_scenario(
        tmp_path / "repair",
        apm_binary_path,
        skill_names=("repair-scope",),
        with_hook=True,
    )
    _run(
        scenario,
        _INSTALL_ARGS,
        expected_returncode=0,
        scenario_id="unrecorded-install-baseline",
    )
    user_path = scenario.project.root / _USER_SKILL
    user_path.parent.mkdir(parents=True)
    user_path.write_text(_skill("user-authored"), encoding="ascii")
    baseline_commit = _commit_baseline(scenario.project.root, scenario.environment)
    assert len(baseline_commit) == 40

    baseline = _capture(scenario.project.root)
    claims = _claim_paths(scenario.project.root)
    assert _SAFE_SKILL in claims
    assert _HOOK_CONFIG not in claims
    assert _HOOK_SIDECAR not in claims
    _, clean_payload = _audit(
        scenario,
        expected_returncode=0,
        scenario_id="unrecorded-audit-baseline",
    )
    assert _drift_entries(clean_payload) == []

    _remove_covering_claims(scenario.project.root, _SAFE_SKILL)
    under_recorded = _capture(scenario.project.root)
    _, failed_payload = _audit(
        scenario,
        expected_returncode=1,
        scenario_id="unrecorded-audit-failed",
    )
    after_failed_audit = _capture(scenario.project.root)

    _assert_same_state(under_recorded, after_failed_audit)
    assert _check(failed_payload, "drift")["passed"] is False
    assert _drift_entries(failed_payload) == [
        {
            "inline_diff": "",
            "kind": "unrecorded",
            "package": "",
            "path": _SAFE_SKILL,
        }
    ]

    _run(
        scenario,
        _INSTALL_ARGS,
        expected_returncode=0,
        scenario_id="unrecorded-install-repair",
    )
    assert _SAFE_SKILL in _claim_paths(scenario.project.root)
    _, repaired_payload = _audit(
        scenario,
        expected_returncode=0,
        scenario_id="unrecorded-audit-repaired",
    )
    assert _drift_entries(repaired_payload) == []
    repaired = _capture(scenario.project.root)

    _run(
        scenario,
        _INSTALL_ARGS,
        expected_returncode=0,
        scenario_id="unrecorded-install-repeat",
    )
    _, repeated_payload = _audit(
        scenario,
        expected_returncode=0,
        scenario_id="unrecorded-audit-repeat",
    )
    repeated = _capture(scenario.project.root)
    _assert_same_state(repaired, repeated)
    assert _drift_entries(repeated_payload) == []

    _remove_legacy_claim_only(scenario.project.root, _SAFE_SKILL)
    _, canonical_claim_payload = _audit(
        scenario,
        expected_returncode=1,
        scenario_id="unrecorded-audit-canonical-ledger-claim",
    )
    assert any(
        entry["path"] == _SAFE_SKILL and entry["kind"] == "unrecorded"
        for entry in _drift_entries(canonical_claim_payload)
    )
    _run(
        scenario,
        _INSTALL_ARGS,
        expected_returncode=0,
        scenario_id="unrecorded-install-after-legacy-view-drift",
    )

    directory_claim = str(PurePosixPath(_SAFE_SKILL).parent)
    assert directory_claim in _claim_paths(scenario.project.root)
    _replace_claim(scenario.project.root, _SAFE_SKILL, None)
    _, directory_payload = _audit(
        scenario,
        expected_returncode=0,
        scenario_id="unrecorded-audit-directory-claim",
    )
    assert _drift_entries(directory_payload) == []

    _run(
        scenario,
        _INSTALL_ARGS,
        expected_returncode=0,
        scenario_id="unrecorded-install-after-directory-claim",
    )
    _replace_covering_claims_with_file(
        scenario.project.root,
        _SAFE_SKILL,
        directory_claim,
    )
    _, file_prefix_payload = _audit(
        scenario,
        expected_returncode=1,
        scenario_id="unrecorded-audit-file-prefix",
    )
    assert any(
        entry["path"] == _SAFE_SKILL and entry["kind"] == "unrecorded"
        for entry in _drift_entries(file_prefix_payload)
    )

    _run(
        scenario,
        _INSTALL_ARGS,
        expected_returncode=0,
        scenario_id="unrecorded-install-before-hook-tamper",
    )
    hook_path = scenario.project.root / _HOOK_CONFIG
    hook_path.write_text('{"hooks": {"tampered": true}}\n', encoding="ascii")
    tampered = _capture(scenario.project.root)
    _, tampered_payload = _audit(
        scenario,
        expected_returncode=1,
        scenario_id="unrecorded-audit-hook-tamper",
    )
    _assert_same_state(tampered, _capture(scenario.project.root))
    assert any(
        entry["path"] == _HOOK_CONFIG and entry["kind"] == "modified"
        for entry in _drift_entries(tampered_payload)
    )

    assert baseline.file(_USER_SKILL).content == _skill("user-authored").encode("ascii")
    assert repeated.file(_USER_SKILL).content == baseline.file(_USER_SKILL).content


def test_unrecorded_hidden_unicode_payload_cannot_pass_ci_audit(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Membership failure catches a critical payload omitted from scanner scope."""
    scenario = _new_scenario(
        tmp_path / "hidden-unicode",
        apm_binary_path,
        skill_names=("unicode-scope",),
        with_hook=False,
    )
    _run(
        scenario,
        _INSTALL_ARGS,
        expected_returncode=0,
        scenario_id="unicode-unrecorded-install-baseline",
    )
    _commit_baseline(scenario.project.root, scenario.environment)
    lock = LockFile.read(scenario.project.root / "apm.lock.yaml")
    assert lock is not None
    dependencies = lock.get_package_dependencies()
    assert len(dependencies) == 1
    module_root = (
        dependencies[0].to_dependency_ref().get_install_path(scenario.project.root / "apm_modules")
    )
    hidden_payload = _skill(
        "unicode-scope",
        body="Visible prefix \u202e hidden suffix",
    ).encode("utf-8")
    assert b"\xe2\x80\xae" in hidden_payload
    (module_root / "skills" / "unicode-scope" / "SKILL.md").write_bytes(hidden_payload)
    (scenario.project.root / _HIDDEN_SKILL).write_bytes(hidden_payload)
    _remove_covering_claims(scenario.project.root, _HIDDEN_SKILL)

    before_audit = _capture(scenario.project.root)
    _, payload = _audit(
        scenario,
        expected_returncode=1,
        scenario_id="unicode-unrecorded-audit-failed",
    )
    after_audit = _capture(scenario.project.root)

    _assert_same_state(before_audit, after_audit)
    assert _check(payload, "content-integrity")["passed"] is True
    assert _check(payload, "drift")["passed"] is False
    assert _drift_entries(payload) == [
        {
            "inline_diff": "",
            "kind": "unrecorded",
            "package": "",
            "path": _HIDDEN_SKILL,
        }
    ]
    assert (
        hashlib.sha256(after_audit.file(_HIDDEN_SKILL).content or b"").hexdigest()
        == hashlib.sha256(hidden_payload).hexdigest()
    )
