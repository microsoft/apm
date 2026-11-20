"""Installed-CLI contract: ``apm init`` re-init keeps a valid ``targets:`` (#3857).

Re-running ``apm init --yes`` over a manifest that still uses the singular
``target:`` key carries the declared targets into the new manifest through the
same mapping as the ``target:`` deprecation warning: ``agents`` becomes ``copilot``,
``agy`` becomes ``antigravity``, and a token with no ``targets:`` equivalent
is refused before apm.yml is rewritten.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]


@pytest.mark.parametrize(
    ("target_line", "expected"),
    [
        pytest.param("target: agents", ["copilot"], id="agents-to-copilot"),
        pytest.param("target: agents,claude", ["claude", "copilot"], id="csv-with-alias"),
        pytest.param("target: [agy]", ["antigravity"], id="agy-to-antigravity"),
    ],
)
def test_reinit_writes_targets_apm_accepts(
    tmp_path: Path, apm_binary_path: Path, target_line: str, expected: list[str]
) -> None:
    project, env, runner = _setup(tmp_path, apm_binary_path, target_line)

    init = runner.run(("init", "--yes"), cwd=project, env=env)
    assert init.returncode == 0, init.stdout + init.stderr
    manifest = load_yaml(project / "apm.yml")
    assert manifest.get("targets") == expected, manifest
    assert "target" not in manifest

    install = runner.run(("install", "--no-policy"), cwd=project, env=env)
    assert install.returncode == 0, install.stdout + install.stderr
    if "copilot" in expected:
        assert (project / ".github" / "instructions" / "style.instructions.md").is_file()


def test_reinit_refuses_a_target_with_no_targets_equivalent(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project, env, runner = _setup(tmp_path, apm_binary_path, "target: intellij")
    before = (project / "apm.yml").read_bytes()

    init = runner.run(("init", "--yes"), cwd=project, env=env)
    output = " ".join((init.stdout + init.stderr).split())
    assert init.returncode != 0, output
    assert "Target 'intellij' has no 'targets:' equivalent" in output, output
    assert "apm init --target" in output, output
    assert (project / "apm.yml").read_bytes() == before


def _setup(
    tmp_path: Path, apm_binary_path: Path, target_line: str
) -> tuple[Path, dict[str, str], ApmLifecycleRunner]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = dict(isolated.subprocess_env())
    env.pop("CI", None)
    project = isolated.work_root / "consumer"
    rules = project / ".apm" / "instructions"
    rules.mkdir(parents=True)
    (rules / "style.instructions.md").write_text(
        '---\napplyTo: "**"\n---\nUse tabs.\n', encoding="ascii"
    )
    (project / "apm.yml").write_text(
        f"name: consumer\nversion: 0.1.0\n{target_line}\n", encoding="ascii"
    )
    return project, env, ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)


def test_targets_reports_apm_yml_as_source_of_declared_targets(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """``apm targets`` names apm.yml as the source of targets it declares (#4210)."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = dict(isolated.subprocess_env())
    env.pop("CI", None)
    project = isolated.work_root / "consumer"
    project.mkdir()
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    declared = ["claude", "copilot", "cursor"]

    init = runner.run(("init", "-y", "--target", ",".join(declared)), cwd=project, env=env)
    assert init.returncode == 0, init.stdout + init.stderr
    assert load_yaml(project / "apm.yml").get("targets") == declared
    before = sorted(p.relative_to(project).as_posix() for p in project.rglob("*"))

    table = runner.run(("targets",), cwd=project, env=env)
    assert table.returncode == 0, table.stdout + table.stderr
    assert table.stdout.isascii() and table.stderr.isascii(), table.stdout + table.stderr
    rows = {line.split()[0]: line.split() for line in table.stdout.splitlines()[2:] if line.strip()}
    for name in declared:
        assert rows[name][1:3] == ["active", "apm.yml"], table.stdout

    as_json = runner.run(("targets", "--json"), cwd=project, env=env)
    assert as_json.returncode == 0, as_json.stdout + as_json.stderr
    by_name = {row["target"]: row for row in json.loads(as_json.stdout)}
    for name in declared:
        assert by_name[name]["status"] == "active", by_name[name]
        assert by_name[name]["source"] == "apm.yml", by_name[name]
    assert all(row["source"] is None for row in by_name.values() if row["target"] not in declared)

    # apm targets is read-only.
    assert sorted(p.relative_to(project).as_posix() for p in project.rglob("*")) == before
