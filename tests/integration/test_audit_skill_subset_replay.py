"""Hermetic source-CLI proof of subset audit in a fresh committed checkout."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

_AUDIT = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")


def _checkout_bytes(project: Path) -> dict[str, bytes]:
    """Snapshot every non-git file, including any accidentally created modules."""
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(project).parts
    }


@pytest.mark.parametrize(
    ("scenario", "failed_checks"),
    [
        ("clean", set()),
        ("tampered-deployment", {"content-integrity", "drift"}),
        ("subset-mismatch", {"skill-subset-consistency"}),
        ("invalid-selection", {"skill-subset-consistency", "drift"}),
        (
            "ref-mismatch",
            {"ref-consistency", "skill-subset-consistency", "config-consistency", "drift"},
        ),
    ],
)
def test_fresh_subset_audit_uses_locked_commit_without_checkout_writes(
    tmp_path: Path,
    apm_engine_command: tuple[str, ...],
    scenario: str,
    failed_checks: set[str],
) -> None:
    """A real install/commit/clone audit stays pinned after the remote advances."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    packages = LocalPackageFactory(isolated.package_root)
    dependency = packages.create("subset-tools")
    packages.add_skill(
        dependency, "alpha", "---\nname: alpha\ndescription: Selected skill\n---\n# Alpha\n"
    )
    packages.add_skill(
        dependency, "beta", "---\nname: beta\ndescription: Unselected skill\n---\n# Beta\n"
    )
    repository = repositories.create("subset-tools", source_tree=dependency.root)
    commit = repositories.commit(repository, message="seed selected skill")
    remote = "https://github.com/test/subset-tools"
    environment = repositories.url_rewrite_subprocess_env(repository, remote)
    consumer = packages.create(
        "consumer",
        dependencies=({"git": remote, "ref": commit.sha, "skills": ["alpha"]},),
        targets=("copilot",),
    )
    (consumer.root / ".gitignore").write_text("apm_modules/\n", encoding="utf-8")
    runner = ApmLifecycleRunner(apm_engine_command, timeout_seconds=120)
    runner.run_sequence(
        (("install", "--no-policy"), _AUDIT),
        expected_returncodes=(0, 0),
        scenario_id="subset-warm",
        cwd=consumer.root,
        env=environment,
    )
    consumer_repo = repositories.create("consumer", source_tree=consumer.root)
    repositories.commit(consumer_repo, message="commit generated outputs")
    fresh = isolated.work_root / "fresh"
    subprocess.run(
        ("git", "clone", "--no-local", consumer_repo.file_url, str(fresh)),
        env=environment,
        check=True,
        capture_output=True,
        timeout=30,
    )
    shutil.rmtree(repository.worktree / "skills" / "alpha")
    repositories.commit(repository, message="remove selected skill on latest main")
    shutil.rmtree(isolated.cache_root)
    isolated.cache_root.mkdir()
    assert not (fresh / "apm_modules").exists()
    if scenario == "tampered-deployment":
        deployed = list(fresh.rglob("SKILL.md"))
        assert len(deployed) == 1
        deployed[0].write_text("# Tampered selected skill\n", encoding="utf-8")
    elif scenario in {"subset-mismatch", "invalid-selection", "ref-mismatch"}:
        manifest_path = fresh / "apm.yml"
        manifest = load_yaml(manifest_path)
        declaration = manifest["dependencies"]["apm"][0]
        if scenario == "ref-mismatch":
            declaration["ref"] = "b" * 40
        else:
            declaration["skills"] = ["beta" if scenario == "subset-mismatch" else "nonexistent"]
        dump_yaml(manifest, manifest_path)
        if scenario == "invalid-selection":
            lock_path = fresh / "apm.lock.yaml"
            lock = load_yaml(lock_path)
            lock["dependencies"][0]["skill_subset"] = ["nonexistent"]
            dump_yaml(lock, lock_path)
    before = _checkout_bytes(fresh)

    result = runner.run(_AUDIT, scenario_id="subset-cold", cwd=fresh, env=environment)

    payload = json.loads(result.stdout)
    assert result.returncode == int(bool(failed_checks)), (payload, result.stderr)
    assert payload["passed"] is (not failed_checks)
    assert {check["name"] for check in payload["checks"] if not check["passed"]} == failed_checks
    subset = next(
        check for check in payload["checks"] if check["name"] == "skill-subset-consistency"
    )
    assert subset["passed"] is ("skill-subset-consistency" not in failed_checks)
    if scenario == "clean":
        assert payload["drift"]["drift"] == []
    elif scenario == "tampered-deployment":
        assert {finding["kind"] for finding in payload["drift"]["drift"]} == {"modified"}
    elif scenario == "subset-mismatch":
        assert "manifest skills ['beta'] != lockfile skill_subset ['alpha']" in subset["details"][0]
    elif scenario == "invalid-selection":
        assert (
            "recorded skill subset path(s) not found in package tree: nonexistent"
            in subset["details"][0]
        )
    assert not (fresh / "apm_modules").exists()
    assert _checkout_bytes(fresh) == before
