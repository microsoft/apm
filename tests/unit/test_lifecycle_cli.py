"""Unit coverage for the ``apm lifecycle`` CLI command group.

The ``apm lifecycle`` subcommands (list / test / init / validate / trust /
untrust) are net-new in this PR and previously had no collected unit coverage
(only the adversarial red-team suite, which CI does not collect, touched the
underlying executors). This module drives each subcommand through Click's
``CliRunner`` against an isolated ``APM_HOME`` + working directory so a real
regression in the CLI surface now fails a normal CI shard.

It is a top-level ``tests/unit/test_*.py`` file by design: it exercises code
paths that are otherwise weakly represented on the second pytest-split shard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.commands.lifecycle import lifecycle


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated project dir + APM_HOME; chdir into the project."""
    home = tmp_path / "apmhome"
    home.mkdir()
    monkeypatch.setenv("APM_HOME", str(home))
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return proj


def _write_apm_yml(proj: Path, lifecycle_block: str = "") -> None:
    body = "name: demo\nversion: 0.0.1\n"
    if lifecycle_block:
        body += lifecycle_block
    (proj / "apm.yml").write_text(body, encoding="utf-8")


_LIFECYCLE_BLOCK = (
    "lifecycle:\n"
    "  post-install:\n"
    "    - type: command\n"
    "      command: echo hi\n"
    "      timeoutSec: 30\n"
)


class TestLifecycleList:
    def test_no_scripts_reports_empty(self, project: Path) -> None:
        _write_apm_yml(project)
        result = CliRunner().invoke(lifecycle, [])
        assert result.exit_code == 0
        assert "No lifecycle scripts discovered" in result.output

    def test_lists_discovered_scripts(self, project: Path) -> None:
        _write_apm_yml(project, _LIFECYCLE_BLOCK)
        result = CliRunner().invoke(lifecycle, [])
        assert result.exit_code == 0
        assert "post-install" in result.output


class TestLifecycleInit:
    def test_init_without_apm_yml_errors(self, project: Path) -> None:
        result = CliRunner().invoke(lifecycle, ["init"])
        assert result.exit_code == 1
        assert "No apm.yml" in result.output

    def test_init_injects_block(self, project: Path) -> None:
        _write_apm_yml(project)
        result = CliRunner().invoke(lifecycle, ["init"])
        assert result.exit_code == 0
        assert "lifecycle:" in (project / "apm.yml").read_text(encoding="utf-8")

    def test_init_existing_block_warns_without_force(self, project: Path) -> None:
        _write_apm_yml(project, _LIFECYCLE_BLOCK)
        result = CliRunner().invoke(lifecycle, ["init"])
        assert result.exit_code == 0
        assert "already has a lifecycle" in result.output

    def test_init_force_overwrites(self, project: Path) -> None:
        _write_apm_yml(project, _LIFECYCLE_BLOCK)
        result = CliRunner().invoke(lifecycle, ["init", "--force"])
        assert result.exit_code == 0
        assert "Injected" in result.output

    def test_init_non_mapping_top_level_errors(self, project: Path) -> None:
        (project / "apm.yml").write_text("- just\n- a\n- list\n", encoding="utf-8")
        result = CliRunner().invoke(lifecycle, ["init"])
        assert result.exit_code == 1
        assert "not a mapping" in result.output


class TestLifecycleValidate:
    def test_validate_clean_manifest(self, project: Path) -> None:
        _write_apm_yml(project, _LIFECYCLE_BLOCK)
        result = CliRunner().invoke(lifecycle, ["validate"])
        assert result.exit_code == 0

    def test_validate_reports_bad_entry(self, project: Path) -> None:
        bad = "lifecycle:\n  post-install:\n    - type: command\n      timeoutSec: 30\n"
        _write_apm_yml(project, bad)
        result = CliRunner().invoke(lifecycle, ["validate"])
        assert result.exit_code != 0 or "error" in result.output.lower()


class TestLifecycleTest:
    def test_dry_run_no_scripts(self, project: Path) -> None:
        _write_apm_yml(project)
        result = CliRunner().invoke(lifecycle, ["test", "post-install"])
        assert result.exit_code == 0
        assert "No scripts registered" in result.output

    def test_dry_run_lists_untrusted(self, project: Path) -> None:
        _write_apm_yml(project, _LIFECYCLE_BLOCK)
        result = CliRunner().invoke(lifecycle, ["test", "post-install"])
        assert result.exit_code == 0
        assert "Dry-run" in result.output
        assert "untrusted" in result.output


class TestLifecycleTrust:
    def test_trust_without_apm_yml_warns(self, project: Path) -> None:
        result = CliRunner().invoke(lifecycle, ["trust"])
        assert result.exit_code == 0
        assert "No apm.yml" in result.output

    def test_trust_then_dry_run_shows_trusted(self, project: Path) -> None:
        _write_apm_yml(project, _LIFECYCLE_BLOCK)
        trust = CliRunner().invoke(lifecycle, ["trust"])
        assert trust.exit_code == 0
        assert "Trusted" in trust.output
        dry = CliRunner().invoke(lifecycle, ["test", "post-install"])
        assert "[trusted]" in dry.output

    def test_untrust_when_not_trusted_is_noop(self, project: Path) -> None:
        _write_apm_yml(project, _LIFECYCLE_BLOCK)
        result = CliRunner().invoke(lifecycle, ["untrust"])
        assert result.exit_code == 0
        assert "not trusted" in result.output.lower()

    def test_trust_then_untrust_revokes(self, project: Path) -> None:
        _write_apm_yml(project, _LIFECYCLE_BLOCK)
        CliRunner().invoke(lifecycle, ["trust"])
        result = CliRunner().invoke(lifecycle, ["untrust"])
        assert result.exit_code == 0
        assert "Revoked" in result.output
