"""Installed-CLI lifecycle: audit verifies a local plugin's skill subset (#4321).

A local Claude plugin without ``apm.yml`` installed with ``--skill`` declares
its edges only in the manifest install synthesizes from it. ``apm audit --ci``
synthesizes that same manifest from the source, never trusting the
``apm_modules`` copy, so the locked selection is verified rather than
reported as unverified; a tampered selection still fails.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I3", "P-OWN-dependency-selection-merge"),
]

_AUDIT = ("audit", "--ci", "--no-policy")


def _output(result: CommandResult) -> str:
    return " ".join((result.stdout + result.stderr).split())


def _plugin(root: Path) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "pkg", "version": "1.0.0"}), encoding="utf-8"
    )
    for name in ("alpha", "beta"):
        skill = root / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}.\n---\n# {name}\n", encoding="utf-8"
        )
    return root


def test_audit_verifies_local_plugin_skill_subset(tmp_path: Path, apm_binary_path: Path) -> None:
    project = tmp_path / "project"
    _plugin(project / "pkg")
    dump_yaml({"name": "consumer", "version": "1.0.0", "targets": ["copilot"]}, project / "apm.yml")
    env = {**os.environ, "HOME": str(tmp_path / "home"), "APM_HOME": str(tmp_path / "apm-home")}
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    install = runner.run(
        ("install", "./pkg", "--skill", "beta", "--no-policy"), cwd=project, env=env
    )
    assert install.returncode == 0, _output(install)
    lock = load_yaml(project / "apm.lock.yaml")
    assert [dep["skill_subset"] for dep in lock["dependencies"]] == [["beta"]]

    warm = runner.run(_AUDIT, cwd=project, env=env)
    assert warm.returncode == 0, _output(warm)
    assert "unverified" not in _output(warm)

    # The apm_modules copy is never trusted: an unreadable edge injected into
    # its normalized manifest changes neither audit nor a frozen install.
    copy_manifest = project / "apm_modules/_local/pkg/apm.yml"
    with copy_manifest.open("a", encoding="utf-8") as handle:
        handle.write("dependencies:\n  apm:\n    - path: ../../../ghost\n")
    tampered_copy = runner.run(_AUDIT, cwd=project, env=env)
    assert tampered_copy.returncode == 0, _output(tampered_copy)
    frozen = runner.run(("install", "--frozen", "--no-policy"), cwd=project, env=env)
    assert frozen.returncode == 0, _output(frozen)
    assert "ghost" not in copy_manifest.read_text(encoding="utf-8")
    assert not (project / "pkg/apm.yml").exists()

    locked_bytes = (project / "apm.lock.yaml").read_bytes()
    lock["dependencies"][0]["skill_subset"] = ["alpha"]
    dump_yaml(lock, project / "apm.lock.yaml")
    tampered = runner.run(_AUDIT, cwd=project, env=env)
    assert tampered.returncode == 1, _output(tampered)
    assert "declared skills ['beta'] != lockfile skill_subset ['alpha']" in _output(tampered)

    (project / "apm.lock.yaml").write_bytes(locked_bytes)
    shutil.rmtree(project / "apm_modules")
    cold = runner.run(_AUDIT, cwd=project, env=env)
    assert cold.returncode == 0, _output(cold)
