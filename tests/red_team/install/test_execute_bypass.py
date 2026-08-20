"""Vector 7 -- `apm lifecycle test --execute` bypasses the trust gate.

Documented design: --execute runs in the developer's OWN repo, so it
intentionally skips the project-trust gate (it calls discover_scripts +
LifecycleScriptRunner directly, NOT build_runner_from_context). This is
NOT a break -- it is opt-in local execution.

The security-relevant assertion is the ASYMMETRY: the normal install
path (build_runner_from_context) must NOT run the same untrusted scripts.
A leak there would be the genuine break.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.lifecycle import lifecycle_test

from .conftest import fire_via_context, touch_cmd, write_project


def test_execute_runs_untrusted_scripts_by_design(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--execute fires untrusted project scripts (documented opt-in)."""
    project = tmp_path / "myrepo"
    sentinel = project / "SENTINEL"
    write_project(project, "post-install", [touch_cmd(sentinel)])

    monkeypatch.chdir(project)
    result = CliRunner().invoke(lifecycle_test, ["post-install", "--execute"])

    assert result.exit_code == 0, result.output
    assert sentinel.exists(), "--execute should run scripts in the user's own repo"


def test_normal_install_path_does_not_run_untrusted(apm_home: Path, tmp_path: Path) -> None:
    """The SAME untrusted project must be skipped on the install firing path."""
    project = tmp_path / "myrepo"
    sentinel = project / "SENTINEL"
    write_project(project, "post-install", [touch_cmd(sentinel)])

    fire_via_context(project, "post-install")

    assert not sentinel.exists(), (
        "GATE LEAK: install path ran untrusted scripts that only --execute is allowed to run"
    )
