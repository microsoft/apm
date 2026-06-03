"""Unit tests for the apm find CLI surface."""

from __future__ import annotations

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.lockfile import LockedDependency, LockFile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lockfile_with_oci() -> LockFile:
    lf = LockFile()
    dep = LockedDependency(
        repo_url="github.com/acme/oci-tools",
        resolved_commit="abc123",
        resolved_ref="main",
        depth=1,
        source="registry",
        resolved_url="oci://registry.example.com/acme/oci-tools@sha256:abc123",
        resolved_hash="sha256:abc123",
        deployed_files=[".github/instructions/oci-tools.instructions.md"],
    )
    lf.add_dependency(dep)
    return lf


def _make_lockfile_with_git() -> LockFile:
    lf = LockFile()
    dep = LockedDependency(
        repo_url="github.com/acme/git-utils",
        resolved_commit="def789",
        resolved_ref="v2.0.0",
        depth=1,
        deployed_files=[".github/instructions/git-utils.instructions.md"],
    )
    lf.add_dependency(dep)
    return lf


def _make_lockfile_with_local() -> LockFile:
    lf = LockFile()
    dep = LockedDependency(
        repo_url="github.com/acme/local-helper",
        source="local",
        local_path="./packages/local-helper",
        depth=1,
        deployed_files=[".github/instructions/local-helper.instructions.md"],
    )
    lf.add_dependency(dep)
    return lf


def _make_lockfile_multi_contributor() -> LockFile:
    lf = LockFile()
    dep1 = LockedDependency(
        repo_url="github.com/acme/pkg-a",
        resolved_commit="aaa111",
        depth=1,
        deployed_files=["AGENTS.md"],
    )
    dep2 = LockedDependency(
        repo_url="github.com/acme/pkg-b",
        resolved_commit="bbb222",
        depth=1,
        deployed_files=["AGENTS.md"],
    )
    lf.add_dependency(dep1)
    lf.add_dependency(dep2)
    return lf


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFindCommandBasic:
    def test_known_file_exits_zero(self):
        lf = _make_lockfile_with_git()
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("apm.lock.yaml", "w", encoding="utf-8") as f:
                f.write(lf.to_yaml())
            result = runner.invoke(cli, ["find", ".github/instructions/git-utils.instructions.md"])
        assert result.exit_code == 0, result.output
        assert "github.com/acme/git-utils" in result.output

    def test_unknown_file_exits_nonzero_with_error_message(self):
        lf = _make_lockfile_with_git()
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("apm.lock.yaml", "w", encoding="utf-8") as f:
                f.write(lf.to_yaml())
            result = runner.invoke(cli, ["find", "nonexistent.txt"])
        assert result.exit_code != 0
        # stderr is mixed into output in Click 8.x
        assert "[x]" in result.output

    def test_source_flag_oci_origin(self):
        lf = _make_lockfile_with_oci()
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("apm.lock.yaml", "w", encoding="utf-8") as f:
                f.write(lf.to_yaml())
            result = runner.invoke(
                cli,
                ["find", ".github/instructions/oci-tools.instructions.md", "--source"],
            )
        assert result.exit_code == 0, result.output
        from urllib.parse import urlparse

        urls = [tok for tok in result.output.split() if "://" in tok]
        assert any(urlparse(url).scheme == "oci" for url in urls), result.output

    def test_source_flag_git_origin(self):
        lf = _make_lockfile_with_git()
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("apm.lock.yaml", "w", encoding="utf-8") as f:
                f.write(lf.to_yaml())
            result = runner.invoke(
                cli,
                ["find", ".github/instructions/git-utils.instructions.md", "--source"],
            )
        assert result.exit_code == 0, result.output
        assert "v2.0.0" in result.output

    def test_source_flag_local_origin(self):
        lf = _make_lockfile_with_local()
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("apm.lock.yaml", "w", encoding="utf-8") as f:
                f.write(lf.to_yaml())
            result = runner.invoke(
                cli,
                ["find", ".github/instructions/local-helper.instructions.md", "--source"],
            )
        assert result.exit_code == 0, result.output
        assert "./packages/local-helper" in result.output

    def test_multi_contributor_lists_all_packages(self):
        lf = _make_lockfile_multi_contributor()
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("apm.lock.yaml", "w", encoding="utf-8") as f:
                f.write(lf.to_yaml())
            result = runner.invoke(cli, ["find", "AGENTS.md"])
        assert result.exit_code == 0, result.output
        assert "github.com/acme/pkg-a" in result.output
        assert "github.com/acme/pkg-b" in result.output

    def test_path_flag_includes_chain(self):
        lf = _make_lockfile_with_git()
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("apm.lock.yaml", "w", encoding="utf-8") as f:
                f.write(lf.to_yaml())
            result = runner.invoke(
                cli,
                ["find", ".github/instructions/git-utils.instructions.md", "--path"],
            )
        assert result.exit_code == 0, result.output
        assert "github.com/acme/git-utils" in result.output

    def test_no_lockfile_exits_with_code_2(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["find", "some-file.md"])
        assert result.exit_code == 2

    def test_output_is_ascii_only(self):
        lf = _make_lockfile_with_git()
        runner = CliRunner()
        with runner.isolated_filesystem():
            with open("apm.lock.yaml", "w", encoding="utf-8") as f:
                f.write(lf.to_yaml())
            result = runner.invoke(
                cli,
                ["find", ".github/instructions/git-utils.instructions.md"],
            )
        for char in result.output:
            assert ord(char) < 128 or char in "\n\r\t", f"Non-ASCII character found: {char!r}"
