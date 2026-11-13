"""``apm install --frozen`` fails closed on deployed-hash drift (req-lk-017, #3654).

Two ways the bytes a frozen install leaves on disk can disagree with
``deployed_file_hashes``:

* An APM upgrade renders an already-locked file differently. 0.36.0 renders
  Cursor ``applyTo: "**"`` rules with ``alwaysApply: true`` (#2030) and
  rewrites Codex agent links (#3611); a repository committed with 0.35.1
  output replays to new bytes.
* A user edits a managed skill that the install keeps.

Frozen must exit 1 naming each path with its expected and observed hash,
never write ``apm.lock.yaml``, and agree with ``apm audit --ci``. The repair
(``apm install`` without ``--frozen``) restores a frozen-green state, and
audit's drift text calls a rendering change a rendering change, not a local
edit to export.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._target_compile_kit import Scenario, output, write, write_package
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
]

_REMOTE = "https://github.com/test/c3-pkg.git"
# Byte-exact v0.35.1 renderings of the kit package (verified against the
# released 0.35.1 CLI during the #3649 hunt).
_OLD_RENDERINGS = {
    ".cursor/rules/global.mdc": "---\ndescription: Global rules\nglobs: **\n---\n\nAlways be global.\n",
    ".codex/agents/rev.toml": (
        'name = "rev"\ndescription = "Reviewer"\n'
        'developer_instructions = "Read [skill](../skills/helper/SKILL.md) and '
        '[ext](https://example.com/x.md) and [anchor](#top)."\n'
    ),
}
_REMEDY = "Rerun 'apm install' without --frozen"


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, env=env, check=True, capture_output=True, text=True, timeout=60
    ).stdout


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _emulate_previous_cli_output(project: Path) -> None:
    """Rewrite files and lock hashes to what the 0.35.1 CLI recorded."""
    lock_path = project / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    for relative, text in _OLD_RENDERINGS.items():
        write(project / relative, text)
        digest = _sha(text.encode("ascii"))
        for dependency in lock["dependencies"]:
            if relative in (dependency.get("deployed_file_hashes") or {}):
                dependency["deployed_file_hashes"][relative] = digest
        for deployment in lock.get("deployments") or []:
            if deployment.get("value") == relative:
                deployment["content_hash"] = digest
    lock["apm_version"] = "0.35.1"
    dump_yaml(lock, lock_path)


def _published(scenario: Scenario) -> tuple[str, dict[str, str]]:
    repositories = LocalGitRepositoryFactory(scenario.isolated.repository_root, env=scenario.env)
    source = write_package(scenario.isolated.package_root / "c3-pkg")
    repository = repositories.create("c3-pkg", source_tree=source)
    commit = repositories.commit(repository, message="publish")
    return commit.sha, repositories.url_rewrite_subprocess_env(repository, _REMOTE)


def _project(scenario: Scenario, sha: str, env: dict[str, str], targets: list[str]) -> Path:
    project = scenario.isolated.work_root / "proj"
    project.mkdir()
    dump_yaml(
        {
            "name": "proj",
            "version": "1.0.0",
            "targets": targets,
            "dependencies": {"apm": [{"git": _REMOTE, "ref": sha}]},
        },
        project / "apm.yml",
    )
    write(project / ".gitignore", "apm_modules/\n")
    scenario.run("install", "--no-policy", cwd=project, env=env)
    return project


def test_frozen_replay_of_previous_cli_output_fails_closed_and_names_paths(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = Scenario.create(tmp_path, apm_binary_path)
    sha, env = _published(scenario)
    project = _project(scenario, sha, env, ["copilot", "cursor", "codex"])
    _emulate_previous_cli_output(project)
    _git(project, env, "init", "--initial-branch=main")
    _git(project, env, "add", "--all")
    _git(project, env, "commit", "-m", "deployed by 0.35.1")

    clone = scenario.isolated.work_root / "ci-clone"
    _git(scenario.isolated.work_root, env, "clone", "--quiet", str(project), str(clone))
    for receipts in scenario.isolated.config_root.glob("deployment-receipts*"):
        shutil.rmtree(receipts)
    shutil.rmtree(scenario.isolated.cache_root)
    scenario.isolated.cache_root.mkdir()
    lock_before = (clone / "apm.lock.yaml").read_bytes()

    # Audit alone names the rendering change and does not offer export-patch.
    audit = scenario.run("audit", "--ci", "--no-policy", cwd=clone, env=env, expect=1)
    text = output(audit)
    assert "render" in text and "apm export-patch" not in text, text

    # Frozen fails closed: every re-rendered path is named with both hashes.
    frozen = scenario.run("install", "--frozen", "--no-policy", cwd=clone, env=env, expect=1)
    text = output(frozen)
    assert "do not match the hashes recorded in apm.lock.yaml" in text, text
    for relative, old in _OLD_RENDERINGS.items():
        observed = compute_file_hash(clone / relative)
        assert f"{relative}: expected {_sha(old.encode('ascii'))}, observed {observed}" in text
    assert _REMEDY in text
    assert "Deployed files verified" not in text
    assert (clone / "apm.lock.yaml").read_bytes() == lock_before
    scenario.run("audit", "--ci", "--no-policy", cwd=clone, env=env, expect=1)

    # The documented repair converges: install, then frozen and audit agree.
    scenario.run("install", "--no-policy", cwd=clone, env=env)
    assert (clone / "apm.lock.yaml").read_bytes() != lock_before
    verified = scenario.run("install", "--frozen", "--no-policy", cwd=clone, env=env)
    assert "Deployed files verified against apm.lock.yaml" in output(verified)
    scenario.run("audit", "--ci", "--no-policy", cwd=clone, env=env)


@pytest.mark.parametrize("scope", ["project", "user"])
def test_warm_frozen_with_kept_skill_edit_fails_closed(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    """Project scope and ``-g`` share one deployed-hash verification (#3654)."""
    scenario = Scenario.create(tmp_path, apm_binary_path)
    sha, env = _published(scenario)
    if scope == "user":
        cwd = scenario.isolated.work_root / "anywhere"
        cwd.mkdir()
        flags: tuple[str, ...] = ("-g",)
        scenario.run(
            "install",
            "-g",
            f"{_REMOTE}#{sha}",
            "--target",
            "claude",
            "--no-policy",
            cwd=cwd,
            env=env,
        )
        deploy_root, lockfile = (
            scenario.isolated.home,
            scenario.isolated.config_root / "apm.lock.yaml",
        )
    else:
        cwd = _project(scenario, sha, env, ["claude"])
        flags = ()
        deploy_root, lockfile = cwd, cwd / "apm.lock.yaml"
    skill = deploy_root / ".claude/skills/helper/SKILL.md"
    assert skill.is_file(), skill
    skill.write_bytes(b"Edited skill.\n")
    lock_before = lockfile.read_bytes()

    frozen = scenario.run("install", *flags, "--frozen", "--no-policy", cwd=cwd, env=env, expect=1)
    text = output(frozen)
    assert ".claude/skills/helper/SKILL.md: expected sha256:" in text, text
    assert f"observed {compute_file_hash(skill)}" in text, text
    assert "apm export-patch" in text
    assert skill.read_bytes() == b"Edited skill.\n"
    assert lockfile.read_bytes() == lock_before

    if scope == "project":
        audit = scenario.run("audit", "--ci", "--no-policy", cwd=cwd, env=env, expect=1)
        assert ".claude/skills/helper/SKILL.md" in output(audit)
    # Install keeps edited skills, so the user resolves the edit, then repairs.
    skill.unlink()
    scenario.run("install", *flags, "--no-policy", cwd=cwd, env=env)
    assert skill.is_file() and skill.read_bytes() != b"Edited skill.\n"
    assert lockfile.read_bytes() == lock_before
    scenario.run("install", *flags, "--frozen", "--no-policy", cwd=cwd, env=env)
    if scope == "project":
        scenario.run("audit", "--ci", "--no-policy", cwd=cwd, env=env)
