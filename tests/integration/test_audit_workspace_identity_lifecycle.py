"""Installed-CLI audit contracts for native workspace spellings and cold clones."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.windows_compat,
]

_AUDIT = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")
_INSTRUCTION = "---\napplyTo: '**'\n---\n# Dependency\nKeep the managed content.\n"


@dataclass(frozen=True)
class _Workspace:
    isolated: IsolatedApmEnvironment
    root: Path
    source: Path
    runner: ApmLifecycleRunner
    env: dict[str, str]


def _git(root: Path, env: dict[str, str], *args: str) -> str:
    """Run Git against only the owned fixture repositories."""
    return subprocess.run(
        ("git", *args),
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout


def _spellings(root: Path, env: dict[str, str]) -> tuple[tuple[str, Path], ...]:
    """Prove host-created Windows aliases, or use the ordinary POSIX control."""
    if os.name != "nt":
        return (("posix", root.resolve()),)

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    spellings = []
    for name in ("GetLongPathNameW", "GetShortPathNameW"):
        function = getattr(kernel32, name)
        function.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        function.restype = wintypes.DWORD
        size = function(str(root), None, 0)
        assert size, f"{name} failed: {ctypes.get_last_error()}"
        buffer = ctypes.create_unicode_buffer(size)
        written = function(str(root), buffer, size)
        assert 0 < written < size, f"{name} failed: {ctypes.get_last_error()}"
        spellings.append(Path(buffer.value))
    long, short = spellings
    assert str(long).casefold() != str(short).casefold(), (
        f"Native 8.3 evidence unavailable: {long!s} has no distinct short spelling"
    )
    assert "~" in str(short), f"Expected a host-created 8.3 alias: {short!s}"
    assert long.samefile(short), (long, short)
    probe = ApmLifecycleRunner((sys.executable,))
    observed = probe.run_sequence(
        (("-c", "import os; print(os.getcwd())"),),
        expected_returncodes=(0,),
        scenario_id="native-short-cwd",
        cwd=short,
        env=env,
    )[0].stdout.strip()
    assert Path(observed).samefile(long)
    assert observed.casefold() != str(long).casefold(), (
        f"Child cwd did not retain the native short spelling: {observed!r}"
    )
    return (("windows-long", long), ("windows-short", short))


def _seed(tmp_path: Path, apm_binary_path: Path, *, dependency_kind: str = "local") -> _Workspace:
    """Install a baseline, commit its real outputs, then clone without modules."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path / "Audit Workspace Long Name",
        base_env=os.environ,
    )
    env = isolated.subprocess_env(overrides={"PYTHONDONTWRITEBYTECODE": "1"})
    packages = LocalPackageFactory(isolated.package_root)
    source = packages.create("sibling-package")
    packages.add_instruction(source, "dependency", _INSTRUCTION)
    repositories = LocalGitRepositoryFactory(isolated.package_root, env=env)
    dependency = {"path": "../sibling-package"}
    if dependency_kind == "git":
        remote = repositories.create("remote-dependency", source_tree=source.root)
        pin = repositories.commit(remote, message="Pin the audit dependency").sha
        url = "https://github.com/test/remote-dependency.git"
        env = repositories.url_rewrite_subprocess_env(remote, url)
        index = int(env["GIT_CONFIG_COUNT"])
        env["GIT_CONFIG_COUNT"] = str(index + 1)
        env[f"GIT_CONFIG_KEY_{index}"] = f"url.{remote.file_url}/.insteadOf"
        env[f"GIT_CONFIG_VALUE_{index}"] = "git@github.com:test/remote-dependency.git"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        dependency = {"git": url, "ref": pin}
        gh = shutil.which("gh", path=env.get("PATH"))
        if gh is not None:
            # Initialize gh's device state before taking read-only audit snapshots.
            auth = subprocess.run(
                (gh, "auth", "token", "--hostname", "github.com"),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            auth_summary = {
                "returncode": auth.returncode,
                "stdout_length": len(auth.stdout),
                "stderr_length": len(auth.stderr),
            }
            assert auth_summary["returncode"] == 1 and auth_summary["stdout_length"] == 0, (
                auth_summary
            )
    consumer = packages.create(
        "consumer-source",
        dependencies=(dependency,),
        targets=("copilot",),
    )
    (consumer.root / ".gitignore").write_text("apm_modules/\n", encoding="utf-8")
    baseline = repositories.create("baseline", source_tree=consumer.root)
    runner = ApmLifecycleRunner((str(apm_binary_path),))
    runner.run_sequence(
        (("install", "--target", "copilot", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="workspace-identity-baseline-install",
        cwd=baseline.worktree,
        env=env,
    )
    repositories.commit(baseline, message="Commit CLI-generated audit baseline")
    root = isolated.package_root / "Fresh Consumer Long Name"
    _git(isolated.package_root, env, "clone", str(baseline.origin), str(root))
    tracked = set(_git(root, env, "ls-files").splitlines())
    assert {"apm.yml", "apm.lock.yaml", ".gitignore"} <= tracked
    assert any(path.startswith(".github/instructions/") for path in tracked)
    assert not any(path.startswith("apm_modules/") for path in tracked)
    assert not (root / "apm_modules").exists()
    if dependency_kind == "git":
        (remote.worktree / ".apm" / "instructions" / "dependency.instructions.md").write_text(
            "---\napplyTo: '**'\n---\n# Changed after the pin\nMust not be replayed.\n",
            encoding="utf-8",
        )
        assert repositories.commit(remote, message="Advance beyond the pin").sha != pin
    return _Workspace(isolated, root, source.root, runner, env)


def _audit(workspace: _Workspace, root: Path, phase: str) -> tuple[CommandResult, dict]:
    """Observe live bytes independently of the product's own read-only guard."""
    before = ArtifactSnapshot.capture(workspace.isolated.root)
    result = workspace.runner.run(
        _AUDIT,
        scenario_id=phase,
        cwd=root,
        env=workspace.env,
    )
    print(f"{phase}: returncode={result.returncode}\n{result.stdout}\n{result.stderr}")
    assert_unchanged(before, ArtifactSnapshot.capture(workspace.isolated.root))
    payload = json.loads(result.stdout)
    return result, payload


def _assert_clean(result: CommandResult, payload: dict) -> None:
    """Require meaningful replay checks, not a pass through a skipped branch."""
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert payload["passed"] is True
    checks = {check["name"]: check for check in payload["checks"]}
    assert {
        "config-consistency",
        "drift",
        "content-integrity",
        "deployed-files-present",
    } <= checks.keys()
    assert all(check["passed"] for check in checks.values()), checks
    assert checks["drift"]["message"] == "no drift detected against lockfile"
    assert payload["drift"]["drift"] == []
    assert "apm install --global" not in result.stdout + result.stderr


@pytest.mark.parametrize("dependency_kind", ("local", "git"))
def test_fresh_clone_audit_preserves_identity_across_cold_repeated_and_warm_runs(
    tmp_path: Path,
    apm_binary_path: Path,
    dependency_kind: str,
) -> None:
    """One physical clone receives the same real audit under every native spelling."""
    workspace = _seed(tmp_path, apm_binary_path, dependency_kind=dependency_kind)
    spellings = _spellings(workspace.root, workspace.env)
    reference_checks = None
    for repetition in range(2):
        for spelling, root in spellings:
            result, payload = _audit(workspace, root, f"{spelling}-cold-{repetition}")
            _assert_clean(result, payload)
            assert not (workspace.root / "apm_modules").exists()
            if reference_checks is None:
                reference_checks = payload["checks"]
            assert payload["checks"] == reference_checks

    workspace.runner.run_sequence(
        (("install", "--target", "copilot", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="workspace-identity-warm-install",
        cwd=spellings[-1][1],
        env=workspace.env,
    )
    assert (workspace.root / "apm_modules").is_dir()
    warm_checks = None
    for repetition in range(2):
        for spelling, root in spellings:
            result, payload = _audit(workspace, root, f"{spelling}-warm-{repetition}")
            _assert_clean(result, payload)
            assert {check["name"] for check in payload["checks"]} == {
                check["name"] for check in reference_checks
            }
            if warm_checks is None:
                warm_checks = payload["checks"]
            assert payload["checks"] == warm_checks


@pytest.mark.parametrize("damage", ("missing", "modified", "missing-source"))
def test_cold_clone_audit_reports_real_damage_without_writing_live_state(
    tmp_path: Path,
    apm_binary_path: Path,
    damage: str,
) -> None:
    """Native aliases cannot hide missing sources or damaged managed deployments."""
    workspace = _seed(tmp_path, apm_binary_path)
    spellings = _spellings(workspace.root, workspace.env)
    if damage == "missing-source":
        workspace.source.rename(workspace.source.with_name("unavailable-source"))
    else:
        deployed = tuple((workspace.root / ".github" / "instructions").glob("*.md"))
        assert len(deployed) == 1
        if damage == "missing":
            deployed[0].unlink()
        else:
            deployed[0].write_text("Changed managed content.\n", encoding="utf-8")
    reference = None
    for spelling, root in spellings:
        result, payload = _audit(workspace, root, f"{spelling}-{damage}")
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert payload["passed"] is False
        assert "apm install --global" not in result.stdout + result.stderr
        failures = {check["name"] for check in payload["checks"] if not check["passed"]}
        assert "drift" in failures
        if damage == "missing-source":
            assert "local source missing" in result.stdout
            assert "config-consistency" in failures
        else:
            assert payload["drift"]["drift"][0]["kind"] == (
                "unintegrated" if damage == "missing" else "modified"
            )
            assert (
                "deployed-files-present" if damage == "missing" else "content-integrity"
            ) in failures
        if reference is None:
            reference = failures
        assert failures == reference
        assert not (workspace.root / "apm_modules").exists()


def test_genuine_global_metadata_audit_preserves_missing_materialization_refusal(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Actual global installs still require live modules; aliases do not change scope."""
    workspace = _seed(tmp_path, apm_binary_path)
    workspace.runner.run_sequence(
        (("install", "--global", str(workspace.source), "--target", "copilot", "--no-policy"),),
        expected_returncodes=(0,),
        scenario_id="workspace-identity-global-install",
        cwd=workspace.isolated.work_root,
        env=workspace.env,
    )
    global_root = workspace.isolated.config_root
    spellings = _spellings(global_root, workspace.env)
    for spelling, root in spellings:
        result, payload = _audit(workspace, root, f"{spelling}-global-warm")
        _assert_clean(result, payload)
    shutil.rmtree(global_root / "apm_modules")
    for spelling, root in spellings:
        result, payload = _audit(workspace, root, f"{spelling}-global-cold")
        assert result.returncode == 1, (result.stdout, result.stderr)
        checks = {check["name"]: check for check in payload["checks"]}
        assert checks["config-consistency"]["passed"] is False
        assert checks["drift"]["passed"] is False
        assert "apm install --global" in checks["drift"]["message"]
        assert not (global_root / "apm_modules").exists()
