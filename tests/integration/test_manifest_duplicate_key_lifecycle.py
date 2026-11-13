"""Installed-CLI contract: a duplicate top-level key in apm.yml is refused.

A merge that leaves two ``dependencies:`` blocks used to make bare
``apm install`` keep only the last block -- dropping the first block's
package and cleaning its deployed files as stale -- while ``apm install
PKG`` and ``apm uninstall`` failed with a raw parser message. Every command
that reads the manifest must now refuse it with one actionable error that
names the key, before any file in the project changes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_COMMANDS = (
    ("install", "--no-policy"),
    ("install", "--no-policy", "./pkgs/gamma"),
    ("uninstall", "./pkgs/alpha"),
    ("install", "--no-policy", "--mcp", "local-echo", "--", "echo"),
    ("update", "--yes"),
    ("lock",),
    ("compile",),
    ("prune",),
    ("approve", "./pkgs/alpha"),
    ("deny", "./pkgs/alpha"),
    ("deps", "list"),
)


def _package(root: Path, name: str) -> None:
    rules = root / ".apm" / "instructions"
    rules.mkdir(parents=True)
    dump_yaml({"name": name, "version": "1.0.0"}, root / "apm.yml")
    (rules / f"{name}.instructions.md").write_text(
        f'---\napplyTo: "**"\n---\n# {name}\n', encoding="ascii"
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


_LAYOUTS = {
    "block": (
        "name: consumer\nversion: 0.1.0\ntarget: copilot\n"
        "dependencies:\n  apm:\n    - ./pkgs/alpha\n",
        "name: consumer\nversion: 0.1.0\ntarget: copilot\n"
        "dependencies:\n  apm:\n    - ./pkgs/alpha\n"
        "dependencies:\n  apm:\n    - ./pkgs/beta\n",
        "declares the top-level key 'dependencies' twice (lines 4 and 7)",
        "Merge both 'dependencies:' blocks into one",
    ),
    "flow": (
        "{name: consumer, version: 0.1.0, target: copilot, dependencies: {apm: [./pkgs/alpha]}}\n",
        "{name: consumer, version: 0.1.0, target: copilot, dependencies: {apm: [./pkgs/alpha]},"
        " dependencies: {apm: [./pkgs/beta]}}\n",
        "declares the top-level key 'dependencies' twice on line 1",
        "Merge both 'dependencies' entries into one",
    ),
}


@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_duplicate_top_level_key_is_refused_before_any_write(
    tmp_path: Path, apm_binary_path: Path, layout: str
) -> None:
    original, duplicated, refusal, remedy = _LAYOUTS[layout]
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    project = isolated.work_root / "consumer"
    for name in ("alpha", "beta", "gamma"):
        _package(project / "pkgs" / name, name)
    manifest = project / "apm.yml"
    manifest.write_text(original, encoding="ascii")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    first = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (project / ".github" / "instructions" / "alpha.instructions.md").is_file()

    # A bad merge leaves a second dependencies entry.
    manifest.write_text(duplicated, encoding="ascii")
    before = _snapshot(project)

    for args in _COMMANDS:
        result = runner.run(args, cwd=project, env=env)
        output = " ".join((result.stdout + result.stderr).split())
        assert result.returncode != 0, f"apm {' '.join(args)} accepted the manifest\n{output}"
        assert refusal in output, output
        assert remedy in output, output
        assert "yaml.dev" not in output and "Traceback" not in output, output
        assert _snapshot(project) == before, f"apm {' '.join(args)} changed the project"
