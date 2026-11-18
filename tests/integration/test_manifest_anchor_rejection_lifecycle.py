"""Installed-CLI contract: apm.yml YAML anchors and aliases are refused (#3858).

OpenAPM req-mf-020(b) requires a consumer to reject ``&anchor`` / ``*alias``
in a manifest. Every command that reads apm.yml must refuse it with one
actionable error naming the file and line, before any file in the project
changes.
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
    ("compile",),
    ("audit",),
    ("manifest", "migrate", "--yes"),
)

_CLEAN = (
    "name: consumer\nversion: 0.1.0\ntargets: [copilot]\n"
    "dependencies:\n  apm:\n    - ./pkgs/alpha\n"
)

_LAYOUTS = {
    "alias": (
        "name: consumer\nversion: 0.1.0\n"
        "x-shared:\n  rule: &rule ./pkgs/alpha\n"
        "targets: [copilot]\n"
        "dependencies:\n  apm:\n    - *rule\n",
        "YAML anchor '&rule' on line 4",
    ),
    "anchored-target-list": (
        "name: consumer\nversion: 0.1.0\n"
        "target: &t [copilot]\n"
        "dependencies:\n  apm:\n    - ./pkgs/alpha\n",
        "YAML anchor '&t' on line 3",
    ),
}


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


@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
def test_manifest_anchor_is_refused_before_any_write(
    tmp_path: Path, apm_binary_path: Path, layout: str
) -> None:
    anchored, refusal = _LAYOUTS[layout]
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = dict(isolated.subprocess_env())
    env.pop("CI", None)
    project = isolated.work_root / "consumer"
    _package(project / "pkgs" / "alpha", "alpha")
    manifest = project / "apm.yml"
    manifest.write_text(_CLEAN, encoding="ascii")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    first = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (project / ".github" / "instructions" / "alpha.instructions.md").is_file()

    manifest.write_text(anchored, encoding="ascii")
    before = _snapshot(project)

    for args in _COMMANDS:
        result = runner.run(args, cwd=project, env=env)
        output = " ".join((result.stdout + result.stderr).split())
        assert result.returncode != 0, f"apm {' '.join(args)} accepted the manifest\n{output}"
        assert "apm.yml" in output and refusal in output, output
        assert "must not use YAML anchors" in output, output
        assert "Traceback" not in output, output
        assert _snapshot(project) == before, f"apm {' '.join(args)} changed the project"

    # Writing the value out in full makes the same project install again.
    manifest.write_text(_CLEAN, encoding="ascii")
    again = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert again.returncode == 0, again.stdout + again.stderr
