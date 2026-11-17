"""``apm update -g`` then ``apm audit --ci`` from ``~/.apm`` stays green (#3781).

A user manifest without APM packages (MCP/LSP servers or the workspace's own
``~/.apm`` content) takes update's service-only path, which never creates
``~/.apm/apm_modules``. Audit must not demand package materialization the
lockfile does not record, while a user lockfile that does record packages
still fails closed when ``apm_modules`` is gone. Project scope is unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.e2e, pytest.mark.requires_apm_binary]

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "user_global_update_audit"
_AUDIT = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")
_TARGETS = ("--target", "copilot,claude")
_URL = "https://github.com/test/user-dep.git"
_MISSING = "installed package materialization is missing"


@dataclass(frozen=True)
class _Scene:
    isolated: IsolatedApmEnvironment
    runner: ApmLifecycleRunner
    env: dict[str, str]

    def run(self, *args: str, cwd: Path | None = None) -> CommandResult:
        return self.runner.run(
            args,
            scenario_id="-".join(args[:2]),
            cwd=cwd or self.isolated.work_root,
            env=self.env,
        )

    def ok(self, *args: str, cwd: Path | None = None) -> CommandResult:
        result = self.run(*args, cwd=cwd)
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        return result

    def audit(self, cwd: Path) -> tuple[CommandResult, dict]:
        result = self.run(*_AUDIT, cwd=cwd)
        return result, json.loads(result.stdout)


def _scene(tmp_path: Path, apm_binary_path: Path) -> _Scene:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scene", base_env=os.environ)
    env = isolated.subprocess_env(overrides={"PYTHONDONTWRITEBYTECODE": "1"})
    for variable in ("COPILOT_HOME", "CLAUDE_CONFIG_DIR"):
        env.pop(variable, None)
    return _Scene(isolated, ApmLifecycleRunner((str(apm_binary_path),)), env)


def _with_package(scene: _Scene) -> _Scene:
    """Serve one plain APM package from a local git origin behind a github.com URL."""
    packages = LocalPackageFactory(scene.isolated.package_root)
    source = packages.create("user-dep")
    packages.add_instruction(source, "user-dep", "---\napplyTo: '**'\n---\n# Dep\n")
    repositories = LocalGitRepositoryFactory(scene.isolated.package_root, env=scene.env)
    remote = repositories.create("user-dep-remote", source_tree=source.root)
    repositories.commit(remote, message="Publish user-dep")
    env = repositories.url_rewrite_subprocess_env(remote, _URL)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for variable in ("COPILOT_HOME", "CLAUDE_CONFIG_DIR"):
        env.pop(variable, None)
    return _Scene(scene.isolated, scene.runner, env)


def _seed_release(scene: _Scene, label: str) -> None:
    source = _FIXTURES / label / "home"
    for path in sorted(source.rglob("*")):
        if path.is_file():
            target = scene.isolated.home / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())


def _assert_green(result: CommandResult, payload: dict) -> None:
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert payload["passed"] is True, payload["checks"]
    checks = {check["name"]: check for check in payload["checks"]}
    assert {"skill-subset-consistency", "config-consistency", "drift"} <= checks.keys()
    assert all(check["passed"] for check in checks.values()), checks
    assert _MISSING not in result.stdout + result.stderr


@pytest.mark.parametrize("label", ["v0.35.1", "v0.36.0"])
def test_global_update_then_audit_passes_for_released_user_lockfiles(
    tmp_path: Path, apm_binary_path: Path, label: str
) -> None:
    scene = _scene(tmp_path, apm_binary_path)
    _seed_release(scene, label)
    user_root = scene.isolated.config_root
    assert f"apm_version: {label[1:]}" in (user_root / "apm.lock.yaml").read_text()

    scene.ok("update", "--global", "--yes", *_TARGETS)
    # The service-only update path never materializes packages; none are locked.
    assert not (user_root / "apm_modules").exists()

    before = ArtifactSnapshot.capture(scene.isolated.home)
    _assert_green(*scene.audit(user_root))
    assert_unchanged(before, ArtifactSnapshot.capture(scene.isolated.home))
    assert not (user_root / "apm_modules").exists()

    scene.ok("install", "--global", "--no-policy", *_TARGETS)
    _assert_green(*scene.audit(user_root))


def test_global_audit_still_reports_missing_package_materialization(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    scene = _with_package(_scene(tmp_path, apm_binary_path))
    user_root = scene.isolated.config_root
    (user_root / "apm.yml").write_text(
        f"name: user-profile\nversion: 1.0.0\ndependencies:\n  apm:\n  - {_URL}\n",
        encoding="utf-8",
    )
    for marker in (".copilot", ".claude"):
        (scene.isolated.home / marker).mkdir()
    scene.ok("install", "--global", "--no-policy", *_TARGETS)
    scene.ok("update", "--global", "--yes", *_TARGETS)
    assert (user_root / "apm_modules").is_dir()
    _assert_green(*scene.audit(user_root))

    shutil.rmtree(user_root / "apm_modules")
    result, payload = scene.audit(user_root)

    assert result.returncode == 1, (result.stdout, result.stderr)
    checks = {check["name"]: check for check in payload["checks"]}
    for name in ("skill-subset-consistency", "drift"):
        assert checks[name]["passed"] is False, checks[name]
        assert _MISSING in checks[name]["message"]
        assert "apm install --global" in checks[name]["message"]
    assert not (user_root / "apm_modules").exists()

    scene.ok("update", "--global", "--yes", *_TARGETS)
    _assert_green(*scene.audit(user_root))


@pytest.mark.parametrize("dependencies", ["package", "service-only"])
def test_project_update_then_audit_is_unchanged(
    tmp_path: Path, apm_binary_path: Path, dependencies: str
) -> None:
    scene = _with_package(_scene(tmp_path, apm_binary_path))
    project = scene.isolated.work_root / "project"
    (project / ".github").mkdir(parents=True)
    apm = f"  apm:\n  - {_URL}\n" if dependencies == "package" else "  apm: []\n"
    (project / "apm.yml").write_text(
        "name: project\nversion: 1.0.0\ndependencies:\n"
        + apm
        + "  mcp:\n  - name: local-tool\n    registry: false\n    transport: stdio\n"
        "    command: node\n",
        encoding="utf-8",
    )
    scene.ok("install", "--no-policy", "--target", "copilot", cwd=project)
    scene.ok("update", "--yes", "--target", "copilot", cwd=project)
    _assert_green(*scene.audit(project))

    shutil.rmtree(project / "apm_modules", ignore_errors=True)
    _assert_green(*scene.audit(project))
    assert not (project / "apm_modules").exists()
