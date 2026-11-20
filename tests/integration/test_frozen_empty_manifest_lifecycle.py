"""Installed-CLI proof: an empty manifest converges without a lockfile (#4050).

With nothing declared, ``apm install`` writes no lockfile. ``install --frozen``
then replays that converged state as a no-op, agreeing with ``audit --ci``,
and the tree stays byte-identical, at project scope and at user scope.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-LAW-lockfile.frozen_preserves_bytes", "P-INV-I3"),
]

_MANIFEST = "name: empty-consumer\nversion: 1.0.0\ntargets:\n- copilot\ndependencies:\n  apm: []\n"


def _evidence(result: CommandResult) -> str:
    return f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"


def _tree(*roots: Path) -> dict[str, bytes | None]:
    return {
        str(path): (path.read_bytes() if path.is_file() else None)
        for root in roots
        for path in sorted(root.rglob("*"))
        if "cache" not in path.relative_to(root).parts
    }


@pytest.mark.parametrize("scope", ["project", "user"])
def test_empty_manifest_frozen_install_agrees_with_audit(
    tmp_path: Path, apm_binary_path: Path, scope: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / scope, base_env=dict(os.environ))
    env = isolated.subprocess_env()
    if scope == "project":
        workspace = cwd = isolated.work_root / "empty"
        workspace.mkdir()
        scope_args: tuple[str, ...] = ()
    else:
        workspace = tmp_path / "apm-home"
        workspace.mkdir()
        env["APM_HOME"] = str(workspace)
        cwd = isolated.work_root / "elsewhere"
        cwd.mkdir()
        scope_args = ("--global",)
    (workspace / "apm.yml").write_text(_MANIFEST, encoding="utf-8")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    def run(*args: str) -> CommandResult:
        return runner.run(args, cwd=cwd, env=env, scenario_id=f"empty-{scope}-{args[0]}")

    installed = run("install", *scope_args)
    assert installed.returncode == 0, _evidence(installed)
    assert not (workspace / "apm.lock.yaml").exists()
    before = _tree(workspace, cwd, isolated.home)

    frozen = run("install", *scope_args, "--frozen")
    audit = runner.run(
        ("audit", "--ci"),
        cwd=workspace,
        env=env,
        scenario_id=f"empty-{scope}-audit",
    )

    assert audit.returncode == 0, _evidence(audit)
    assert frozen.returncode == 0, _evidence(frozen)
    assert not (workspace / "apm.lock.yaml").exists()
    assert _tree(workspace, cwd, isolated.home) == before
