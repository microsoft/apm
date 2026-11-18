"""Installed-CLI contract: apm.yml YAML anchors and aliases are refused (#3858).

OpenAPM req-mf-020(b) requires a consumer to reject ``&anchor`` / ``*alias``
in a manifest. Every command that reads apm.yml must refuse it with one
actionable error naming the file and line, before any file in the project
changes.
"""

from __future__ import annotations

import os
import shutil
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
    ("audit", "--ci"),
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


def test_dependency_manifest_anchor_is_refused_before_any_write(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """A dependency's own apm.yml is read by the same owner, so its anchors fail too."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = dict(isolated.subprocess_env())
    env.pop("CI", None)
    project = isolated.work_root / "consumer"
    _package(project / "pkgs" / "alpha", "alpha")
    (project / "pkgs" / "alpha" / "apm.yml").write_text(
        "name: alpha\nx-version: &v 1.0.0\nversion: *v\n", encoding="ascii"
    )
    (project / "apm.yml").write_text(_CLEAN, encoding="ascii")
    before = _snapshot(project)
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    result = runner.run(("install", "--no-policy"), cwd=project, env=env)
    output = " ".join((result.stdout + result.stderr).split())
    assert result.returncode != 0, output
    assert "./pkgs/alpha" in output and "YAML anchor '&v' on line 2" in output, output
    assert "must not use YAML anchors" in output and "Traceback" not in output, output
    assert not (project / ".github" / "instructions" / "alpha.instructions.md").exists()
    assert {key: value for key, value in _snapshot(project).items() if key in before} == before


def test_ci_audit_reports_anchor_on_a_cold_checkout(tmp_path: Path, apm_binary_path: Path) -> None:
    """Without apm_modules/, `apm audit --ci -f json` still renders its report."""
    import json

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
    shutil.rmtree(project / "apm_modules")
    manifest.write_text(_LAYOUTS["alias"][0], encoding="ascii")

    result = runner.run(("audit", "--ci", "-f", "json"), cwd=project, env=env)
    assert result.returncode != 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    failed = {check["name"]: check["message"] for check in report["checks"] if not check["passed"]}
    assert "YAML anchor '&rule' on line 4" in failed.get("manifest-parse", ""), failed
    assert "Traceback" not in result.stdout + result.stderr
