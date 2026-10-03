"""Real CLI onboarding transitions preserve author-owned content and install state."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import (
    ArtifactSnapshot,
    ArtifactSnapshotSet,
    assert_only_snapshot_paths_changed,
    assert_snapshot_set_unchanged,
    assert_unchanged,
)
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_SKILL = (
    b"---\r\nname: review\r\ndescription: Review author-provided code\r\n---\r\n"
    b"# Review\r\nKeep this author's formatting unchanged.\r\n"
)
_REFERENCE = "./.claude/skills/review"
_DISCOVER = ("init", "--discover", "--format", "json")
_APPLY = (*_DISCOVER, "--apply", "--yes")
_INSTALL = ("install", "--target", "copilot", "--no-policy")


@dataclass(frozen=True)
class _Onboarding:
    isolated: IsolatedApmEnvironment
    root: Path
    source: Path
    runner: ApmLifecycleRunner

    def run(self, args: tuple[str, ...], phase: str) -> CommandResult:
        result = self.runner.run(
            args,
            scenario_id=f"onboarding-{phase}",
            cwd=self.root,
            env=self.isolated.subprocess_env(),
        )
        assert result.returncode == 0, (
            f"{phase}: {result.command}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        return result

    def artifacts(self) -> ArtifactSnapshotSet:
        return ArtifactSnapshotSet.capture(
            {
                "project": self.root,
                "home": self.isolated.home,
                "cache": self.isolated.cache_root,
            }
        )

    def state(self) -> LifecycleStateSnapshot:
        return LifecycleStateSnapshot.capture(self.root, targets=("copilot", "claude"))


def _onboarding(tmp_path: Path, apm_binary_path: Path) -> _Onboarding:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    consumer = LocalPackageFactory(isolated.work_root).create("consumer", targets=("copilot",))
    # A brownfield project has author content but no APM manifest yet.
    consumer.manifest_path.unlink()
    source = consumer.root / ".claude" / "skills" / "review"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_bytes(_SKILL)
    (source / "resources").mkdir()
    (source / "resources" / "sample.bin").write_bytes(b"\x00\xff\r\npayload")
    (consumer.root / "AGENTS.md").write_text("# User context\nKeep me.\n", encoding="ascii")
    return _Onboarding(
        isolated,
        consumer.root,
        source,
        ApmLifecycleRunner((str(apm_binary_path),)),
    )


def _declared_local_paths(root: Path) -> list[str]:
    manifest = load_yaml(root / "apm.yml")
    references = [
        DependencyReference.parse(entry)
        if isinstance(entry, str)
        else DependencyReference.parse_from_dict(entry)
        for entry in manifest["dependencies"]["apm"]
    ]
    return [reference.local_path for reference in references if reference.is_local]


@pytest.mark.parametrize("existing_manifest", [False, True], ids=["new-manifest", "existing"])
def test_discover_declare_install_rerun_state_machine(
    tmp_path: Path, apm_binary_path: Path, existing_manifest: bool
) -> None:
    """Each command phase has an explicit write set and ownership invariant."""
    onboarding = _onboarding(tmp_path, apm_binary_path)
    if existing_manifest:
        (onboarding.root / "apm.yml").write_text(
            "# Preserve this author's metadata\n"
            "name: consumer\nversion: 1.0.0\ntargets: [copilot]\n"
            "scripts:\n  review: echo unchanged\n"
            "dependencies:\n  apm: []\n",
            encoding="ascii",
        )
    original = onboarding.artifacts()
    source = ArtifactSnapshot.capture(onboarding.source)
    before = onboarding.state()

    result = onboarding.run(_DISCOVER, "discover")
    report = json.loads(result.stdout)
    skill = next(
        finding for finding in report["findings"] if finding["path"] == ".claude/skills/review"
    )
    assert skill["status"] == "supported"
    assert skill["dependency"] == {"path": _REFERENCE}
    assert report["additions"] == [{"path": _REFERENCE}]
    assert_snapshot_set_unchanged(original, onboarding.artifacts())
    assert onboarding.state().semantic_bytes == before.semantic_bytes

    onboarding.run(_APPLY, "declare")
    declared = onboarding.state()
    assert_only_snapshot_paths_changed(
        original,
        onboarding.artifacts(),
        {"project": {"apm.yml"}, "home": {".apm/.apm-lifecycle.lock"}},
    )
    assert _declared_local_paths(onboarding.root) == [_REFERENCE]
    assert declared.lockfile_bytes is None
    assert declared.deployment_records == ()
    if existing_manifest:
        assert declared.manifest_bytes.startswith(b"# Preserve this author's metadata\n")
        assert b"scripts:\n  review: echo unchanged\n" in declared.manifest_bytes
        assert load_yaml(onboarding.root / "apm.yml")["scripts"] == {"review": "echo unchanged"}

    after_declaration = onboarding.artifacts()
    onboarding.run(_APPLY, "repeat-declare")
    assert_snapshot_set_unchanged(after_declaration, onboarding.artifacts())
    assert onboarding.state().manifest_bytes == declared.manifest_bytes

    onboarding.run(_INSTALL, "install")
    installed = onboarding.state()
    assert installed.lockfile_bytes is not None
    assert {
        record.locator.value: record.active_owner for record in installed.deployment_records
    } == {
        ".agents/skills/review": _REFERENCE,
        ".agents/skills/review/SKILL.md": _REFERENCE,
        ".agents/skills/review/resources/sample.bin": _REFERENCE,
    }
    lock = LockFile.read(onboarding.root / "apm.lock.yaml")
    local = [dependency for dependency in lock.dependencies.values() if dependency.local_path]
    assert len(local) == 1
    assert local[0].local_path == _REFERENCE
    staged = local[0].to_dependency_ref().get_install_path(onboarding.root / "apm_modules")
    assert (staged / "SKILL.md").read_bytes() == _SKILL
    assert (staged / "resources" / "sample.bin").read_bytes() == b"\x00\xff\r\npayload"
    deployed = onboarding.root / ".agents" / "skills" / "review"
    assert (deployed / "SKILL.md").read_bytes() == _SKILL
    assert (deployed / "resources" / "sample.bin").read_bytes() == b"\x00\xff\r\npayload"
    assert_unchanged(source, ArtifactSnapshot.capture(onboarding.source))
    assert (onboarding.root / "AGENTS.md").read_bytes() == b"# User context\nKeep me.\n"

    after_install = onboarding.artifacts()
    onboarding.run(_DISCOVER, "rediscover")
    assert_snapshot_set_unchanged(after_install, onboarding.artifacts())
    onboarding.run(_APPLY, "repeat-after-install")
    assert_snapshot_set_unchanged(after_install, onboarding.artifacts())
    repeated = onboarding.state()
    assert repeated.semantic_bytes == installed.semantic_bytes
    assert repeated.lockfile_bytes == installed.lockfile_bytes
    assert repeated.deployment_records == installed.deployment_records
    assert _declared_local_paths(onboarding.root) == [_REFERENCE]

    edited_skill = _SKILL + b"\r\nAuthor's later edit.\r\n"
    (onboarding.source / "SKILL.md").write_bytes(edited_skill)
    after_edit = onboarding.artifacts()
    onboarding.run(_APPLY, "source-edited")
    assert_snapshot_set_unchanged(after_edit, onboarding.artifacts())
    assert (deployed / "SKILL.md").read_bytes() == _SKILL
    assert onboarding.state().lockfile_bytes == installed.lockfile_bytes
    assert onboarding.state().deployment_records == installed.deployment_records
    findings = {finding["path"]: finding for finding in report["findings"]}
    assert "AGENTS.md" in findings, report
    assert findings["AGENTS.md"]["status"] == "unsupported"
    assert findings["AGENTS.md"]["dependency"] is None


def test_onboarding_does_not_grant_install_collision_ownership(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Declaring a package never authorizes replacement of native user content."""
    onboarding = _onboarding(tmp_path, apm_binary_path)
    destination = onboarding.root / ".agents" / "skills" / "review"
    destination.mkdir(parents=True)
    user_skill = _SKILL + b"\nDifferent, user-maintained target content.\n"
    (destination / "SKILL.md").write_bytes(user_skill)
    # Predeclare the intended source so discovery cannot choose the conflicting
    # same-name target skill as a second package.
    (onboarding.root / "apm.yml").write_text(
        "name: consumer\nversion: 1.0.0\ntargets: [copilot]\n"
        f"dependencies:\n  apm:\n    - path: {_REFERENCE}\n",
        encoding="ascii",
    )
    before = onboarding.artifacts()
    result = onboarding.runner.run(
        _APPLY,
        scenario_id="onboarding-collision-apply",
        cwd=onboarding.root,
        env=onboarding.isolated.subprocess_env(),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert_snapshot_set_unchanged(before, onboarding.artifacts())
    source = ArtifactSnapshot.capture(onboarding.source)
    target = ArtifactSnapshot.capture(destination)
    install = onboarding.runner.run(
        _INSTALL,
        scenario_id="onboarding-collision-install",
        cwd=onboarding.root,
        env=onboarding.isolated.subprocess_env(),
    )
    assert install.returncode == 0, install.stdout + install.stderr
    assert (destination / "SKILL.md").read_bytes() == user_skill
    assert_unchanged(target, ArtifactSnapshot.capture(destination))
    assert_unchanged(source, ArtifactSnapshot.capture(onboarding.source))
    assert _declared_local_paths(onboarding.root) == [_REFERENCE]
    assert not any(
        record.locator.value == ".agents/skills/review"
        or record.locator.value.startswith(".agents/skills/review/")
        for record in onboarding.state().deployment_records
    )


def test_discover_rejects_target_selection_without_writes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """Target selection belongs to install, not the read-only inventory."""
    onboarding = _onboarding(tmp_path, apm_binary_path)
    before = onboarding.artifacts()
    result = onboarding.runner.run(
        (*_DISCOVER, "--target", "copilot"),
        scenario_id="onboarding-target-rejected",
        cwd=onboarding.root,
        env=onboarding.isolated.subprocess_env(),
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--discover cannot" in result.stderr
    assert "select targets" in result.stderr
    assert_snapshot_set_unchanged(before, onboarding.artifacts())


def test_global_declaration_reports_matching_install_scope(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """The separate install hint must consume the manifest just declared."""
    onboarding = _onboarding(tmp_path, apm_binary_path)
    source = onboarding.isolated.home / ".claude" / "skills" / "review"
    source.parent.mkdir(parents=True)
    onboarding.source.rename(source)
    before = onboarding.artifacts()
    result = onboarding.run(
        ("init", "--discover", "--global", "--apply", "--yes"), "global-declare"
    )
    assert "Run 'apm install --global' separately." in result.stdout
    assert_only_snapshot_paths_changed(
        before,
        onboarding.artifacts(),
        {"home": {".apm/apm.yml", ".apm/.apm-lifecycle.lock"}},
    )
    assert _declared_local_paths(onboarding.isolated.home / ".apm") == [str(source)]
