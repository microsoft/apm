"""Installed-CLI intentional omission and real MCP drift regression contracts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.requires_apm_binary]

_FIREBASE_CONFIG = {
    "command": "npx",
    "args": ["-y", "firebase-tools@15.32.1", "mcp", "--dir", "."],
    "env": {"IS_FIREBASE_MCP": "true"},
}


@dataclass
class _Scenario:
    isolated: IsolatedApmEnvironment
    project: Path
    env: dict[str, str]
    runner: ApmLifecycleRunner
    install_args: list[str]

    def run(self, *args: str) -> None:
        """Require successful execution by the installed CLI."""
        result = self.runner.run(args, cwd=self.project, env=self.env)
        assert result.returncode == 0, result.stdout + result.stderr

    def audit(self, *, passed: bool) -> dict:
        """Observe all durable bytes and metadata independently of the lockfile."""
        roots = {
            "home": self.isolated.home,
            "work": self.isolated.work_root,
            "sources": self.isolated.package_root,
        }
        before = ArtifactSnapshotSet.capture(roots)
        metadata = _metadata(roots)
        report = self.isolated.root / "audit.json"
        result = self.runner.run(
            (
                "audit",
                "--ci",
                "--no-policy",
                "--no-fail-fast",
                "-f",
                "json",
                "--output",
                str(report),
            ),
            cwd=self.project,
            env=self.env,
        )
        assert ArtifactSnapshotSet.capture(roots) == before
        assert _metadata(roots) == metadata
        payload = json.loads(report.read_text(encoding="utf-8"))
        check = next(item for item in payload["checks"] if item["name"] == "config-consistency")
        assert check["passed"] is passed, (check, result.stdout, result.stderr)
        assert result.returncode == (0 if passed else 1), payload
        return check


def _metadata(roots: dict[str, Path]) -> dict[str, tuple[int, int]]:
    """Snapshot mode and mtime in addition to ArtifactSnapshot's exact content."""
    return {
        str(path): (path.lstat().st_mode, path.lstat().st_mtime_ns)
        for root in roots.values()
        for path in root.rglob("*")
    }


def _scenario(tmp_path: Path, apm_binary_path: Path, case: str) -> _Scenario:
    """Mirror the pinned Firebase shape with inert authored skill content."""
    isolated = IsolatedApmEnvironment.create(tmp_path / case, base_env=dict(os.environ))
    env = isolated.subprocess_env()
    source = isolated.package_root / "firebase"
    (source / ".claude-plugin").mkdir(parents=True)
    (source / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "firebase", "version": "1.0.0", "description": "Inert fixture"}),
        encoding="utf-8",
    )
    (source / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"firebase": _FIREBASE_CONFIG}}), encoding="utf-8"
    )
    skill = source / "skills/firebase-hosting-basics"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: firebase-hosting-basics\ndescription: Inert fixture\n---\n# Fixture\n",
        encoding="utf-8",
    )
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)
    repository = repositories.create("firebase", source_tree=source)
    pin = repositories.commit(repository, message="pinned Firebase-shape fixture")
    url = "https://github.com/firebase/agent-skills.git"
    env = repositories.url_rewrite_subprocess_env(repository, url)
    entry = {"git": url, "ref": pin.sha, "skills": ["firebase-hosting-basics"]}
    dependencies: list = [entry]
    if case == "transitive":
        wrapper = LocalPackageFactory(isolated.package_root).create(
            "wrapper", dependencies=(entry,)
        )
        dependencies = [str(wrapper.root)]
    if case == "global":
        factory = LocalPackageFactory(isolated.package_root)
        package = factory.create(
            "skills",
            mcp_dependencies=(
                {
                    "name": "chrome-devtools",
                    "registry": False,
                    "transport": "stdio",
                    "command": "inert-fixture-command",
                },
                {
                    "name": "context7",
                    "registry": False,
                    "transport": "http",
                    "url": "https://mcp.example.invalid/context7",
                },
            ),
        )
        factory.add_skill(
            package, "sample", "---\nname: sample\ndescription: Inert\n---\n# Inert\n"
        )
        dependencies = [str(package.root)]
    project = isolated.config_root if case == "global" else isolated.work_root / "consumer"
    project.mkdir(exist_ok=True)
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["agent-skills"] if case == "global" else ["claude", "copilot"],
            "dependencies": {"apm": dependencies},
        },
        project / "apm.yml",
    )
    args = ["install"]
    if case != "transitive":
        args += ["--only", "apm"]
    if case == "global":
        args += ["--global", "--target", "agent-skills"]
    return _Scenario(isolated, project, env, ApmLifecycleRunner((str(apm_binary_path),)), args)


@pytest.mark.parametrize("case", ["direct", "transitive", "global"])
def test_omitted_mcp_stays_omitted_across_install_audit_and_repeat(
    tmp_path: Path, apm_binary_path: Path, case: str
) -> None:
    """Both public Firebase paths and global skills-only retain explicit non-deployment."""
    scenario = _scenario(tmp_path, apm_binary_path, case)
    native = scenario.project / ".mcp.json"
    native.write_text('{"mcpServers": {}, "userSetting": "keep"}\n', encoding="utf-8")
    native_before = native.read_bytes()
    scenario.run(*scenario.install_args)
    assert native.read_bytes() == native_before
    lock_path = scenario.project / "apm.lock.yaml"
    lock = load_yaml(lock_path)
    expected = {"chrome-devtools", "context7"} if case == "global" else {"firebase"}
    assert set(lock["mcp_omissions"]) == expected
    assert not lock.get("mcp_configs")
    assert not lock.get("mcp_servers")
    scenario.audit(passed=True)
    scenario.run(*scenario.install_args)
    scenario.audit(passed=True)
    assert native.read_bytes() == native_before
    assert load_yaml(lock_path)["mcp_omissions"] == lock["mcp_omissions"]

    for damaged in ({}, {name: {} for name in expected}):
        data = load_yaml(lock_path)
        data["mcp_omissions"] = damaged
        dump_yaml(data, lock_path)
        check = scenario.audit(passed=False)
        assert any("selection is unknown" in detail for detail in check["details"])
        scenario.run(*scenario.install_args)
        scenario.audit(passed=True)
        assert native.read_bytes() == native_before


@pytest.mark.parametrize("root_declared", [False, True])
def test_skills_only_preserves_admitted_baseline_and_real_drift(
    tmp_path: Path, apm_binary_path: Path, root_declared: bool
) -> None:
    """Later skills-only installs cannot erase admitted root or dependency drift."""
    scenario = _scenario(tmp_path, apm_binary_path, "transitive")
    scenario.run(*scenario.install_args)
    scenario.audit(passed=True)
    if root_declared:
        manifest_path = scenario.project / "apm.yml"
        data = load_yaml(manifest_path)
        data["dependencies"]["mcp"] = [
            {
                "name": "root-service",
                "registry": False,
                "transport": "stdio",
                **_FIREBASE_CONFIG,
            }
        ]
        dump_yaml(data, manifest_path)
        scenario.audit(passed=False)
    scenario.run("install", "--trust-transitive-mcp")
    scenario.audit(passed=True)
    lock_path = scenario.project / "apm.lock.yaml"
    baseline = load_yaml(lock_path)
    native = scenario.project / ".mcp.json"
    native_before = native.read_bytes()
    assert "firebase" in baseline["mcp_configs"]
    assert not baseline.get("mcp_omissions")
    scenario.run("install", "--only", "apm")
    scenario.audit(passed=True)
    after = load_yaml(lock_path)
    for field in ("mcp_configs", "mcp_servers", "mcp_config_provenance", "mcp_target_servers"):
        assert after.get(field) == baseline.get(field)
    assert native.read_bytes() == native_before
    frozen_lock = lock_path.read_bytes()
    refusal = scenario.runner.run(
        ("install", "--only", "apm", "--frozen"), cwd=scenario.project, env=scenario.env
    )
    assert refusal.returncode == 1, refusal.stdout + refusal.stderr
    assert "firebase" in refusal.stdout + refusal.stderr
    assert lock_path.read_bytes() == frozen_lock
    assert native.read_bytes() == native_before
    scenario.run("install", "--only", "apm", "--frozen", "--trust-transitive-mcp")
    assert lock_path.read_bytes() == frozen_lock
    assert native.read_bytes() == native_before
    scenario.audit(passed=True)
    source_manifest = (
        scenario.project / "apm.yml"
        if root_declared
        else scenario.project / "apm_modules/firebase/agent-skills/apm.yml"
    )
    original = source_manifest.read_bytes()
    for mutation in ("changed", "removed"):
        data = load_yaml(source_manifest)
        if mutation == "changed":
            data["dependencies"]["mcp"][0]["args"] = ["different"]
        else:
            data["dependencies"].pop("mcp")
        dump_yaml(data, source_manifest)
        check = scenario.audit(passed=False)
        name = "root-service" if root_declared else "firebase"
        assert any(detail.startswith(f"{name}:") for detail in check["details"])
        source_manifest.write_bytes(original)

    if not root_declared:
        source_manifest.unlink()
        check = scenario.audit(passed=False)
        assert any("firebase:" in detail for detail in check["details"])
        source_manifest.write_bytes(original)

    data = load_yaml(lock_path)
    data.pop("mcp_configs")
    dump_yaml(data, lock_path)
    scenario.audit(passed=False)
    assert native.read_bytes() == native_before


def test_direct_mcp_admission_supersedes_prior_omission(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A real direct install replaces omission intent without muting later drift."""
    scenario = _scenario(tmp_path, apm_binary_path, "direct")
    scenario.run(*scenario.install_args)
    lock_path = scenario.project / "apm.lock.yaml"
    assert "firebase" in load_yaml(lock_path)["mcp_omissions"]
    scenario.audit(passed=True)
    scenario.run("install", "--mcp", "firebase", "--", "inert")
    assert "firebase" in load_yaml(lock_path)["mcp_configs"]
    scenario.audit(passed=True)
    scenario.run("install", "--mcp", "firebase", "--", "inert")
    scenario.audit(passed=True)
    manifest_path = scenario.project / "apm.yml"
    manifest = load_yaml(manifest_path)
    server = next(item for item in manifest["dependencies"]["mcp"] if item["name"] == "firebase")
    server["command"] = "different-inert"
    dump_yaml(manifest, manifest_path)
    check = scenario.audit(passed=False)
    assert any(detail.startswith("firebase:") for detail in check["details"])
