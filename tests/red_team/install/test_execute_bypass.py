"""Vector 7 -- `apm lifecycle test --execute` fires through the script gate.

--execute builds its runner with build_runner_from_context, like every
other lifecycle command, so APM_NO_SCRIPTS, org executables.deny_all and
project script trust all apply (#3983). The non-executing preview still
lists untrusted scripts with their trust status.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.lifecycle import lifecycle_test

from .conftest import fire_via_context, touch_cmd, trust, write_project

pytestmark = pytest.mark.component


def test_execute_withholds_untrusted_project_scripts(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--execute never fires an untrusted project script; trusting it does."""
    project = tmp_path / "myrepo"
    sentinel = project / "SENTINEL"
    apm_yml = write_project(project, "post-install", [touch_cmd(sentinel)])

    monkeypatch.chdir(project)
    result = CliRunner().invoke(lifecycle_test, ["post-install", "--execute"])

    assert result.exit_code == 0, result.output
    assert not sentinel.exists(), "--execute ran an untrusted project script"
    assert "apm lifecycle trust" in result.output

    trust(apm_yml)
    result = CliRunner().invoke(lifecycle_test, ["post-install", "--execute"])
    assert result.exit_code == 0, result.output
    assert sentinel.exists(), "--execute must fire a trusted project script"


def test_execute_honours_apm_no_scripts(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APM_NO_SCRIPTS disables --execute even for trusted scripts."""
    project = tmp_path / "myrepo"
    sentinel = project / "SENTINEL"
    trust(write_project(project, "post-install", [touch_cmd(sentinel)]))
    monkeypatch.setenv("APM_NO_SCRIPTS", "1")

    monkeypatch.chdir(project)
    result = CliRunner().invoke(lifecycle_test, ["post-install", "--execute"])

    assert result.exit_code == 0, result.output
    assert not sentinel.exists(), "APM_NO_SCRIPTS must disable --execute"


def test_normal_install_path_does_not_run_untrusted(apm_home: Path, tmp_path: Path) -> None:
    """The SAME untrusted project must be skipped on the install firing path."""
    project = tmp_path / "myrepo"
    sentinel = project / "SENTINEL"
    write_project(project, "post-install", [touch_cmd(sentinel)])

    fire_via_context(project, "post-install")

    assert not sentinel.exists(), (
        "GATE LEAK: install path ran untrusted scripts that only --execute is allowed to run"
    )
