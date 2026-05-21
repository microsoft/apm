"""Unit tests for apm_cli.deps.aggregator."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest


class TestScanWorkflowsForDependencies:
    """Tests for scan_workflows_for_dependencies."""

    def test_returns_empty_set_when_no_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.deps.aggregator import scan_workflows_for_dependencies

        with patch("apm_cli.deps.aggregator.glob.glob", return_value=[]):
            result = scan_workflows_for_dependencies()

        assert result == set()

    def test_extracts_mcp_servers_from_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        prompt_file = tmp_path / "test.prompt.md"
        prompt_file.write_text(
            textwrap.dedent("""\
                ---
                mcp:
                  - github
                  - filesystem
                ---
                Some prompt content
            """),
            encoding="utf-8",
        )

        from apm_cli.deps.aggregator import scan_workflows_for_dependencies

        with patch(
            "apm_cli.deps.aggregator.glob.glob",
            return_value=[str(prompt_file)],
        ):
            result = scan_workflows_for_dependencies()

        assert "github" in result
        assert "filesystem" in result

    def test_ignores_non_mcp_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        prompt_file = tmp_path / "test.prompt.md"
        prompt_file.write_text(
            textwrap.dedent("""\
                ---
                title: Just a prompt
                ---
                Body text
            """),
            encoding="utf-8",
        )

        from apm_cli.deps.aggregator import scan_workflows_for_dependencies

        with patch(
            "apm_cli.deps.aggregator.glob.glob",
            return_value=[str(prompt_file)],
        ):
            result = scan_workflows_for_dependencies()

        assert result == set()

    def test_ignores_mcp_non_list(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        prompt_file = tmp_path / "test.prompt.md"
        prompt_file.write_text(
            textwrap.dedent("""\
                ---
                mcp: single-string-not-a-list
                ---
                Body text
            """),
            encoding="utf-8",
        )

        from apm_cli.deps.aggregator import scan_workflows_for_dependencies

        with patch(
            "apm_cli.deps.aggregator.glob.glob",
            return_value=[str(prompt_file)],
        ):
            result = scan_workflows_for_dependencies()

        assert result == set()

    def test_deduplicates_servers_across_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        f1 = tmp_path / "a.prompt.md"
        f2 = tmp_path / "b.prompt.md"
        f1.write_text("---\nmcp:\n  - github\n---\n", encoding="utf-8")
        f2.write_text("---\nmcp:\n  - github\n  - filesystem\n---\n", encoding="utf-8")

        from apm_cli.deps.aggregator import scan_workflows_for_dependencies

        with patch(
            "apm_cli.deps.aggregator.glob.glob",
            return_value=[str(f1), str(f2)],
        ):
            result = scan_workflows_for_dependencies()

        assert result == {"github", "filesystem"}

    def test_handles_file_read_error_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.deps.aggregator import scan_workflows_for_dependencies

        with (
            patch(
                "apm_cli.deps.aggregator.glob.glob",
                return_value=["nonexistent.prompt.md"],
            ),
            patch(
                "apm_cli.deps.aggregator.frontmatter.load",
                side_effect=OSError("file not found"),
            ),
        ):
            result = scan_workflows_for_dependencies()

        # Errors are printed but not raised; result is still empty set
        assert result == set()
        captured = capsys.readouterr()
        assert "Error processing" in captured.out


class TestSyncWorkflowDependencies:
    """Tests for sync_workflow_dependencies."""

    def test_returns_true_and_server_list_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.deps.aggregator import sync_workflow_dependencies

        with (
            patch(
                "apm_cli.deps.aggregator.scan_workflows_for_dependencies",
                return_value={"github", "filesystem"},
            ),
            patch("apm_cli.utils.yaml_io.dump_yaml") as mock_dump,
        ):
            success, servers = sync_workflow_dependencies(str(tmp_path / "apm.yml"))

        assert success is True
        assert set(servers) == {"filesystem", "github"}
        mock_dump.assert_called_once()

    def test_returns_false_on_write_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.deps.aggregator import sync_workflow_dependencies

        with (
            patch(
                "apm_cli.deps.aggregator.scan_workflows_for_dependencies",
                return_value={"github"},
            ),
            patch(
                "apm_cli.utils.yaml_io.dump_yaml",
                side_effect=OSError("disk full"),
            ),
        ):
            success, servers = sync_workflow_dependencies(str(tmp_path / "apm.yml"))

        assert success is False
        assert servers == []
        captured = capsys.readouterr()
        assert "Error writing" in captured.out

    def test_servers_are_sorted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        from apm_cli.deps.aggregator import sync_workflow_dependencies

        captured_config: dict = {}

        def _capture_dump(config: dict, _path: str) -> None:
            captured_config.update(config)

        with (
            patch(
                "apm_cli.deps.aggregator.scan_workflows_for_dependencies",
                return_value={"zebra", "alpha", "mango"},
            ),
            patch("apm_cli.utils.yaml_io.dump_yaml", side_effect=_capture_dump),
        ):
            sync_workflow_dependencies("apm.yml")

        assert captured_config["servers"] == ["alpha", "mango", "zebra"]
