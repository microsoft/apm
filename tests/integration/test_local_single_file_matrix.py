"""Installed-CLI local-file lifecycle, admission, and ownership contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration.test_govern_core_contract import (
    _initialize_policy_remote,
    _policy_status_args,
    _write_policy_leaf,
)
from tests.integration.test_public_github_anonymous_lifecycle_e2e import _child_environment
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.git_credential_shim import GitCredentialShimFactory
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_http_server import LocalGitHttpServerFactory
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]

_KINDS = [
    pytest.param("agent", "agents", id="agent"),
    pytest.param("instructions", "instructions", id="instructions"),
    pytest.param("prompt", "prompts", id="prompt"),
]
_INSTALL = ("--target", "copilot", "--no-policy")


def _content(version: str = "initial") -> bytes:
    return (
        "---\ndescription: Local single-file lifecycle\napplyTo: '**'\n"
        f"---\n# Guidance\nUse {version} local guidance.\n"
    ).encode()


@dataclass
class _Scenario:
    isolated: IsolatedApmEnvironment
    factory: LocalPackageFactory
    project: LocalPackage
    runner: ApmLifecycleRunner
    env: dict[str, str]

    def run(self, *args: str, expected: int = 0, cwd: Path | None = None) -> CommandResult:
        """Run the installed candidate without masking command failures."""
        result = self.runner.run(
            args,
            scenario_id="local-single-file-" + "-".join(args[:2]),
            cwd=cwd if cwd is not None else self.project.root,
            env=self.env,
        )
        assert result.returncode == expected, (
            f"{result.command!r}\n{result.stdout}\n{result.stderr}"
        )
        return result


def _scenario(tmp_path: Path, binary: Path) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    factory = LocalPackageFactory(isolated.work_root)
    project = factory.create("consumer", targets=("copilot",))
    return _Scenario(
        isolated, factory, project, ApmLifecycleRunner((str(binary),)), isolated.subprocess_env()
    )


def _source(scenario: _Scenario, kind: str) -> Path:
    source = scenario.project.root / "ai" / f"xyz.{kind}.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(_content())
    source.with_name("companion.txt").write_text("Do not collect me.", encoding="utf-8")
    return source


def _assert_state(
    root: Path, deployment_root: Path, source: Path, reference: str, directory: str, *, user: bool
) -> dict:
    """Check actual bytes and both raw lock projections, not derived owner APIs."""
    target = deployment_root / directory / source.name
    assert target.read_bytes() == source.read_bytes()
    assert not target.is_symlink()
    assert not (deployment_root / directory / "companion.txt").exists()
    lock = load_yaml(root / "apm.lock.yaml")
    assert len(lock["dependencies"]) == 1
    dependency = lock["dependencies"][0]
    assert dependency["source"] == "local"
    assert dependency["local_path"] == reference
    assert dependency["repo_url"] == f"_local/{source.name}"
    assert dependency["package_type"] == "apm_package"
    relative = f"{'.copilot' if user else '.github'}/{directory}/{source.name}"
    digest = f"sha256:{sha256(source.read_bytes()).hexdigest()}"
    assert dependency["deployed_files"] == [relative]
    assert dependency["deployed_file_hashes"] == {relative: digest}
    assert lock["deployments"] == [
        {
            "kind": "project-relative",
            "target": "copilot",
            "value": relative,
            "runtime": None,
            "scope": "user" if user else "project",
            "owners": [reference],
            "active_owner": reference,
            "content_hash": digest,
        }
    ]
    materialized = root / "apm_modules" / "_local" / source.name
    assert (materialized / ".apm" / directory / source.name).read_bytes() == source.read_bytes()
    assert sorted(
        p.relative_to(materialized).as_posix()
        for p in materialized.rglob("*")
        if p.is_file() and p.name != ".apm-pin"
    ) == [f".apm/{directory}/{source.name}", "apm.yml"]
    package_digest = sha256()
    for relative_path in (f".apm/{directory}/{source.name}", "apm.yml"):
        package_digest.update(relative_path.encode("utf-8"))
        package_digest.update((materialized / relative_path).read_bytes())
    assert dependency["content_hash"] == f"sha256:{package_digest.hexdigest()}"
    return lock


def _assert_audit_read_only(scenario: _Scenario, *, user: bool = False) -> None:
    before_project = ArtifactSnapshot.capture(scenario.project.root)
    before_home = ArtifactSnapshot.capture(scenario.isolated.home)
    workspace = scenario.isolated.config_root if user else scenario.project.root
    result = scenario.run("audit", "--ci", "--no-policy", "--format", "json", cwd=workspace)
    report = json.loads(result.stdout)
    assert report["passed"] is True
    assert report["summary"]["failed"] == 0
    assert_unchanged(before_project, ArtifactSnapshot.capture(scenario.project.root))
    assert_unchanged(before_home, ArtifactSnapshot.capture(scenario.isolated.home))


@pytest.mark.parametrize(("kind", "directory"), _KINDS)
@pytest.mark.parametrize("form", ["cli", "string", "path-object"])
def test_project_file_forms_repeat_edit_audit_and_remove(
    tmp_path: Path, apm_binary_path: Path, kind: str, directory: str, form: str
) -> None:
    """All three declaration forms retain normal snapshots and removal ownership."""
    scenario = _scenario(tmp_path, apm_binary_path)
    source = _source(scenario, kind)
    reference = f"./ai/{source.name}"
    declaration = {"path": reference} if form == "path-object" else reference
    if form != "cli":
        scenario.factory.replace_apm_dependencies(scenario.project, (declaration,))
    args = (reference,) if form == "cli" else ()
    scenario.run("install", *args, *_INSTALL)
    manifest = scenario.project.manifest_path.read_bytes()
    assert load_yaml(scenario.project.manifest_path)["dependencies"]["apm"] == [declaration]
    first = _assert_state(
        scenario.project.root,
        scenario.project.root / ".github",
        source,
        reference,
        directory,
        user=False,
    )
    scenario.run("install", *_INSTALL)
    assert scenario.project.manifest_path.read_bytes() == manifest
    assert (
        _assert_state(
            scenario.project.root,
            scenario.project.root / ".github",
            source,
            reference,
            directory,
            user=False,
        )
        == first
    )
    source.write_bytes(_content("edited"))
    scenario.run("install", *_INSTALL)
    _assert_state(
        scenario.project.root,
        scenario.project.root / ".github",
        source,
        reference,
        directory,
        user=False,
    )
    scenario.run("update", "--yes", "--target", "copilot")
    _assert_audit_read_only(scenario)
    user_file = scenario.project.root / ".github" / directory / "user.md"
    user_file.write_bytes(b"User-owned content.\n")
    scenario.run("uninstall", reference)
    assert not (scenario.project.root / ".github" / directory / source.name).exists()
    assert not (scenario.project.root / "apm_modules" / "_local" / source.name).exists()
    assert user_file.read_bytes() == b"User-owned content.\n"
    assert source.read_bytes() == _content("edited")
    assert not load_yaml(scenario.project.manifest_path).get("dependencies", {}).get("apm")
    assert not (scenario.project.root / "apm.lock.yaml").exists()


@pytest.mark.parametrize(("kind", "directory"), _KINDS)
@pytest.mark.parametrize("form", ["absolute", "home-relative"])
def test_user_file_absolute_and_home_relative_lifecycle(
    tmp_path: Path, apm_binary_path: Path, kind: str, directory: str, form: str
) -> None:
    """User scope keeps existing absolute/expanded-home admission restrictions."""
    scenario = _scenario(tmp_path, apm_binary_path)
    source = scenario.isolated.home / f"xyz.{kind}.md"
    source.write_bytes(_content())
    reference = source.as_posix() if form == "absolute" else f"~/{source.name}"
    scenario.run("install", "--global", reference, *_INSTALL)
    root = scenario.isolated.config_root
    deployment = scenario.isolated.home / ".copilot"
    manifest = (root / "apm.yml").read_bytes()
    first = _assert_state(root, deployment, source, reference, directory, user=True)
    scenario.run("install", "--global", *_INSTALL)
    assert _assert_state(root, deployment, source, reference, directory, user=True) == first
    source.write_bytes(_content("edited"))
    scenario.run("update", "--global", "--yes", "--target", "copilot")
    assert (deployment / directory / source.name).read_bytes() == _content()
    assert load_yaml(root / "apm.lock.yaml") == first
    scenario.run("install", "--global", *_INSTALL)
    _assert_state(root, deployment, source, reference, directory, user=True)
    scenario.run("update", "--global", "--yes", "--target", "copilot")
    _assert_state(root, deployment, source, reference, directory, user=True)
    assert (root / "apm.yml").read_bytes() == manifest
    _assert_audit_read_only(scenario, user=True)
    scenario.run("uninstall", "--global", f"_local/{source.name}")
    assert not (deployment / directory / source.name).exists()
    assert not (root / "apm.lock.yaml").exists()
    assert not (root / "apm_modules" / "_local" / source.name).exists()
    assert not load_yaml(root / "apm.yml").get("dependencies", {}).get("apm")
    assert source.read_bytes() == _content("edited")


@pytest.mark.parametrize(
    ("form", "kind"),
    [
        pytest.param("absolute", "agent", id="absolute-agent"),
        pytest.param("home-relative", "agent", id="home-relative-agent"),
        pytest.param("absolute", "instructions", id="absolute-instructions"),
        pytest.param("home-relative", "instructions", id="home-relative-instructions"),
        pytest.param("absolute", "prompt", id="absolute-prompt"),
        pytest.param("home-relative", "prompt", id="home-relative-prompt"),
    ],
)
def test_project_absolute_and_home_file_claude_lifecycle(
    tmp_path: Path, apm_binary_path: Path, form: str, kind: str
) -> None:
    """Project scope renders HOME sources without confusing source and native hashes."""
    directory, description, source_initial, native_relative, native_initial = {
        "agent": (
            "agents",
            "Local agent",
            b"---\ndescription: Local agent\n---\n# Agent\nUse initial guidance.\n",
            ".claude/agents/xyz.md",
            b"---\ndescription: Local agent\n---\n# Agent\nUse initial guidance.\n",
        ),
        "instructions": (
            "instructions",
            "Local rules",
            b"---\ndescription: Local rules\napplyTo: 'src/**'\n"
            b"---\n# Rule\nUse initial guidance.\n",
            ".claude/rules/xyz.md",
            b'---\npaths:\n  - "src/**"\n---\n\n# Rule\nUse initial guidance.\n',
        ),
        "prompt": (
            "prompts",
            "Local command",
            b"---\ndescription: Local command\n---\n# Command\nUse initial guidance.\n",
            ".claude/commands/xyz.md",
            b"---\ndescription: Local command\n---\n\n# Command\nUse initial guidance.",
        ),
    }[kind]
    scenario = _scenario(tmp_path, apm_binary_path)
    scenario.factory.set_targets(scenario.project, ("claude",))
    root = scenario.project.root
    source = scenario.isolated.home / "single-files" / f"xyz.{kind}.md"
    source.parent.mkdir()
    source.write_bytes(source_initial)
    companion = source.with_name("companion.txt")
    companion.write_bytes(b"Source-only companion.\n")
    reference = source.as_posix() if form == "absolute" else f"~/single-files/{source.name}"
    target = root / native_relative
    target.parent.mkdir(parents=True)
    personal = target.with_name("personal.md")
    personal.write_bytes(b"Personal project content.\n")
    user_targets: list[ArtifactSnapshot] = []
    for name in (".claude", ".copilot"):
        user_target = scenario.isolated.home / name
        user_target.mkdir()
        (user_target / "personal.md").write_bytes(b"Personal user content.\n")
        user_targets.append(ArtifactSnapshot.capture(user_target))
    source_before = ArtifactSnapshot.capture(source.parent)
    materialized = root / "apm_modules" / "_local" / source.name
    primitive_relative = f".apm/{directory}/{source.name}"
    install_args = ("--target", "claude", "--no-policy")

    def assert_preserved(source_snapshot: ArtifactSnapshot) -> None:
        """Protect source siblings, personal files, and both user target trees."""
        assert_unchanged(source_snapshot, ArtifactSnapshot.capture(source.parent))
        for before in user_targets:
            assert_unchanged(before, ArtifactSnapshot.capture(before.root))
        assert companion.read_bytes() == b"Source-only companion.\n"
        assert personal.read_bytes() == b"Personal project content.\n"
        assert not (root / ".github").exists()
        assert not (root / ".claude" / "prompts").exists()

    def assert_state(expected_source: bytes, expected_native: bytes) -> dict:
        """Read both ownership projections with literal renderer-independent oracles."""
        manifest = load_yaml(scenario.project.manifest_path)
        assert manifest["targets"] == ["claude"]
        assert manifest["dependencies"]["apm"] == [reference]
        assert source.read_bytes() == expected_source
        assert target.read_bytes() == expected_native
        assert not target.is_symlink()
        if kind != "agent":
            assert expected_native != expected_source
        native_files = {
            path.relative_to(root).as_posix()
            for path in (root / ".claude").rglob("*")
            if path.is_file()
        }
        assert native_files == {native_relative, personal.relative_to(root).as_posix()}
        assert not materialized.is_symlink()
        entries = list(materialized.rglob("*"))
        assert all(not path.is_symlink() for path in entries)
        assert sorted(
            path.relative_to(materialized).as_posix()
            for path in entries
            if path.is_file() and path.name != ".apm-pin"
        ) == [primitive_relative, "apm.yml"]
        assert (materialized / primitive_relative).read_bytes() == expected_source
        assert load_yaml(materialized / "apm.yml") == {
            "name": source.name,
            "version": "1.0.0",
            "description": description,
        }
        package_digest = sha256()
        for relative_path in sorted((primitive_relative, "apm.yml")):
            package_digest.update(relative_path.encode("utf-8"))
            package_digest.update((materialized / relative_path).read_bytes())
        native_digest = f"sha256:{sha256(expected_native).hexdigest()}"
        lock = load_yaml(root / "apm.lock.yaml")
        assert len(lock["dependencies"]) == 1
        dependency = lock["dependencies"][0]
        assert dependency["source"] == "local"
        assert dependency["local_path"] == reference
        assert dependency["repo_url"] == f"_local/{source.name}"
        assert dependency["package_type"] == "apm_package"
        assert dependency["deployed_files"] == [native_relative]
        assert dependency["deployed_file_hashes"] == {native_relative: native_digest}
        assert dependency["content_hash"] == f"sha256:{package_digest.hexdigest()}"
        assert dependency["content_hash"] != native_digest
        assert lock["deployments"] == [
            {
                "kind": "project-relative",
                "target": "claude",
                "value": native_relative,
                "runtime": None,
                "scope": "project",
                "owners": [reference],
                "active_owner": reference,
                "content_hash": native_digest,
            }
        ]
        return lock

    scenario.run("install", reference, *install_args)
    manifest_before = scenario.project.manifest_path.read_bytes()
    first = assert_state(source_initial, native_initial)
    assert_preserved(source_before)
    native_before = ArtifactSnapshot.capture(root / ".claude")

    scenario.run("install", *install_args)
    assert assert_state(source_initial, native_initial) == first
    assert scenario.project.manifest_path.read_bytes() == manifest_before
    assert_unchanged(native_before, ArtifactSnapshot.capture(root / ".claude"))
    assert_preserved(source_before)

    source_edited = source_initial.replace(b"initial", b"edited")
    native_edited = native_initial.replace(b"initial", b"edited")
    source.write_bytes(source_edited)
    source_after_edit = ArtifactSnapshot.capture(source.parent)
    scenario.run("install", *install_args)
    edited = assert_state(source_edited, native_edited)
    assert edited["dependencies"][0]["content_hash"] != first["dependencies"][0]["content_hash"]
    assert (
        edited["dependencies"][0]["deployed_file_hashes"]
        != first["dependencies"][0]["deployed_file_hashes"]
    )
    assert scenario.project.manifest_path.read_bytes() == manifest_before
    assert_preserved(source_after_edit)

    _assert_audit_read_only(scenario)
    assert assert_state(source_edited, native_edited) == edited
    assert scenario.project.manifest_path.read_bytes() == manifest_before
    assert_preserved(source_after_edit)

    scenario.run("uninstall", reference)
    assert not target.exists()
    assert not target.is_symlink()
    assert not materialized.exists()
    assert not materialized.is_symlink()
    assert not (root / "apm.lock.yaml").exists()
    manifest_after = load_yaml(scenario.project.manifest_path)
    assert manifest_after["targets"] == ["claude"]
    assert not manifest_after.get("dependencies", {}).get("apm")
    assert {
        path.relative_to(root).as_posix()
        for path in (root / ".claude").rglob("*")
        if path.is_file()
    } == {personal.relative_to(root).as_posix()}
    assert_preserved(source_after_edit)


@pytest.mark.parametrize(("kind", "directory"), _KINDS)
def test_local_file_policy_denial_preserves_owned_state(
    tmp_path: Path, apm_binary_path: Path, kind: str, directory: str
) -> None:
    """Real cached policy denies a selected file without replacing prior ownership."""
    scenario = _scenario(tmp_path, apm_binary_path)
    assert "APM_POLICY_DISABLE" not in scenario.env
    source = _source(scenario, kind)
    reference = f"./ai/{source.name}"
    root = scenario.project.root
    scenario.factory.replace_apm_dependencies(scenario.project, (reference,))
    _initialize_policy_remote(root, env=scenario.env)

    def prime_policy(*, denied: bool) -> None:
        policy = scenario.factory.write_policy(
            scenario.project,
            {
                "name": "local-file-policy",
                "version": "1",
                "enforcement": "block",
                "fetch_failure": "block",
                "dependencies": {
                    "allow": [reference],
                    "deny": [reference] if denied else [],
                },
            },
        )
        leaf = _write_policy_leaf(root, policy)
        for cold in (True, False):
            if not cold:
                leaf.unlink()
            status = json.loads(scenario.run(*_policy_status_args(cold=cold)).stdout)
            assert status["outcome"] == "found"
            assert status["enforcement"] == "block"
            assert status["cached"] is (not cold)

    prime_policy(denied=False)
    installed = scenario.run("install", "--target", "copilot")
    assert "Policy:" in installed.stdout
    assert "cached" in installed.stdout
    first = _assert_state(root, root / ".github", source, reference, directory, user=False)
    user_file = root / ".github" / directory / "personal.md"
    user_file.write_bytes(b"User-owned content.\n")
    prime_policy(denied=True)
    source.write_bytes(_content("policy-blocked"))
    manifest_before = scenario.project.manifest_path.read_bytes()
    lock_before = (root / "apm.lock.yaml").read_bytes()
    target_before = ArtifactSnapshot.capture(root / ".github")

    result = scenario.run("install", "--target", "copilot", expected=1)

    output = " ".join((result.stdout + result.stderr).split())
    assert "Install blocked by org policy" in output
    assert "denied by pattern" in output
    assert reference in output
    assert scenario.project.manifest_path.read_bytes() == manifest_before
    assert (root / "apm.lock.yaml").read_bytes() == lock_before
    assert load_yaml(root / "apm.lock.yaml") == first
    assert_unchanged(target_before, ArtifactSnapshot.capture(root / ".github"))
    assert (root / ".github" / directory / source.name).read_bytes() == _content()
    assert user_file.read_bytes() == b"User-owned content.\n"
    assert source.read_bytes() == _content("policy-blocked")


@pytest.mark.parametrize(("kind", "directory"), _KINDS)
def test_local_file_scanner_block_preserves_owned_state(
    tmp_path: Path, apm_binary_path: Path, kind: str, directory: str
) -> None:
    """A real selected-file scanner finding blocks before changing native output."""
    scenario = _scenario(tmp_path, apm_binary_path)
    assert "APM_POLICY_DISABLE" not in scenario.env
    source = _source(scenario, kind)
    reference = f"./ai/{source.name}"
    root = scenario.project.root
    scenario.run("install", reference, "--target", "copilot")
    first = _assert_state(root, root / ".github", source, reference, directory, user=False)
    user_file = root / ".github" / directory / "personal.md"
    user_file.write_bytes(b"User-owned content.\n")
    source.write_bytes(_content("scanner-blocked \u202e"))
    manifest_before = scenario.project.manifest_path.read_bytes()
    lock_before = (root / "apm.lock.yaml").read_bytes()
    target_before = ArtifactSnapshot.capture(root / ".github")

    result = scenario.run("install", "--target", "copilot", expected=1)

    output = " ".join((result.stdout + result.stderr).split())
    assert "Blocked:" in output
    assert "contains critical hidden character(s)" in output
    assert f".apm/{directory}/{source.name}" in output
    assert scenario.project.manifest_path.read_bytes() == manifest_before
    assert (root / "apm.lock.yaml").read_bytes() == lock_before
    assert load_yaml(root / "apm.lock.yaml") == first
    assert_unchanged(target_before, ArtifactSnapshot.capture(root / ".github"))
    assert (root / ".github" / directory / source.name).read_bytes() == _content()
    assert user_file.read_bytes() == b"User-owned content.\n"
    assert source.read_bytes() == _content("scanner-blocked \u202e")


@pytest.mark.parametrize(("kind", "directory"), _KINDS)
@pytest.mark.parametrize("failure", ["missing", "unreadable", "invalid-yaml", "invalid-utf8"])
def test_failed_source_preserves_owned_output(
    tmp_path: Path, apm_binary_path: Path, kind: str, directory: str, failure: str
) -> None:
    """Failed input is not successful absence and never authorizes prior cleanup."""
    scenario = _scenario(tmp_path, apm_binary_path)
    source = _source(scenario, kind)
    reference = f"./ai/{source.name}"
    scenario.run("install", reference, *_INSTALL)
    root = scenario.project.root
    lock_before = (root / "apm.lock.yaml").read_bytes()
    manifest_before = scenario.project.manifest_path.read_bytes()
    target_before = ArtifactSnapshot.capture(root / ".github")
    if failure == "missing":
        source.unlink()
        diagnostic = "path does not exist"
    elif failure == "unreadable":
        source.chmod(0)
        diagnostic = "Cannot read local package"
    elif failure == "invalid-yaml":
        source.write_bytes(b"---\ndescription: [broken\n---\n")
        diagnostic = "Invalid primitive content"
    else:
        source.write_bytes(b"\xff\xfe")
        diagnostic = "Invalid primitive content"
    try:
        result = scenario.run("install", *_INSTALL, expected=1)
        assert diagnostic in result.stdout + result.stderr
        assert (root / "apm.lock.yaml").read_bytes() == lock_before
        assert scenario.project.manifest_path.read_bytes() == manifest_before
        assert_unchanged(target_before, ArtifactSnapshot.capture(root / ".github"))
        assert (root / ".github" / directory / source.name).read_bytes() == _content()
    finally:
        if source.exists():
            source.chmod(0o600)


@pytest.mark.parametrize(("kind", "directory"), _KINDS)
def test_direct_file_symlink_retarget_preserves_owned_state(
    tmp_path: Path, apm_binary_path: Path, kind: str, directory: str
) -> None:
    """An explicit CLI re-install cannot follow a retargeted selected leaf."""
    scenario = _scenario(tmp_path, apm_binary_path)
    source = _source(scenario, kind)
    reference = f"./ai/{source.name}"
    scenario.run("install", reference, *_INSTALL)
    root = scenario.project.root
    locked = _assert_state(root, root / ".github", source, reference, directory, user=False)
    personal = root / ".github" / directory / "personal.md"
    personal.write_bytes(b"User-owned content.\n")
    manifest_before = scenario.project.manifest_path.read_bytes()
    lock_before = (root / "apm.lock.yaml").read_bytes()
    native_before = ArtifactSnapshot.capture(root / ".github")
    modules_before = ArtifactSnapshot.capture(root / "apm_modules")
    outside = scenario.isolated.home / f"outside.{kind}.md"
    outside.write_bytes(_content("outside must not be adopted"))
    source.unlink()
    source.symlink_to(outside)

    result = scenario.run("install", reference, *_INSTALL, expected=1)

    diagnostic = " ".join((result.stdout + result.stderr).split())
    assert "Local single-file dependency is a symlink" in diagnostic
    assert scenario.project.manifest_path.read_bytes() == manifest_before
    assert (root / "apm.lock.yaml").read_bytes() == lock_before
    assert load_yaml(root / "apm.lock.yaml") == locked
    assert_unchanged(native_before, ArtifactSnapshot.capture(root / ".github"))
    assert_unchanged(modules_before, ArtifactSnapshot.capture(root / "apm_modules"))
    assert (root / ".github" / directory / source.name).read_bytes() == _content()
    assert personal.read_bytes() == b"User-owned content.\n"
    assert outside.read_bytes() == _content("outside must not be adopted")
    assert source.is_symlink() and source.readlink() == outside
    assert source.with_name("companion.txt").read_bytes() == b"Do not collect me."


@pytest.mark.parametrize(("kind", "directory"), _KINDS)
@pytest.mark.parametrize("escape", ["absolute", "parent-traversal"])
def test_remote_parent_cannot_adopt_local_file_or_erase_prior_ownership(
    tmp_path: Path, apm_binary_path: Path, kind: str, directory: str, escape: str
) -> None:
    """Authenticated acquisition does not authorize consumer-filesystem paths."""
    scenario = _scenario(tmp_path, apm_binary_path)
    source = _source(scenario, kind)
    reference = f"./ai/{source.name}"
    scenario.run("install", reference, *_INSTALL)
    root = scenario.project.root
    locked = _assert_state(root, root / ".github", source, reference, directory, user=False)
    personal = root / ".github" / directory / "personal.md"
    personal.write_bytes(b"User-owned content.\n")
    outside = scenario.isolated.home / f"private.{kind}.md"
    outside.write_bytes(_content("consumer-only bytes"))
    child = outside.as_posix() if escape == "absolute" else f"../../../../../home/{outside.name}"
    remote_root = root / "apm_modules" / "fixture-local-file" / "remote-parent"
    assert (remote_root / child).resolve() == outside.resolve()
    parent = LocalPackageFactory(scenario.isolated.package_root).create(
        "remote-parent", dependencies=({"path": child},), targets=("copilot",)
    )
    repositories = LocalGitRepositoryFactory(scenario.isolated.repository_root, env=scenario.env)
    repository = repositories.create("remote-parent", source_tree=parent.root)
    repositories.commit(repository, message="remote selected-file escape fixture")
    remote = "fixture-local-file/remote-parent"
    scenario.factory.replace_apm_dependencies(scenario.project, (reference, f"{remote}#main"))
    manifest_before = scenario.project.manifest_path.read_bytes()
    native_before = ArtifactSnapshot.capture(root / ".github")
    local_module = root / "apm_modules" / "_local" / source.name
    local_before = ArtifactSnapshot.capture(local_module)
    git = shutil.which("git")
    assert git is not None
    # Keep sibling fixture shims outside the consumer's trusted-executable exclusion root.
    subprocess.run(
        (git, "init", "--quiet", str(root)),
        env=scenario.env,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    token = "fixture-local-file-private-token"
    server_factory = LocalGitHttpServerFactory(
        scenario.isolated.repository_root, real_git=Path(git), env=scenario.env
    )
    with server_factory.start(
        (repository,), private_repositories=(repository,), password=token
    ) as server:
        shim = GitCredentialShimFactory(scenario.isolated.root / "shim").create(
            base_env=scenario.env,
            real_git=Path(git),
            remote_map={remote: server.remote_url(repository)},
            credential="must-not-reach-helper",
            gh_credential=token,
        )
        scenario.env = _child_environment(scenario.isolated, shim, proxy_url=server.proxy_url)
        scenario.env["APM_TEST_LOOPBACK_PORTS"] = str(urlparse(server.proxy_url).port)
        # Existing containment handling omits the unsafe child, not the valid parent.
        result = scenario.run("install", *_INSTALL, "--parallel-downloads", "0")
        assert any(
            observation.accepted and observation.authorization_present
            for observation in server.observations
        ), result.stdout + result.stderr + f"\nshim_events={shim.events()!r}"
    diagnostic = " ".join((result.stdout + result.stderr).split())
    assert "Refusing to install local_path dependency" in diagnostic
    assert "declared by remote package 'remote-parent'" in diagnostic
    if escape == "absolute":
        assert "absolute paths inside remote packages are not allowed" in diagnostic
    else:
        assert "outside" in diagnostic
    assert token not in result.stdout + result.stderr
    assert scenario.project.manifest_path.read_bytes() == manifest_before
    current = load_yaml(root / "apm.lock.yaml")
    assert {row["repo_url"] for row in current["dependencies"]} == {
        f"_local/{source.name}",
        remote,
    }
    assert current["deployments"] == locked["deployments"]
    assert [row for row in current["dependencies"] if row.get("local_path") == reference] == locked[
        "dependencies"
    ]
    assert not any(row.get("local_path") == child for row in current["dependencies"])
    assert_unchanged(native_before, ArtifactSnapshot.capture(root / ".github"))
    assert_unchanged(local_before, ArtifactSnapshot.capture(local_module))
    assert (root / ".github" / directory / source.name).read_bytes() == _content()
    assert personal.read_bytes() == b"User-owned content.\n"
    assert outside.read_bytes() == _content("consumer-only bytes")
    assert source.read_bytes() == _content()
    assert source.with_name("companion.txt").read_bytes() == b"Do not collect me."
    assert not (root / "apm_modules" / "_local" / outside.name).exists()


@pytest.mark.parametrize(("kind", "directory"), _KINDS)
def test_nested_file_anchors_and_redirected_root(
    tmp_path: Path, apm_binary_path: Path, kind: str, directory: str
) -> None:
    """Nested paths resolve against the trusted declaring source, never the write root."""
    scenario = _scenario(tmp_path, apm_binary_path)
    parent = scenario.factory.create(
        "parent", dependencies=(f"./xyz.{kind}.md",), targets=("copilot",)
    )
    source = parent.root / f"xyz.{kind}.md"
    source.write_bytes(_content())
    scenario.factory.replace_apm_dependencies(scenario.project, ("../parent",))
    redirected = scenario.isolated.work_root / "redirected"
    redirected.mkdir()
    scenario.run("install", "--root", str(redirected), *_INSTALL)
    assert (redirected / ".github" / directory / source.name).read_bytes() == _content()
    assert not (scenario.project.root / ".github").exists()
    assert not (parent.root / "apm_modules").exists()
    lock = load_yaml(redirected / "apm.lock.yaml")
    children = [row for row in lock["dependencies"] if row.get("local_path") == f"./{source.name}"]
    assert len(children) == 1
    assert children[0]["deployed_file_hashes"] == {
        f".github/{directory}/{source.name}": f"sha256:{sha256(_content()).hexdigest()}"
    }
    assert source.read_bytes() == _content()


def test_relative_global_and_unsupported_files_are_not_admitted(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path)
    source = _source(scenario, "agent")
    scenario.run("install", "--global", f"./ai/{source.name}", *_INSTALL, expected=1)
    assert not (scenario.isolated.home / ".copilot" / "agents" / source.name).exists()
    unsupported = scenario.project.root / "notes.md"
    unsupported.write_text("# Ordinary Markdown\n", encoding="utf-8")
    manifest_before = scenario.project.manifest_path.read_bytes()
    result = scenario.run("install", "./notes.md", *_INSTALL, expected=1)
    assert "Unsupported single-file dependency" in result.stdout + result.stderr
    assert "directory package" in result.stdout + result.stderr
    assert scenario.project.manifest_path.read_bytes() == manifest_before


def test_declaration_rename_and_same_name_conflict_preserve_identity(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path)
    source = _source(scenario, "instructions")
    scenario.run("install", "./ai/xyz.instructions.md", *_INSTALL)
    renamed = source.with_name("renamed.instructions.md")
    source.rename(renamed)
    scenario.factory.replace_apm_dependencies(scenario.project, ("./ai/renamed.instructions.md",))
    scenario.run("install", *_INSTALL)
    assert not (scenario.project.root / ".github/instructions/xyz.instructions.md").exists()
    _assert_state(
        scenario.project.root,
        scenario.project.root / ".github",
        renamed,
        "./ai/renamed.instructions.md",
        "instructions",
        user=False,
    )
    second = scenario.project.root / "second" / renamed.name
    second.parent.mkdir()
    second.write_bytes(_content("different source"))
    scenario.factory.replace_apm_dependencies(
        scenario.project, ("./ai/renamed.instructions.md", "./second/renamed.instructions.md")
    )
    before = ArtifactSnapshot.capture(scenario.project.root / ".github")
    lock_before = (scenario.project.root / "apm.lock.yaml").read_bytes()
    scenario.run("install", *_INSTALL)
    assert (
        scenario.project.root / ".github/instructions/renamed.instructions.md"
    ).read_bytes() == second.read_bytes()
    lock = load_yaml(scenario.project.root / "apm.lock.yaml")
    dependencies = {row["local_path"]: row for row in lock["dependencies"]}
    assert set(dependencies) == {"./ai/renamed.instructions.md", "./second/renamed.instructions.md"}
    assert (
        dependencies["./ai/renamed.instructions.md"]["content_hash"]
        != dependencies["./second/renamed.instructions.md"]["content_hash"]
    )
    assert not dependencies["./ai/renamed.instructions.md"].get("deployed_files")
    relative = ".github/instructions/renamed.instructions.md"
    digest = f"sha256:{sha256(second.read_bytes()).hexdigest()}"
    assert dependencies["./second/renamed.instructions.md"]["deployed_files"] == [relative]
    assert dependencies["./second/renamed.instructions.md"]["deployed_file_hashes"] == {
        relative: digest
    }
    assert lock["deployments"][0]["owners"] == ["./second/renamed.instructions.md"]
    assert lock["deployments"][0]["active_owner"] == "./second/renamed.instructions.md"
    assert lock["deployments"][0]["content_hash"] == digest
    assert len(lock["deployments"]) == 1
    assert (scenario.project.root / "apm.lock.yaml").read_bytes() != lock_before
    assert before.diff(
        ArtifactSnapshot.capture(scenario.project.root / ".github")
    ).changed == frozenset({"instructions/renamed.instructions.md"})
    assert renamed.read_bytes() != second.read_bytes()
    scenario.factory.replace_apm_dependencies(
        scenario.project,
        (
            {"path": "./ai/renamed.instructions.md", "alias": "first-file"},
            {"path": "./second/renamed.instructions.md", "alias": "second-file"},
        ),
    )
    scenario.run("install", *_INSTALL)
    for alias, original in (("first-file", renamed), ("second-file", second)):
        package = scenario.project.root / "apm_modules" / alias
        assert (package / ".apm/instructions" / original.name).read_bytes() == original.read_bytes()
    assert (
        scenario.project.root / ".github/instructions/renamed.instructions.md"
    ).read_bytes() == second.read_bytes()
    lock = load_yaml(scenario.project.root / "apm.lock.yaml")
    assert {row["local_path"]: row["alias"] for row in lock["dependencies"]} == {
        "./ai/renamed.instructions.md": "first-file",
        "./second/renamed.instructions.md": "second-file",
    }
    for row in lock["dependencies"]:
        materialized = scenario.project.root / "apm_modules" / row["alias"]
        package_digest = sha256()
        for relative_path in (f".apm/instructions/{renamed.name}", "apm.yml"):
            package_digest.update(relative_path.encode())
            package_digest.update((materialized / relative_path).read_bytes())
        assert row["content_hash"] == f"sha256:{package_digest.hexdigest()}"
    _assert_audit_read_only(scenario)
