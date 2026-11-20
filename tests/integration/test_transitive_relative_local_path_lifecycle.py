"""APMLifecycle: a transitive relative local dependency installs at every scope (#4153).

PROMISE: outcome.status_matches_state -- a relative ``local_path``
declared by a dependency anchors on the DECLARING package, so its transitive
closure installs the same at project and user scope; a transitive dependency
that cannot be resolved fails the install loudly (source: issue #4153).
CELL: OP=install SRC=local PRIM=skill SCOPE={project,user} LOCK=absent
owner: dependency-identity-materialization (deps/path_anchoring.py local_dep_base_dir)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-dependency-identity-materialization"),
]

_TARGET = "copilot"


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _assert_output_oracles(result: CommandResult) -> None:
    """I6 ASCII and I9 no duplicate status lines per stream."""
    for stream in (result.stdout, result.stderr):
        assert stream.isascii(), _evidence(result)
        lines = [line.strip() for line in stream.splitlines() if line.lstrip().startswith("[")]
        assert len(lines) == len(set(lines)), _evidence(result)


def _package(root: Path, name: str, deps: tuple[str, ...] = ()) -> Path:
    package = root / name
    skill = package / ".apm" / "skills" / name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n{name}\n", "utf-8")
    manifest: dict[str, object] = {"name": name, "version": "1.0.0"}
    if deps:
        manifest["dependencies"] = {"apm": [{"path": dep} for dep in deps]}
    (package / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return package


class _World:
    def __init__(self, tmp_path: Path, apm_binary_path: Path, scope: str) -> None:
        isolated = IsolatedApmEnvironment.create(tmp_path.resolve() / "world", base_env=os.environ)
        self.scope = scope
        self.home = isolated.home
        # User scope keeps APM_HOME outside HOME so no anchor can lean on HOME.
        self.apm_home = isolated.root / "apm-home"
        self.apm_home.mkdir()
        self.env = isolated.subprocess_env()
        self.env.update(APM_HOME=str(self.apm_home), APM_NO_CACHE="1", COLUMNS="400")
        for variable in ("COPILOT_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            self.env.pop(variable, None)
        self.packages = isolated.package_root
        self.cwd = isolated.work_root
        self.project = isolated.work_root / "project"
        self.project.mkdir()
        if scope == "project":
            (self.project / "apm.yml").write_text(
                yaml.safe_dump({"name": "project", "version": "1.0.0", "targets": [_TARGET]}),
                encoding="utf-8",
            )
        self.runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    @property
    def deploy_root(self) -> Path:
        return self.project if self.scope == "project" else self.home

    @property
    def manifest_root(self) -> Path:
        return self.project if self.scope == "project" else self.apm_home

    def apm(self, *args: str) -> CommandResult:
        argv = (*args, "-g", "--target", _TARGET) if self.scope == "user" else args
        cwd = self.project if self.scope == "project" else self.cwd
        result = self.runner.run(argv, cwd=cwd, env=self.env)
        _assert_output_oracles(result)
        return result

    def locked(self) -> dict[str, dict]:
        lock = yaml.safe_load((self.manifest_root / "apm.lock.yaml").read_text(encoding="utf-8"))
        return {row["name"]: row for row in lock.get("dependencies") or []}

    def snapshot(self) -> tuple[ArtifactSnapshot, ArtifactSnapshot]:
        return ArtifactSnapshot.capture(self.deploy_root), ArtifactSnapshot.capture(
            self.manifest_root
        )


@pytest.mark.parametrize("scope", ["project", "user"])
def test_transitive_relative_local_paths_anchor_on_the_declaring_package(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    world = _World(tmp_path, apm_binary_path, scope)
    _package(world.packages, "p-child")
    parent = _package(world.packages, "p-parent", ("../p-child",))
    parent2 = _package(world.packages, "p-parent2", ("../p-child", "./p-leaf"))
    _package(parent2, "p-leaf")

    # WHEN: the diamond installs; both parents reach p-child through ../p-child.
    installed = world.apm("install", str(parent), str(parent2))
    assert installed.returncode == 0, _evidence(installed)

    # THEN: every transitive relative dependency is deployed and locked (I1).
    locked = world.locked()
    assert {"p-parent", "p-parent2", "p-child", "p-leaf"} <= set(locked), (
        sorted(locked),
        _evidence(installed),
    )
    for name in ("p-parent", "p-parent2", "p-child", "p-leaf"):
        skill = world.deploy_root / ".agents" / "skills" / name / "SKILL.md"
        assert skill.is_file(), f"{name} not deployed at {scope} scope\n{_evidence(installed)}"
        assert f".agents/skills/{name}/SKILL.md" in locked[name]["deployed_files"]
    assert "Skipping" not in installed.stdout, _evidence(installed)

    # I2: the converged install changes no bytes.
    before = world.snapshot()
    again = world.apm("install")
    assert again.returncode == 0, _evidence(again)
    after = world.snapshot()
    assert_unchanged(before[0], after[0])
    assert_unchanged(before[1], after[1])

    # I3: audit agrees with the deployed state (user scope audits from APM_HOME).
    audit = world.runner.run(("audit", "--ci"), cwd=world.manifest_root, env=world.env)
    assert audit.returncode == 0, _evidence(audit)


@pytest.mark.parametrize("scope", ["project", "user"])
def test_unresolvable_transitive_local_path_fails_the_install(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    world = _World(tmp_path, apm_binary_path, scope)
    broken = _package(world.packages, "p-broken", ("../p-absent",))

    result = world.apm("install", str(broken))

    # I7 + I8: never a silent drop; the missing source is named and exit is 1.
    assert result.returncode == 1, _evidence(result)
    output = result.stdout + result.stderr
    assert "../p-absent" in output, _evidence(result)
    assert not (world.deploy_root / ".agents" / "skills" / "p-absent").exists()
