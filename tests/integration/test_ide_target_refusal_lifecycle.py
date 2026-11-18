"""Installed-CLI contract: apm.yml never records an IDE as a target (#3878).

``--target intellij`` used to be recorded as ``copilot`` by ``apm init`` and
the ``apm install <pkg>`` bootstrap (and as ``intellij`` by the global MCP
bootstrap, #3870). Every path that writes apm.yml now refuses before writing,
with the guidance to use ``copilot``. An existing manifest is never rewritten,
so ``--target intellij`` still applies to that one install.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_GUIDANCE = (
    "'intellij' is an IDE, not a target, so apm.yml cannot record it. "
    "Use '--target copilot' for GitHub Copilot in JetBrains IDEs; "
    "once apm.yml exists, '--target intellij' writes JetBrains MCP config."
)


def _project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = dict(isolated.subprocess_env())
    env.pop("CI", None)
    project = isolated.work_root / "consumer"
    rules = project / "pkgs" / "alpha" / ".apm" / "instructions"
    rules.mkdir(parents=True)
    dump_yaml({"name": "alpha", "version": "1.0.0"}, project / "pkgs" / "alpha" / "apm.yml")
    (rules / "alpha.instructions.md").write_text(
        '---\napplyTo: "**"\n---\n# alpha\n', encoding="ascii"
    )
    return project, env


def _tree(root: Path) -> dict[str, bytes]:
    # The lifecycle lock (taken before any command body runs) and the network
    # guard bytecode cache are harness state, not project or manifest state;
    # skip them.
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".apm-lifecycle.lock" and "__pycache__" not in path.parts
    }


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(
            ("install", "--no-policy", "--target", "intellij", "./pkgs/alpha"), id="install"
        ),
        pytest.param(
            ("install", "--no-policy", "--target", "claude,intellij", "./pkgs/alpha"),
            id="install-mixed",
        ),
        pytest.param(
            ("install", "--no-policy", "--dry-run", "--target", "intellij", "./pkgs/alpha"),
            id="install-dry-run",
        ),
        pytest.param(
            ("install", "-g", "--no-policy", "--target", "intellij", "acme/never-fetched"),
            id="install-global",
        ),
        pytest.param(
            ("install", "--no-policy", "--root", "out", "--target", "intellij", "./pkgs/alpha"),
            id="install-root",
        ),
        pytest.param(("init", "--yes", "--target", "intellij"), id="init"),
        pytest.param(("init", "--yes", "--target", "all,intellij"), id="init-mixed"),
        pytest.param(("init", "fresh", "--yes", "--target", "intellij"), id="init-named"),
    ],
)
def test_new_manifest_refuses_an_ide_target(
    tmp_path: Path, apm_binary_path: Path, args: tuple[str, ...]
) -> None:
    project, env = _project(tmp_path)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    root = tmp_path / "isolated"
    before = _tree(root)

    result = runner.run(args, cwd=project, env=env)
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode == 1, output
    assert _GUIDANCE in output, output
    assert "Nothing was written" in output, output
    assert _tree(root) == before
    assert sorted(p.name for p in project.iterdir()) == ["pkgs"]


def test_runtime_flag_bootstrap_never_records_an_ide(tmp_path: Path, apm_binary_path: Path) -> None:
    project, env = _project(tmp_path)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    result = runner.run(
        ("install", "--no-policy", "--runtime", "intellij", "./pkgs/alpha"), cwd=project, env=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = load_yaml(project / "apm.yml")
    assert "targets" not in manifest
    assert "intellij" not in (project / "apm.yml").read_text(encoding="utf-8")


def test_existing_manifest_is_not_rewritten_for_an_ide_target(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project, env = _project(tmp_path)
    (project / "apm.yml").write_text("name: consumer\nversion: 0.1.0\n", encoding="ascii")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    result = runner.run(
        ("install", "--no-policy", "--target", "intellij", "./pkgs/alpha"), cwd=project, env=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = load_yaml(project / "apm.yml")
    assert "targets" not in manifest
    assert manifest["dependencies"]["apm"] == ["./pkgs/alpha"]
