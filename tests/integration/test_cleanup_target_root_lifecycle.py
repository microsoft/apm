"""Installed-CLI proof: cleanup never removes a target root (#4132).

Retiring the last file APM deployed under a target root (``.github/``) must
leave that root in place: it existed before APM ran, and removing it makes
the next install recreate it, so the retiring install is not a fixed point.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-OWN-file-level-deploy-sync-cleanup", "P-LAW-idempotency.byte_stable"),
]

_RULE = "---\napplyTo: '**'\ndescription: Retired rule\n---\nRetire me.\n"


def _evidence(result: CommandResult) -> str:
    return f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


def _tree(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes() if path.is_file() else None)
        for path in sorted(root.rglob("*"))
        if "apm_modules" not in path.relative_to(root).parts
    }


def test_retiring_the_last_deployed_file_keeps_the_target_root(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "roots", base_env=dict(os.environ))
    packages = LocalPackageFactory(isolated.work_root)
    project = packages.create("root-consumer", targets=("copilot",))
    root = project.root
    (root / ".github").mkdir()
    dependencies = LocalPackageFactory(root / "packages")
    dependency = dependencies.create("retired-rules")
    dependencies.add_instruction(dependency, "retired", _RULE)
    packages.replace_apm_dependencies(
        project, ({"path": "./packages/retired-rules", "alias": dependency.name},)
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    env = isolated.subprocess_env()

    def install(label: str) -> CommandResult:
        result = runner.run(("install",), cwd=root, env=env, scenario_id=f"roots-{label}")
        assert result.returncode == 0, _evidence(result)
        return result

    install("deploy")
    assert (root / ".github/instructions/retired.instructions.md").is_file()

    packages.replace_apm_dependencies(project, ())
    before = LifecycleStateSnapshot.capture(root, targets=("copilot",))
    install("retire")
    after = LifecycleStateSnapshot.capture(root, targets=("copilot",))

    assert not (root / ".github/instructions/retired.instructions.md").exists()
    assert (root / ".github").is_dir(), "the pre-existing target root was removed"
    changed = {state.relative_path for state in after.files} ^ {
        state.relative_path for state in before.files
    }
    assert ".github" not in changed

    converged = _tree(root)
    install("repeat")
    assert _tree(root) == converged


def test_user_scope_removes_the_target_root_it_created(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """At user scope, a root APM created goes with its last deployed file."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "user-roots", base_env=dict(os.environ))
    packages = LocalPackageFactory(isolated.work_root)
    dependency = packages.create("user-rules", targets=("claude",))
    packages.add_instruction(dependency, "retired", _RULE)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)
    env = isolated.subprocess_env()
    claude_root = isolated.home / ".claude"
    assert not claude_root.exists()

    installed = runner.run(
        ("install", "-g", str(dependency.root), "--target", "claude"),
        cwd=isolated.work_root,
        env=env,
        scenario_id="user-roots-install",
    )
    assert installed.returncode == 0, _evidence(installed)
    assert any(path.is_file() for path in claude_root.rglob("*")), _evidence(installed)

    removed = runner.run(
        ("uninstall", "-g", str(dependency.root)),
        cwd=isolated.work_root,
        env=env,
        scenario_id="user-roots-uninstall",
    )
    assert removed.returncode == 0, _evidence(removed)
    assert not claude_root.exists(), sorted(p.as_posix() for p in claude_root.rglob("*"))
