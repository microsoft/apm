"""A failed install names the native target files it leaves on disk (#3525).

``InstallTransaction`` restores ``apm.yml`` and ``apm_modules`` staging only;
native target integrations run outside it. When an install fails after those
files were written, the output must list every leftover path, must not imply
they were rolled back or restored, and must give one next step.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.command_logger import InstallLogger
from apm_cli.deps.lockfile import LockedDependency, LockFile, get_lockfile_path
from apm_cli.install.context import InstallContext
from apm_cli.install.errors import DirectDependencyError
from apm_cli.install.failure_residue import (
    report_failed_install_residue,
    unrecorded_deployed_paths,
)
from apm_cli.models.results import InstallDisposition
from apm_cli.utils.diagnostics import DiagnosticCollector

_ROLLBACK_WORDING = ("rolled back", "roll back", "rollback", "restored", "restore", "reverted")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _assert_no_rollback_claim(text: str) -> None:
    lowered = text.lower()
    for phrase in _ROLLBACK_WORDING:
        assert phrase not in lowered, f"unexpected rollback wording {phrase!r} in: {text}"


def _write_local_package(root: Path) -> None:
    (root / ".apm" / "agents").mkdir(parents=True)
    (root / "apm.yml").write_text("name: good\nversion: 1.0.0\n", encoding="ascii")
    (root / ".apm" / "agents" / "helper.agent.md").write_text(
        "---\ndescription: helper\n---\nhello\n", encoding="ascii"
    )


def _project_with_local_dependency(tmp_path: Path, monkeypatch) -> tuple[Path, bytes]:
    _write_local_package(tmp_path / "good")
    project = tmp_path / "proj"
    project.mkdir()
    manifest = b"name: proj\nversion: 1.0.0\ndependencies:\n  apm:\n    - ../good\n"
    (project / "apm.yml").write_bytes(manifest)
    monkeypatch.chdir(project)
    return project, manifest


def _forced_lockfile_failure(self) -> None:
    raise RuntimeError("forced mid-install failure")


class TestCliFailureAfterNativeDeploy:
    def test_failure_lists_every_leftover_path_without_rollback_claim(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        project, manifest = _project_with_local_dependency(tmp_path, monkeypatch)

        with patch(
            "apm_cli.install.phases.lockfile.LockfileBuilder.build_and_save",
            _forced_lockfile_failure,
        ):
            result = CliRunner().invoke(cli, ["install", "--target", "claude"])

        output = _flat(result.output)
        leftover = project / ".claude" / "agents" / "helper.md"
        assert result.exit_code == 1, result.output
        assert leftover.is_file(), "the forced failure must come after a native deploy"
        assert ".claude/agents/helper.md" in output
        assert "native target" in output
        assert "Remove these files" in output
        assert "re-run 'apm install'" in output
        assert "--force" in output
        _assert_no_rollback_claim(result.output)
        # Existing apm.yml and lockfile behavior on failure is unchanged.
        assert (project / "apm.yml").read_bytes() == manifest
        assert not get_lockfile_path(project).exists()

    def test_successful_install_reports_no_residue(self, tmp_path: Path, monkeypatch) -> None:
        project, _manifest = _project_with_local_dependency(tmp_path, monkeypatch)

        result = CliRunner().invoke(cli, ["install", "--target", "claude"])

        assert result.exit_code == 0, result.output
        assert (project / ".claude" / "agents" / "helper.md").is_file()
        assert "native target" not in _flat(result.output)


def _run_pipeline_with_integrate(tmp_path: Path, monkeypatch, integrate, logger):
    from apm_cli.install.pipeline import run_install_pipeline

    monkeypatch.chdir(tmp_path)
    # Local primitives keep the pipeline past its nothing-to-install exit.
    instructions = tmp_path / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "local.instructions.md").write_text(
        "---\napplyTo: '**'\n---\nlocal\n", encoding="ascii"
    )
    pkg = MagicMock()
    pkg.dependencies = {"apm": []}

    def _fake_targets_run(ctx: InstallContext) -> None:
        ctx.integrators["skill"] = MagicMock()

    with (
        patch("apm_cli.install.phases.integrate.run", side_effect=integrate),
        patch("apm_cli.install.phases.resolve.run"),
        patch("apm_cli.install.phases.targets.run", side_effect=_fake_targets_run),
        patch("apm_cli.install.phases.download.run"),
    ):
        return run_install_pipeline(apm_package=pkg, verbose=False, logger=logger, target="claude")


def _record_partial_deploy(ctx: InstallContext) -> None:
    ctx.package_deployed_files["owner/b"] = [".claude/skills/b-skill", ".claude/agents/b.md"]
    ctx.package_deployed_files["owner/a"] = [".claude/agents/a.md"]
    ctx.local_deployed_files = [".claude/agents/local.md"]


_EXPECTED_PARTIAL = [
    ".claude/agents/a.md",
    ".claude/agents/b.md",
    ".claude/agents/local.md",
    ".claude/skills/b-skill",
]


class TestPipelineFailureExits:
    def test_raised_failure_reports_every_deployed_path(self, tmp_path: Path, monkeypatch) -> None:
        logger = MagicMock()

        def _integrate(ctx: InstallContext) -> None:
            _record_partial_deploy(ctx)
            ctx.direct_dep_failed = True

        with pytest.raises(DirectDependencyError):
            _run_pipeline_with_integrate(tmp_path, monkeypatch, _integrate, logger)

        logger.failed_install_residue.assert_called_once_with(_EXPECTED_PARTIAL)

    def test_report_failure_never_masks_the_original_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        logger = MagicMock()
        logger.failed_install_residue.side_effect = OSError("console closed")

        def _integrate(ctx: InstallContext) -> None:
            _record_partial_deploy(ctx)
            ctx.direct_dep_failed = True

        with pytest.raises(DirectDependencyError):
            _run_pipeline_with_integrate(tmp_path, monkeypatch, _integrate, logger)

        logger.failed_install_residue.assert_called_once()

    def test_failed_result_reports_every_deployed_path(self, tmp_path: Path, monkeypatch) -> None:
        logger = InstallLogger()

        def _integrate(ctx: InstallContext) -> None:
            _record_partial_deploy(ctx)
            ctx.diagnostics.error("forced package failure", package="owner/c")

        with patch.object(InstallLogger, "failed_install_residue") as spy:
            result = _run_pipeline_with_integrate(tmp_path, monkeypatch, _integrate, logger)

        assert result.disposition is InstallDisposition.FAILED
        spy.assert_called_once_with(_EXPECTED_PARTIAL)

    def test_failure_before_any_deploy_reports_nothing(self, tmp_path: Path, monkeypatch) -> None:
        logger = MagicMock()

        def _integrate(ctx: InstallContext) -> None:
            ctx.direct_dep_failed = True

        with pytest.raises(DirectDependencyError):
            _run_pipeline_with_integrate(tmp_path, monkeypatch, _integrate, logger)

        logger.failed_install_residue.assert_not_called()


def _ctx(tmp_path: Path, logger) -> InstallContext:
    ctx = InstallContext(project_root=tmp_path, apm_dir=tmp_path)
    ctx.logger = logger
    ctx.diagnostics = DiagnosticCollector()
    return ctx


class TestUnrecordedDeployedPaths:
    def test_paths_recorded_by_on_disk_lockfile_are_excluded(self, tmp_path: Path) -> None:
        lockfile = LockFile()
        lockfile.add_dependency(
            LockedDependency(repo_url="owner/old", deployed_files=[".claude/agents/old.md"])
        )
        lockfile.local_deployed_files = [".claude/agents/local-old.md"]
        lockfile.write(get_lockfile_path(tmp_path))
        ctx = _ctx(tmp_path, MagicMock())
        ctx.package_deployed_files = {
            "owner/old": [".claude/agents/old.md"],
            "owner/new": [".claude\\agents\\new.md"],
        }
        ctx.local_deployed_files = [".claude/agents/local-old.md", ".claude/agents/local-new.md"]

        assert unrecorded_deployed_paths(ctx) == [
            ".claude/agents/local-new.md",
            ".claude/agents/new.md",
        ]

    def test_unreadable_lockfile_lists_every_deployed_path(self, tmp_path: Path) -> None:
        get_lockfile_path(tmp_path).write_text(": not [valid yaml", encoding="ascii")
        ctx = _ctx(tmp_path, MagicMock())
        ctx.package_deployed_files = {"owner/new": [".claude/agents/new.md"]}

        assert unrecorded_deployed_paths(ctx) == [".claude/agents/new.md"]

    def test_dry_run_reports_nothing(self, tmp_path: Path) -> None:
        logger = MagicMock()
        ctx = _ctx(tmp_path, logger)
        ctx.dry_run = True
        ctx.package_deployed_files = {"owner/new": [".claude/agents/new.md"]}

        report_failed_install_residue(ctx)

        logger.failed_install_residue.assert_not_called()


class TestInstallLoggerResidue:
    @patch("apm_cli.core.command_logger._rich_echo")
    @patch("apm_cli.core.command_logger._rich_info")
    @patch("apm_cli.core.command_logger._rich_warning")
    def test_residue_lists_paths_and_one_next_step(self, mock_warning, mock_info, mock_echo):
        InstallLogger().failed_install_residue([".claude/agents/a.md", ".claude/skills/s"])

        rendered = " ".join(
            str(call.args[0])
            for mock in (mock_warning, mock_echo, mock_info)
            for call in mock.call_args_list
        )
        assert "2 native target files" in rendered
        assert ".claude/agents/a.md" in rendered
        assert ".claude/skills/s" in rendered
        assert "Remove these files, or re-run 'apm install'" in rendered
        assert "--force" in rendered
        _assert_no_rollback_claim(rendered)

    @patch("apm_cli.core.command_logger._rich_error")
    def test_failed_summary_with_residue_drops_uncommitted_claim(self, mock_error):
        logger = InstallLogger()
        with (
            patch("apm_cli.core.command_logger._rich_warning"),
            patch("apm_cli.core.command_logger._rich_echo"),
            patch("apm_cli.core.command_logger._rich_info"),
        ):
            logger.failed_install_residue([".claude/agents/a.md"])

        logger.install_summary(
            apm_count=0, mcp_count=0, errors=1, disposition=InstallDisposition.FAILED
        )

        msg = mock_error.call_args[0][0]
        assert "No install transaction changes were committed" not in msg
        assert "1 native target file listed above remains on disk" in msg
        _assert_no_rollback_claim(msg)

    @patch("apm_cli.core.command_logger._rich_error")
    def test_failed_summary_without_residue_is_unchanged(self, mock_error):
        InstallLogger().install_summary(
            apm_count=0, mcp_count=0, errors=1, disposition=InstallDisposition.FAILED
        )

        msg = mock_error.call_args[0][0]
        assert (
            msg
            == "Installation failed with 1 error(s). No install transaction changes were committed."
        )
