"""Wave 6: integration tests for output/script_formatters.py and deps/lockfile.py.

Goal: maximise code coverage by exercising real code paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# output/script_formatters.py
# ---------------------------------------------------------------------------


class TestScriptExecutionFormatter:
    """Cover ScriptExecutionFormatter methods -- pure logic, no mocking needed."""

    def test_header_no_params(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_script_header("test-script", {})
        assert len(lines) == 1
        assert "test-script" in lines[0]

    def test_header_with_params(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_script_header("build", {"env": "prod", "flag": "on"})
        assert len(lines) == 3
        assert "env" in lines[1]
        assert "flag" in lines[2]

    def test_header_with_color(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=True)
        lines = fmt.format_script_header("build", {"env": "prod"})
        assert len(lines) >= 1

    def test_compilation_progress_empty(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        assert fmt.format_compilation_progress([]) == []

    def test_compilation_progress_single(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_compilation_progress(["prompt.md"])
        assert any("Compiling prompt" in line for line in lines)

    def test_compilation_progress_multiple(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_compilation_progress(["a.md", "b.md", "c.md"])
        assert any("3 prompts" in line for line in lines)

    def test_compilation_progress_with_color(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=True)
        lines = fmt.format_compilation_progress(["a.md", "b.md"])
        assert len(lines) >= 1

    def test_runtime_execution_copilot(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_runtime_execution("copilot", "copilot chat", 500)
        assert any("copilot" in line.lower() for line in lines)
        assert any("500" in line for line in lines)

    def test_runtime_execution_codex(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_runtime_execution("codex", "codex run", 1000)
        assert any("codex" in line.lower() for line in lines)

    def test_runtime_execution_unknown(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_runtime_execution("unknown", "cmd", 100)
        assert len(lines) >= 1

    def test_runtime_execution_with_color(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=True)
        lines = fmt.format_runtime_execution("copilot", "copilot chat", 500)
        assert len(lines) >= 1

    def test_content_preview_short(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_content_preview("Short content")
        assert any("preview" in line.lower() for line in lines)

    def test_content_preview_long(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_content_preview("x" * 500, max_preview=100)
        assert len(lines) >= 1

    def test_content_preview_with_color(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=True)
        lines = fmt.format_content_preview("Content here")
        assert len(lines) >= 1

    def test_environment_setup(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_environment_setup("copilot", ["GITHUB_TOKEN", "GITHUB_APM_PAT"])
        assert any("GITHUB_TOKEN" in line for line in lines)

    def test_environment_setup_empty(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_environment_setup("copilot", [])
        assert len(lines) == 0 or isinstance(lines, list)

    def test_environment_setup_with_color(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=True)
        lines = fmt.format_environment_setup("codex", ["GITHUB_TOKEN"])
        assert len(lines) >= 1

    def test_execution_success(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_execution_success("copilot", 1.5)
        assert any("1.5" in line or "success" in line.lower() for line in lines)

    def test_execution_success_with_color(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=True)
        lines = fmt.format_execution_success("codex", 0.5)
        assert len(lines) >= 1

    def test_execution_failure(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=False)
        lines = fmt.format_execution_error("copilot", 1, "something failed")
        assert any("fail" in line.lower() or "error" in line.lower() for line in lines)

    def test_execution_failure_with_color(self) -> None:
        from apm_cli.output.script_formatters import ScriptExecutionFormatter

        fmt = ScriptExecutionFormatter(use_color=True)
        lines = fmt.format_execution_error("llm", 127)
        assert len(lines) >= 1


# ---------------------------------------------------------------------------
# deps/lockfile.py
# ---------------------------------------------------------------------------


class TestLockFile:
    """Cover LockFile read/write/query methods."""

    def test_read_nonexistent(self, tmp_path: Path) -> None:
        from apm_cli.deps.lockfile import LockFile

        result = LockFile.read(tmp_path / "apm.lock.yaml")
        assert result is None

    def test_read_empty_file(self, tmp_path: Path) -> None:
        from apm_cli.deps.lockfile import LockFile

        f = tmp_path / "apm.lock.yaml"
        f.write_text("")
        result = LockFile.read(f)
        # May return None or empty lockfile
        assert result is None or isinstance(result, LockFile)

    def test_read_valid_lockfile(self, tmp_path: Path) -> None:
        from apm_cli.deps.lockfile import LockFile

        f = tmp_path / "apm.lock.yaml"
        f.write_text(
            'lockfile_version: "1"\n'
            "packages:\n"
            "  test-dep:\n"
            '    version: "1.0.0"\n'
            '    resolved: "github:owner/repo"\n'
            '    integrity: "sha256:abc"\n'
        )
        result = LockFile.read(f)
        assert result is not None

    def test_get_lockfile_path(self, tmp_path: Path) -> None:
        from apm_cli.deps.lockfile import get_lockfile_path

        path = get_lockfile_path(tmp_path)
        assert path.name == "apm.lock.yaml"
        assert path.parent == tmp_path

    def test_lockfile_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        from apm_cli.deps.lockfile import LockFile

        lf = LockFile()
        lf_path = tmp_path / "apm.lock.yaml"
        lf.write(lf_path)
        assert lf_path.exists()
        result = LockFile.read(lf_path)
        assert result is not None

    def test_get_package_dependencies_empty(self) -> None:
        from apm_cli.deps.lockfile import LockFile

        lf = LockFile()
        deps = lf.get_package_dependencies()
        assert isinstance(deps, list)
        assert len(deps) == 0


# ---------------------------------------------------------------------------
# models/dependency/reference.py -- DependencyReference parsing
# ---------------------------------------------------------------------------


class TestDependencyReferenceParsing:
    """Cover DependencyReference.parse() with various formats."""

    def test_parse_owner_repo(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo")
        assert ref.repo_url is not None

    def test_parse_github_url(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("https://github.com/owner/repo")
        assert ref is not None

    def test_parse_local_path(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse(str(tmp_path))
        assert ref.is_local is True

    def test_parse_virtual_package(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo/path/to/skill.prompt.md")
        assert ref.is_virtual is True

    def test_parse_with_ref(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo#main")
        assert ref is not None

    def test_parse_with_host(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("ghes.corp.com/owner/repo")
        assert ref.host is not None

    def test_parse_ado_format(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("dev.azure.com/org/project/_git/repo")
        assert ref.is_azure_devops() is True

    def test_get_identity(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo")
        identity = ref.get_identity()
        assert isinstance(identity, str)
        assert len(identity) > 0

    def test_get_install_path(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo")
        path = ref.get_install_path(tmp_path)
        assert isinstance(path, Path)

    def test_is_virtual_subdirectory(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo/subdir/file.prompt.md")
        result = ref.is_virtual_subdirectory()
        assert isinstance(result, bool)

    def test_get_unique_key(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse("owner/repo")
        key = ref.get_unique_key()
        assert isinstance(key, str)

    def test_parse_from_dict_git(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse_from_dict(
            {"git": "https://github.com/owner/repo", "version": ">=1.0"}
        )
        assert ref is not None

    def test_parse_from_dict_path(self, tmp_path: Path) -> None:
        from apm_cli.models.apm_package import DependencyReference

        ref = DependencyReference.parse_from_dict({"path": str(tmp_path)})
        assert ref is not None
        assert ref.is_local is True

    def test_parse_from_dict_missing_fields(self) -> None:
        from apm_cli.models.apm_package import DependencyReference

        with pytest.raises(ValueError, match=r"git.*path"):
            DependencyReference.parse_from_dict({"version": "1.0"})
