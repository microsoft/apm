"""Integration test: lifecycle trust gate wires correctly through build_runner_from_context."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.core.lifecycle_scripts import (
    LifecycleEvent,
    PackageInfo,
    build_runner_from_context,
)
from apm_cli.core.script_trust import trust_project_scripts


def _write_apm_yml(path: Path, sentinel: Path) -> None:
    """Write an apm.yml with a post-install command that creates sentinel."""
    cmd = f"{sys.executable} -c \\\"open('{sentinel}', 'w').close()\\\""
    path.write_text(
        f'name: test-pkg\nlifecycle:\n  post-install:\n    - type: command\n      run: "{cmd}"\n',
        encoding="utf-8",
    )


def _fire_post_install(project_root: Path) -> None:
    """Build runner + fire post-install, joining any threads."""
    with patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=None):
        runner = build_runner_from_context(project_root=str(project_root))
    event = LifecycleEvent.create(
        event="post-install",
        packages=[PackageInfo(name="test/pkg", reference="v1.0.0")],
        scope="project",
        working_directory=str(project_root),
    )
    threads = runner.fire("post-install", event)
    for t in threads:
        t.join(timeout=10)


class TestLifecycleTrustGate:
    def test_untrusted_project_scripts_do_not_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without trust, post-install sentinel is NOT created."""
        apm_home = tmp_path / "apm_home"
        apm_home.mkdir()
        monkeypatch.setenv("APM_HOME", str(apm_home))
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)

        project = tmp_path / "project"
        project.mkdir()
        sentinel = project / "sentinel.txt"

        _write_apm_yml(project / "apm.yml", sentinel)

        _fire_post_install(project)

        assert not sentinel.exists(), "Untrusted script should NOT have created sentinel"

    def test_trusted_project_scripts_do_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With trust, post-install sentinel IS created."""
        apm_home = tmp_path / "apm_home"
        apm_home.mkdir()
        monkeypatch.setenv("APM_HOME", str(apm_home))
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)

        project = tmp_path / "project"
        project.mkdir()
        sentinel = project / "sentinel.txt"

        apm_yml = project / "apm.yml"
        _write_apm_yml(apm_yml, sentinel)

        trust_store = apm_home / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            trust_project_scripts(apm_yml)

        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            _fire_post_install(project)

        assert sentinel.exists(), "Trusted script should have created sentinel"

    def test_apm_no_scripts_blocks_even_trusted_scripts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """APM_NO_SCRIPTS=1 prevents even trusted scripts from running."""
        apm_home = tmp_path / "apm_home"
        apm_home.mkdir()
        monkeypatch.setenv("APM_HOME", str(apm_home))
        monkeypatch.setenv("APM_NO_SCRIPTS", "1")

        project = tmp_path / "project"
        project.mkdir()
        sentinel = project / "sentinel.txt"

        apm_yml = project / "apm.yml"
        _write_apm_yml(apm_yml, sentinel)

        trust_store = apm_home / "scripts-trust.json"
        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            trust_project_scripts(apm_yml)

        with patch("apm_cli.core.script_trust._trust_store_path", return_value=trust_store):
            _fire_post_install(project)

        assert not sentinel.exists(), "APM_NO_SCRIPTS should block even trusted scripts"

    def test_runner_reports_skipped_project_scripts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Untrusted project scripts are counted in skipped_project_scripts."""
        apm_home = tmp_path / "apm_home"
        apm_home.mkdir()
        monkeypatch.setenv("APM_HOME", str(apm_home))
        monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)

        project = tmp_path / "project"
        project.mkdir()
        sentinel = project / "sentinel.txt"
        _write_apm_yml(project / "apm.yml", sentinel)

        with patch("apm_cli.policy.discovery.discover_policy_with_chain", return_value=None):
            runner = build_runner_from_context(project_root=str(project))

        assert runner._skipped_project_scripts == 1
        assert runner._scripts == []
