"""Installed CLI marketplace declaration/audit consistency, with real local Git."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.e2e, pytest.mark.requires_apm_binary]

_NAME = "audit-skill"
_CATALOG = "audit-catalog"
_REMOTE = "https://git.example.invalid/team/audit-package"
_TAG = "releases/audit-skill@1.2.3"
_SKILL = "---\nname: audit-skill\ndescription: Audit fixture\n---\n# Audit fixture\n"
_AUDIT = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")
_INSTALL = ("install", "--target", "copilot", "--no-policy", "--parallel-downloads", "0")


@dataclass
class _Scenario:
    root: Path
    env: dict[str, str]
    runner: ApmLifecycleRunner
    commit: str
    raw: dict[str, str]
    declaration: dict[str, str]

    def run(self, args: tuple[str, ...], expected: int = 0) -> CommandResult:
        result = self.runner.run(
            args, scenario_id="marketplace-object-audit", cwd=self.root, env=self.env
        )
        assert result.returncode == expected, (
            f"{result.command!r}\n{result.stdout}\n{result.stderr}"
        )
        return result

    def audit(self, expected: int = 0) -> dict:
        before = LifecycleStateSnapshot.capture(self.root, targets=("copilot",))
        result = self.run(_AUDIT, expected)
        after = LifecycleStateSnapshot.capture(self.root, targets=("copilot",))
        assert after.manifest_bytes == before.manifest_bytes
        assert after.lockfile_bytes == before.lockfile_bytes
        assert after.files == before.files
        assert after.deployment_records == before.deployment_records
        report = json.loads(result.stdout)
        if expected == 0:
            drift = next(check for check in report["checks"] if check["name"] == "drift")
            assert drift["passed"] is True
            assert drift["message"] == "no drift detected against lockfile"
        return report


def _scenario(
    root: Path, binary: Path, *, layout: str = "apm-subdir", selection: str | None = None
) -> _Scenario:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("audit-package")
    subdir = "packages/audit-skill" if layout == "apm-subdir" else ""
    source = repository.worktree / subdir
    source.mkdir(parents=True, exist_ok=True)
    if layout.startswith("apm"):
        dump_yaml({"name": _NAME, "version": "9.9.9"}, source / "apm.yml")
    if layout == "skill":
        (source / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    else:
        skill = source / "skills" / _NAME / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(_SKILL, encoding="utf-8")
    if layout == "plugin":
        metadata = source / ".claude-plugin" / "plugin.json"
        metadata.parent.mkdir()
        metadata.write_text(json.dumps({"name": _NAME, "version": "9.9.9"}), encoding="utf-8")
    commit = repositories.commit(repository, message="publish audit fixture")
    repositories.tag(repository, _TAG, commit)
    repositories.tag(repository, "v1.2.3", commit)
    environment = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
    catalog = isolated.work_root / "catalog"
    catalog.mkdir()
    source_spec = {
        "source": "git-subdir" if subdir else "url",
        "url": _REMOTE,
        "tag_pattern": "releases/{name}@{version}",
    }
    if subdir:
        source_spec["path"] = subdir
    (catalog / "marketplace.json").write_text(
        json.dumps({"name": _CATALOG, "plugins": [{"name": _NAME, "source": source_spec}]}),
        encoding="utf-8",
    )
    declaration = {"name": _NAME, "marketplace": _CATALOG}
    if selection:
        declaration["version"] = commit.sha if selection == "sha" else selection
    raw = {"git": _REMOTE}
    if subdir:
        raw["path"] = subdir
    if selection:
        raw["ref"] = (
            commit.sha
            if selection == "sha"
            else _TAG
            if selection in {"^1.0.0", "1.2.3"}
            else selection
        )
    consumer = LocalPackageFactory(isolated.work_root).create(
        "consumer", dependencies=(declaration,), targets=("copilot",)
    )
    scenario = _Scenario(
        consumer.root, environment, ApmLifecycleRunner((str(binary),)), commit.sha, raw, declaration
    )
    scenario.run(("marketplace", "add", str(catalog), "--name", _CATALOG))
    return scenario


@pytest.mark.parametrize(
    ("layout", "selection"),
    [
        ("apm-subdir", None),
        ("apm-subdir", "^1.0.0"),
        ("apm-subdir", "1.2.3"),
        ("apm-subdir", "main"),
        ("apm-subdir", "v1.2.3"),
        ("apm-subdir", "sha"),
        ("apm-root", "^1.0.0"),
        ("skill", "^1.0.0"),
        ("plugin", "^1.0.0"),
    ],
)
def test_marketplace_object_install_audit_repeat_offline(
    tmp_path: Path, apm_binary_path: Path, layout: str, selection: str | None
) -> None:
    """Real installs, raw controls and mixed declarations share locked audit evidence."""
    scenario = _scenario(
        tmp_path / "lifecycle", apm_binary_path, layout=layout, selection=selection
    )
    scenario.run(_INSTALL)
    lock = load_yaml(scenario.root / "apm.lock.yaml")
    dependency = lock["dependencies"][0]
    assert dependency["resolved_commit"] == scenario.commit
    assert dependency["discovered_via"] == _CATALOG
    assert dependency["marketplace_plugin_name"] == _NAME
    if selection in {"^1.0.0", "1.2.3"}:
        assert dependency["version"] == "1.2.3"
        assert dependency["constraint"] == selection
        assert dependency["resolved_tag"] == _TAG
        assert datetime.fromisoformat(dependency["resolved_at"]).tzinfo == timezone.utc
    scenario.audit()
    scenario.run(_INSTALL)
    scenario.audit()

    manifest_path = scenario.root / "apm.yml"
    manifest = load_yaml(manifest_path)
    for declarations in ([scenario.raw], [scenario.declaration, scenario.raw]):
        manifest["dependencies"]["apm"] = declarations
        dump_yaml(manifest, manifest_path)
        scenario.run(_INSTALL)
        scenario.audit()
    manifest["dependencies"]["apm"] = [scenario.declaration]
    dump_yaml(manifest, manifest_path)
    scenario.run(_INSTALL)
    scenario.run(("marketplace", "remove", _CATALOG, "--yes"))
    scenario.audit()


@pytest.mark.parametrize(
    "damage",
    [
        "range",
        "selector-echo",
        "raw-ref",
        "sha",
        "missing-version",
        "missing-tag",
        "tag-ref-disagreement",
        "missing-provenance",
        "ambiguous-provenance",
        "orphan",
        "missing-content",
        "drift",
    ],
)
def test_marketplace_object_negative_evidence(
    tmp_path: Path, apm_binary_path: Path, damage: str
) -> None:
    """Actual locked state mutations fail audit without changing the mutated bytes."""
    scenario = _scenario(tmp_path / "negative", apm_binary_path, selection="^1.0.0")
    scenario.run(_INSTALL)
    scenario.audit()
    lock_path = scenario.root / "apm.lock.yaml"
    manifest_path = scenario.root / "apm.yml"
    lock = load_yaml(lock_path)
    manifest = load_yaml(manifest_path)
    dependency = lock["dependencies"][0]
    if damage == "range":
        manifest["dependencies"]["apm"][0]["version"] = "^2.0.0"
    elif damage == "selector-echo":
        dependency["version"] = "2.0.0"
    elif damage in {"raw-ref", "sha"}:
        manifest["dependencies"]["apm"][0]["version"] = (
            "different-branch" if damage == "raw-ref" else "f" * 40
        )
        dependency["version"] = manifest["dependencies"]["apm"][0]["version"]
    elif damage in {"missing-version", "missing-tag", "missing-provenance"}:
        dependency.pop(
            {
                "missing-version": "version",
                "missing-tag": "resolved_tag",
                "missing-provenance": "discovered_via",
            }[damage]
        )
    elif damage == "tag-ref-disagreement":
        dependency["resolved_ref"] = "other-tag"
    elif damage in {"ambiguous-provenance", "orphan"}:
        extra = copy.deepcopy(dependency)
        extra["repo_url"] = "team/extra"
        if damage == "orphan":
            extra.pop("discovered_via")
            extra.pop("marketplace_plugin_name")
        lock["dependencies"].append(extra)
    else:
        deployed = scenario.root / ".agents" / "skills" / _NAME / "SKILL.md"
        assert deployed.is_file()
        if damage == "missing-content":
            deployed.unlink()
        else:
            deployed.write_text(_SKILL + "\nchanged\n", encoding="utf-8")
    dump_yaml(lock, lock_path)
    dump_yaml(manifest, manifest_path)
    report = scenario.audit(expected=1)
    expected_check = {
        "orphan": "no-orphaned-packages",
        "missing-content": "deployed-files-present",
        "drift": "drift",
    }.get(damage, "ref-consistency")
    failed = {check["name"]: check for check in report["checks"] if not check["passed"]}
    assert expected_check in failed
    if damage in {"ambiguous-provenance", "missing-provenance"}:
        reason = damage.split("-")[0]
        assert f"{reason} marketplace provenance" in json.dumps(failed[expected_check])
