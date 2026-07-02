"""Vector 1 -- end-to-end trust gate on the REAL firing path.

Builds an on-disk project (apm.yml lifecycle: post-install command that
writes a SENTINEL), then fires through build_runner_from_context exactly
as InstallService.run does. These are regression traps for the supply-
chain gate: a sentinel that appears when it should not is a GATE LEAK.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import fire_via_context, touch_cmd, trust, write_project


def test_untrusted_clone_does_not_run_scripts(apm_home: Path, tmp_path: Path) -> None:
    """Untrusted project -> post-install must be skipped (no sentinel)."""
    project = tmp_path / "clone"
    sentinel = project / "SENTINEL"
    write_project(project, "post-install", [touch_cmd(sentinel)])

    fire_via_context(project, "post-install")

    assert not sentinel.exists(), "GATE LEAK: untrusted project script executed"


def test_untrusted_clone_emits_skip_notice(apm_home: Path, tmp_path: Path) -> None:
    """Skipping untrusted scripts must surface a visible skip-notice."""
    project = tmp_path / "clone"
    sentinel = project / "SENTINEL"
    write_project(project, "post-install", [touch_cmd(sentinel)])

    class _Log:
        def __init__(self) -> None:
            self.msgs: list[str] = []

        def warning(self, msg: str) -> None:
            self.msgs.append(msg)

    log = _Log()
    fire_via_context(project, "post-install", logger=log)

    assert any("untrusted project script" in m for m in log.msgs), (
        f"expected a skip-notice, got: {log.msgs}"
    )
    assert not sentinel.exists()


def test_trusted_project_runs_scripts(apm_home: Path, tmp_path: Path) -> None:
    """After apm lifecycle trust -> sentinel MUST exist."""
    project = tmp_path / "trusted"
    sentinel = project / "SENTINEL"
    apm_yml = write_project(project, "post-install", [touch_cmd(sentinel)])

    trust(apm_yml)
    fire_via_context(project, "post-install")

    assert sentinel.exists(), "trusted project script should have run"


def test_apm_no_scripts_blocks_even_when_trusted(
    apm_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APM_NO_SCRIPTS=1 is a blanket kill-switch even for trusted repos."""
    project = tmp_path / "trusted"
    sentinel = project / "SENTINEL"
    apm_yml = write_project(project, "post-install", [touch_cmd(sentinel)])
    trust(apm_yml)

    monkeypatch.setenv("APM_NO_SCRIPTS", "1")
    fire_via_context(project, "post-install")

    assert not sentinel.exists(), "APM_NO_SCRIPTS must suppress trusted scripts"


def test_org_deny_all_suppresses_trusted_scripts(apm_home: Path, tmp_path: Path) -> None:
    """Org executables.deny_all is a one-way ceiling -> no sentinel."""
    project = tmp_path / "trusted"
    sentinel = project / "SENTINEL"
    apm_yml = write_project(project, "post-install", [touch_cmd(sentinel)])
    trust(apm_yml)

    fire_via_context(project, "post-install", deny_all=True)

    assert not sentinel.exists(), "org deny_all must suppress all scripts"


def test_editing_lifecycle_revokes_trust(apm_home: Path, tmp_path: Path) -> None:
    """Trust is keyed by the lifecycle: hash -- editing it revokes trust."""
    project = tmp_path / "trusted"
    sentinel = project / "SENTINEL"
    apm_yml = write_project(project, "post-install", [touch_cmd(sentinel)])
    trust(apm_yml)

    # Attacker swaps the command after trust was granted.
    evil = project / "EVIL"
    write_project(project, "post-install", [touch_cmd(evil)])

    fire_via_context(project, "post-install")

    assert not evil.exists(), "GATE LEAK: edited lifecycle ran without re-trust"
