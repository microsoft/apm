"""Integration tests for apm find command."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockFile

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "find"


def _load_fixture_lockfile() -> LockFile:
    lf = LockFile.read(FIXTURE_DIR / "apm.lock.yaml")
    assert lf is not None, "Fixture lockfile could not be read"
    return lf


def _write_lockfile_and_run(runner: CliRunner, lf: LockFile, args: list[str]):
    """Write lf to disk in isolated FS and invoke CLI with args."""
    with runner.isolated_filesystem():
        with open("apm.lock.yaml", "w", encoding="utf-8") as f:
            f.write(lf.to_yaml())
        return runner.invoke(cli, args)


class TestFindIntegration:
    def test_acceptance_1_exits_zero_for_tracked_file(self):
        """apm find <path> resolves tracked file to owner package(s), exit 0."""
        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(
            runner, lf, ["find", ".github/instructions/git-utils.instructions.md"]
        )
        assert result.exit_code == 0, result.output
        assert "github.com/acme/git-utils" in result.output

    def test_acceptance_2_default_output_package_names_only(self):
        """Default output: package names only (one per line)."""
        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(
            runner, lf, ["find", ".github/instructions/git-utils.instructions.md"]
        )
        assert result.exit_code == 0
        lines = [line for line in result.output.strip().splitlines() if line.strip()]
        assert any("github.com/acme/git-utils" in line for line in lines)
        from urllib.parse import urlparse

        urls = [tok for tok in result.output.split() if "://" in tok]
        assert not any(urlparse(url).scheme == "oci" for url in urls)

    def test_acceptance_3_source_flag_oci(self):
        """--source renders oci:// origins correctly."""
        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(
            runner, lf, ["find", ".github/instructions/oci-tools.instructions.md", "--source"]
        )
        assert result.exit_code == 0, result.output
        from urllib.parse import urlparse

        urls = [tok for tok in result.output.split() if "://" in tok]
        assert any(urlparse(url).scheme == "oci" for url in urls), result.output

    def test_acceptance_3_source_flag_git(self):
        """--source renders git ref origins correctly."""
        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(
            runner, lf, ["find", ".github/instructions/git-utils.instructions.md", "--source"]
        )
        assert result.exit_code == 0, result.output
        assert "v2.0.0" in result.output

    def test_acceptance_3_source_flag_local(self):
        """--source renders local path origins correctly."""
        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(
            runner, lf, ["find", ".github/instructions/local-helper.instructions.md", "--source"]
        )
        assert result.exit_code == 0, result.output
        assert "./packages/local-helper" in result.output

    def test_acceptance_4_path_flag_chain_output(self):
        """--path: chain output includes repo_url."""
        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(
            runner, lf, ["find", ".github/instructions/git-utils.instructions.md", "--path"]
        )
        assert result.exit_code == 0, result.output
        assert "github.com/acme/git-utils" in result.output

    def test_acceptance_5_agents_md_lists_all_contributors(self):
        """AGENTS.md lists ALL contributing packages."""
        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(runner, lf, ["find", "AGENTS.md"])
        assert result.exit_code == 0, result.output
        assert "github.com/acme/git-utils" in result.output
        assert "github.com/acme/local-helper" in result.output

    def test_acceptance_5_claude_md_lists_all_contributors(self):
        """CLAUDE.md lists ALL contributing packages."""
        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(runner, lf, ["find", "CLAUDE.md"])
        assert result.exit_code == 0, result.output
        assert "github.com/acme/git-utils" in result.output
        assert "github.com/acme/local-helper" in result.output

    def test_acceptance_6_unknown_path_nonzero_exit(self):
        """Unknown path -> non-zero exit + [x] message via _rich_error."""
        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(runner, lf, ["find", "totally-unknown-file.txt"])
        assert result.exit_code != 0
        # stderr is mixed into output in Click 8.x
        assert "[x]" in result.output

    def test_acceptance_7_path_normalization_directory_prefix(self):
        """Path normalization: .claude/ directory entry resolves files under it."""
        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(runner, lf, ["find", ".claude/skills/oci-tools/"])
        assert result.exit_code == 0, result.output
        assert "github.com/acme/oci-tools" in result.output

    def test_acceptance_8_output_ascii_only(self):
        """All output ASCII-only."""
        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(
            runner, lf, ["find", ".github/instructions/git-utils.instructions.md", "--source"]
        )
        for char in result.output:
            assert ord(char) < 128 or char in "\n\r\t", f"Non-ASCII character found: {char!r}"

    def test_acceptance_9_zero_network_operations(self, monkeypatch):
        """Zero network/auth/write operations: no HTTP calls."""
        import urllib.request

        def fail_urlopen(*args, **kwargs):
            raise AssertionError("Network call detected in apm find")

        monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(
            runner, lf, ["find", ".github/instructions/git-utils.instructions.md"]
        )
        assert result.exit_code == 0, result.output

    def test_workspace_local_deployed_files(self):
        """Workspace-deployed files are attributed to '.' (workspace)."""
        lf = _load_fixture_lockfile()
        runner = CliRunner()
        result = _write_lockfile_and_run(
            runner, lf, ["find", ".github/instructions/workspace.instructions.md"]
        )
        assert result.exit_code == 0, result.output
        # The workspace sentinel "." must appear as a whole token on its own line.
        output_lines = [line.strip() for line in result.output.splitlines()]
        assert "." in output_lines, (
            f"Workspace sentinel '.' not found as own line: {result.output!r}"
        )
