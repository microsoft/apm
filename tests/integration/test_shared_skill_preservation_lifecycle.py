"""Installed CLI regression for separately owned assets in shared skills (#3288)."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet, assert_snapshot_set_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.requires_apm_binary]

_ROOTS = {".claude/skills/s": "claude", ".agents/skills/s": "codex"}


def _skill(owner: str) -> bytes:
    return f"---\nname: s\ndescription: Shared skill fixture\n---\n\n{owner} body.\n".encode()


def _package(root: Path, owner: str) -> Path:
    package = root / owner
    skill = package / "skills" / "s"
    skill.mkdir(parents=True)
    (package / "apm.yml").write_text(f"name: {owner}\nversion: 1.0.0\n", encoding="utf-8")
    (skill / "SKILL.md").write_bytes(_skill(owner))
    (skill / f"{owner}.md").write_bytes(f"{owner} asset\n".encode())
    return package


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nreturncode={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


@dataclass
class _Scenario:
    runner: ApmLifecycleRunner
    environment: IsolatedApmEnvironment
    project: Path
    packages: dict[str, Path]
    user_scope: bool

    @property
    def deploy_root(self) -> Path:
        return self.environment.home if self.user_scope else self.project

    @property
    def lock_root(self) -> Path:
        return self.environment.config_root if self.user_scope else self.project

    def run(self, command: str, *args: str, expected: int = 0) -> CommandResult:
        audit_roots = {"project": self.project, "home": self.environment.home}
        before_audit = ArtifactSnapshotSet.capture(audit_roots) if command == "audit" else None
        result = self.runner.run(
            (
                command,
                *(("-g",) if self.user_scope and command != "audit" else ()),
                *args,
                "--no-policy",
            ),
            scenario_id="shared-skill-preservation",
            cwd=self.lock_root if command == "audit" else self.project,
            env=self.environment.subprocess_env(),
        )
        assert result.returncode == expected, _evidence(result)
        if before_audit is not None:
            assert_snapshot_set_unchanged(before_audit, ArtifactSnapshotSet.capture(audit_roots))
        return result

    def install(self, *owners: str, target: str | None = "claude,codex") -> CommandResult:
        return self.run(
            "install",
            *(str(self.packages[owner]) for owner in owners),
            *(("--target", target) if target else ()),
        )

    def manifest(self, **changes: object) -> None:
        path = self.lock_root / "apm.yml"
        data = load_yaml(path)
        data.update(changes)
        dump_yaml(data, path)

    def assert_state(
        self,
        active: str,
        assets: tuple[str, ...],
        *,
        installed: tuple[str, ...] = ("a", "b"),
        roots: dict[str, str] = _ROOTS,
        body: bytes | None = None,
    ) -> None:
        """Compare independent expected bytes and exact canonical/compatibility projections."""
        expected_files = {
            "SKILL.md": (active, _skill(active) if body is None else body),
            **{f"{name}.md": (name, f"{name} asset\n".encode()) for name in assets},
        }
        paths_by_owner: dict[str, list[str]] = {name: [] for name in installed}
        hashes_by_owner: dict[str, dict[str, str]] = {name: {} for name in installed}
        expected_rows = []
        for root, target in roots.items():
            paths_by_owner[active].append(root)
            active_key = str(self.packages[active])
            expected_rows.append((target, root, (active_key,), active_key, None))
            actual = {
                path.relative_to(self.deploy_root / root).as_posix(): path.read_bytes()
                for path in (self.deploy_root / root).rglob("*")
                if path.is_file()
            }
            assert actual == {name: data for name, (_, data) in expected_files.items()}
            for name, (owner, data) in expected_files.items():
                path = f"{root}/{name}"
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
                paths_by_owner[owner].append(path)
                hashes_by_owner[owner][path] = digest
                key = str(self.packages[owner])
                expected_rows.append((target, path, (key,), key, digest))
        lock = load_yaml(self.lock_root / "apm.lock.yaml")
        for name in installed:
            dependency = next(
                row
                for row in lock["dependencies"]
                if row["repo_url"] == f"_local/{self.packages[name].name}"
            )
            assert dependency.get("deployed_files", []) == sorted(paths_by_owner[name])
            assert dependency.get("deployed_file_hashes", {}) == hashes_by_owner[name]
        assert sorted(
            (
                row["target"],
                row["value"],
                tuple(row["owners"]),
                row["active_owner"],
                row.get("content_hash"),
            )
            for row in lock["deployments"]
        ) == sorted(expected_rows)


def _scenario(tmp_path: Path, apm_binary_path: Path, user_scope: bool) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(tmp_path / "environment", base_env=os.environ)
    project = isolated.work_root
    (project / "apm.yml").write_text(
        "name: shared-skill-consumer\nversion: 1.0.0\ntargets: [claude, codex]\n",
        encoding="utf-8",
    )
    return _Scenario(
        ApmLifecycleRunner((str(apm_binary_path),)),
        isolated,
        project,
        {owner: _package(isolated.package_root, owner) for owner in ("a", "b")},
        user_scope,
    )


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")], ids=["a-b-a", "b-a-b"])
@pytest.mark.parametrize("both_unique", [False, True], ids=["reported", "both-assets"])
def test_reinstall_preserves_other_package_assets(
    tmp_path: Path,
    apm_binary_path: Path,
    user_scope: bool,
    order: tuple[str, str],
    both_unique: bool,
) -> None:
    """Every install preserves unique bytes and records exact active ownership."""
    scenario = _scenario(tmp_path, apm_binary_path, user_scope)
    if not both_unique:
        (scenario.packages[order[0]] / "skills" / "s" / f"{order[0]}.md").unlink()
    installed = []
    for owner in (*order, order[0]):
        scenario.install(owner)
        if owner not in installed:
            installed.append(owner)
        scenario.assert_state(
            owner,
            tuple(name for name in installed if both_unique or name == order[1]),
            installed=tuple(installed),
        )


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")], ids=["a-b", "b-a"])
def test_shared_skill_same_run_and_replays(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool, order: tuple[str, str]
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path, user_scope)
    scenario.install(*order)
    scenario.assert_state("b", ("a", "b"))
    scenario.install(order[0])
    scenario.assert_state(order[0], ("a", "b"))
    before = (scenario.lock_root / "apm.lock.yaml").read_bytes()
    scenario.run("install", "--frozen")
    scenario.assert_state(order[0], ("a", "b"))
    assert (scenario.lock_root / "apm.lock.yaml").read_bytes() == before
    scenario.install(target=None)
    scenario.assert_state("b", ("a", "b"))
    settled = (scenario.lock_root / "apm.lock.yaml").read_bytes()
    scenario.run("audit", "--ci")
    assert (scenario.lock_root / "apm.lock.yaml").read_bytes() == settled


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
def test_shared_skill_source_dependency_and_target_removal(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path, user_scope)
    scenario.install("a", "b")
    scenario.assert_state("b", ("a", "b"))
    (scenario.packages["b"] / "skills" / "s" / "b.md").unlink()
    scenario.install(target=None)
    scenario.assert_state("b", ("a",))
    scenario.manifest(targets=["claude"])
    scenario.install(target=None)
    scenario.assert_state("b", ("a",), roots={".claude/skills/s": "claude"})
    assert not (scenario.deploy_root / ".agents/skills/s").exists()
    scenario.manifest(dependencies={"apm": [str(scenario.packages["a"])]})
    scenario.install(target=None)
    scenario.assert_state("a", ("a",), installed=("a",), roots={".claude/skills/s": "claude"})
    scenario.manifest(dependencies={"apm": []})
    scenario.install(target=None)
    assert not (scenario.deploy_root / ".claude/skills/s").exists()
    lock = load_yaml(scenario.lock_root / "apm.lock.yaml")
    assert not lock.get("deployments")
    assert not lock.get("dependencies")


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
def test_shared_skill_preserves_user_content_and_reports_real_drift(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path, user_scope)
    scenario.install("a", "b")
    before = load_yaml(scenario.lock_root / "apm.lock.yaml")
    for root in _ROOTS:
        (scenario.deploy_root / root / "a.md").write_bytes(b"user edit\n")
        (scenario.deploy_root / root / "personal.md").write_bytes(b"user asset\n")
    scenario.install("a")
    for root in _ROOTS:
        assert (scenario.deploy_root / root / "a.md").read_bytes() == b"user edit\n"
        assert (scenario.deploy_root / root / "personal.md").read_bytes() == b"user asset\n"
    after = load_yaml(scenario.lock_root / "apm.lock.yaml")
    for root in _ROOTS:
        old = next(row for row in before["deployments"] if row["value"] == f"{root}/a.md")
        new = next(row for row in after["deployments"] if row["value"] == f"{root}/a.md")
        assert new == old
        assert not any(row["value"] == f"{root}/personal.md" for row in after["deployments"])
    snapshot = (scenario.lock_root / "apm.lock.yaml").read_bytes()
    audit = scenario.run("audit", "--ci", expected=1)
    assert "content-integrity" in audit.stdout
    assert (scenario.lock_root / "apm.lock.yaml").read_bytes() == snapshot
    scenario.manifest(dependencies={"apm": []})
    scenario.install(target=None)
    for root in _ROOTS:
        assert (scenario.deploy_root / root / "a.md").read_bytes() == b"user edit\n"
        assert (scenario.deploy_root / root / "personal.md").read_bytes() == b"user asset\n"
        assert not (scenario.deploy_root / root / "b.md").exists()
        assert not (scenario.deploy_root / root / "SKILL.md").exists()


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
def test_shared_skill_partial_failure_does_not_claim_sibling_or_unwritten_assets(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path, user_scope)
    scenario.install("a", "b")
    before = load_yaml(scenario.lock_root / "apm.lock.yaml")
    source = scenario.packages["a"] / "skills" / "s"
    (source / "new.md").write_bytes(b"new source asset\n")
    (source / "zz-blocked").mkdir()
    (source / "zz-blocked" / "unwritten.md").write_bytes(b"must not be claimed\n")
    blocked = scenario.deploy_root / ".claude/skills/s/zz-blocked"
    blocked.write_bytes(b"user-owned blocking file\n")
    result = scenario.run(
        "install", str(scenario.packages["a"]), "--target", "claude,codex", expected=1
    )
    assert "Could not" in result.stdout or "failed" in result.stdout
    after = load_yaml(scenario.lock_root / "apm.lock.yaml")
    assert after["deployments"] == before["deployments"]
    for previous, current in zip(before["dependencies"], after["dependencies"], strict=True):
        assert current["deployed_files"] == previous["deployed_files"]
        assert current["deployed_file_hashes"] == previous["deployed_file_hashes"]
    assert (scenario.deploy_root / ".claude/skills/s/new.md").read_bytes() == b"new source asset\n"
    assert not (scenario.deploy_root / ".agents/skills/s/new.md").exists()
    for root in _ROOTS:
        assert (scenario.deploy_root / root / "b.md").read_bytes() == b"b asset\n"
    assert blocked.read_bytes() == b"user-owned blocking file\n"
    assert not (blocked / "unwritten.md").exists()
    assert not any(
        row["value"].endswith(("new.md", "unwritten.md", "zz-blocked"))
        for row in after["deployments"]
    )


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
def test_target_only_install_preserves_unprocessed_shared_root_claim(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path, user_scope)
    scenario.install("a", target="copilot")
    scenario.manifest(targets=["codex", "copilot"])
    before = load_yaml(scenario.lock_root / "apm.lock.yaml")
    old_asset = next(
        row for row in before["deployments"] if row["value"] == ".agents/skills/s/a.md"
    )
    assert old_asset["target"] == "copilot"
    (scenario.packages["a"] / "skills" / "s" / "a.md").unlink()
    changed_body = _skill("updated a")
    (scenario.packages["a"] / "skills" / "s" / "SKILL.md").write_bytes(changed_body)
    scenario.install("a", target="codex")
    assert (scenario.deploy_root / ".agents/skills/s/a.md").read_bytes() == b"a asset\n"
    narrowed = load_yaml(scenario.lock_root / "apm.lock.yaml")
    assert old_asset in narrowed["deployments"]
    changed_hash = "sha256:" + hashlib.sha256(changed_body).hexdigest()
    assert {
        row["content_hash"]
        for row in narrowed["deployments"]
        if row["value"] == ".agents/skills/s/SKILL.md"
    } == {changed_hash}
    assert (
        narrowed["dependencies"][0]["deployed_file_hashes"][".agents/skills/s/SKILL.md"]
        == changed_hash
    )
    scenario.manifest(targets=["codex"])
    scenario.install(target=None)
    assert not (scenario.deploy_root / ".agents/skills/s/a.md").exists()
    scenario.assert_state(
        "a", (), installed=("a",), roots={".agents/skills/s": "codex"}, body=changed_body
    )


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")], ids=["native-plugin", "plugin-native"])
def test_native_skill_and_legacy_plugin_share_only_member_ownership(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool, order: tuple[str, str]
) -> None:
    scenario = _scenario(tmp_path, apm_binary_path, user_scope)
    native = scenario.environment.package_root / "native" / "s"
    native.mkdir(parents=True)
    (native / "SKILL.md").write_bytes(_skill("a"))
    (native / "a.md").write_bytes(b"a asset\n")
    scenario.packages["a"] = native
    plugin = scenario.packages["b"] / ".claude-plugin"
    plugin.mkdir()
    (plugin / "plugin.json").write_text(
        json.dumps({"name": "b", "version": "1.0.0", "skills": ["./skills/s"]}),
        encoding="utf-8",
    )
    installed = []
    for owner in (*order, order[0]):
        scenario.install(owner)
        if owner not in installed:
            installed.append(owner)
        scenario.assert_state(owner, tuple(installed), installed=tuple(installed))
    lock = load_yaml(scenario.lock_root / "apm.lock.yaml")
    assert {row["package_type"] for row in lock["dependencies"]} == {
        "claude_skill",
        "marketplace_plugin",
    }
