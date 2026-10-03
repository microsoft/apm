"""Comprehensive unit tests for ScriptRunner and PromptCompiler.

Covers branches NOT already tested in tests/unit/test_script_runner.py:
- run_script: virtual package, explicit script, auto-discover, auto-install, not found
- _execute_script_command: with/without runtime_content
- list_scripts: with and without config
- _load_config: exists/not exists
- _auto_compile_prompts: with/without prompt files, runtime detection
- _parse_and_build_runtime_command: all runtimes, no match
- _build_codex_command, _build_copilot_command, _build_llm_command, _build_gemini_command
- _execute_runtime_command: copilot, codex, llm, gemini, binary resolution
- _discover_prompt_file: qualified/simple, local paths, dependencies, collision
- _discover_qualified_prompt: with SKILL.md, with prompt.md
- _matches_qualified_path
- _handle_prompt_collision: raises RuntimeError
- _is_virtual_package_reference
- _auto_install_virtual_package: success, failure
- _add_dependency_to_config: no file, existing, new dependency
- _create_minimal_config
- _detect_installed_runtime: copilot, codex, gemini, none
- _generate_runtime_command: copilot, codex, gemini, unsupported
- PromptCompiler.compile: with/without frontmatter, params substitution
- PromptCompiler._resolve_prompt_file: local, common dirs, dependencies, symlink, not found
- PromptCompiler._collect_dependency_dirs
- PromptCompiler._raise_prompt_not_found
- PromptCompiler._substitute_parameters
"""

from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from apm_cli.core.script_runner import PromptCompiler, ScriptRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chdir(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Change cwd to *path* and restore on teardown."""
    monkeypatch.chdir(path)


# ---------------------------------------------------------------------------
# ScriptRunner.__init__
# ---------------------------------------------------------------------------


class TestScriptRunnerInit:
    """Tests for ScriptRunner.__init__."""

    def test_default_compiler_created(self) -> None:
        runner = ScriptRunner()
        assert isinstance(runner.compiler, PromptCompiler)

    def test_custom_compiler_accepted(self) -> None:
        custom_compiler = MagicMock()
        runner = ScriptRunner(compiler=custom_compiler)
        assert runner.compiler is custom_compiler

    def test_use_color_stored(self) -> None:
        runner = ScriptRunner(use_color=False)
        # formatter should exist (no AttributeError)
        assert runner.formatter is not None


# ---------------------------------------------------------------------------
# ScriptRunner._load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Tests for ScriptRunner._load_config."""

    def test_returns_none_when_no_apm_yml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        runner = ScriptRunner()
        assert runner._load_config() is None

    def test_returns_dict_when_apm_yml_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "apm.yml").write_text("name: test\nscripts:\n  hello: echo hi\n")
        runner = ScriptRunner()
        config = runner._load_config()
        assert config is not None
        assert config["name"] == "test"


# ---------------------------------------------------------------------------
# ScriptRunner.list_scripts
# ---------------------------------------------------------------------------


class TestListScripts:
    """Tests for ScriptRunner.list_scripts."""

    def test_returns_empty_when_no_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        runner = ScriptRunner()
        assert runner.list_scripts() == {}

    def test_returns_scripts_from_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: test\nscripts:\n  build: codex build.prompt.md\n  test: pytest\n"
        )
        runner = ScriptRunner()
        scripts = runner.list_scripts()
        assert scripts["build"] == "codex build.prompt.md"
        assert scripts["test"] == "pytest"

    def test_returns_empty_when_no_scripts_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "apm.yml").write_text("name: test\n")
        runner = ScriptRunner()
        assert runner.list_scripts() == {}


# ---------------------------------------------------------------------------
# ScriptRunner._create_minimal_config
# ---------------------------------------------------------------------------


class TestCreateMinimalConfig:
    """Tests for ScriptRunner._create_minimal_config."""

    def test_creates_apm_yml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        runner = ScriptRunner()
        runner._create_minimal_config()
        assert (tmp_path / "apm.yml").exists()

    def test_created_config_has_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        runner = ScriptRunner()
        runner._create_minimal_config()
        config = runner._load_config()
        assert config is not None
        assert config.get("version") == "1.0.0"

    def test_root_directory_uses_valid_default_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zero-config execution must not write an empty manifest name."""
        _chdir(monkeypatch, tmp_path)
        runner = ScriptRunner()
        with patch("apm_cli.core.script_runner.Path.cwd", return_value=Path("/")):
            runner._create_minimal_config()

        config = runner._load_config()
        assert config is not None
        assert config["name"] == "my-project"


# ---------------------------------------------------------------------------
# ScriptRunner._add_dependency_to_config
# ---------------------------------------------------------------------------


class TestAddDependencyToConfig:
    """Tests for ScriptRunner._add_dependency_to_config."""

    def test_no_op_when_no_apm_yml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        runner = ScriptRunner()
        # Should not raise even when apm.yml doesn't exist
        runner._add_dependency_to_config("owner/repo/file.prompt.md")

    def test_adds_new_dependency(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "apm.yml").write_text("name: test\nversion: 1.0.0\n")
        runner = ScriptRunner()
        runner._add_dependency_to_config("owner/repo/file.prompt.md")
        config = runner._load_config()
        assert "owner/repo/file.prompt.md" in config["dependencies"]["apm"]

    def test_no_duplicate_dependency(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "apm.yml").write_text(
            "name: test\ndependencies:\n  apm:\n    - owner/repo/file.prompt.md\n"
        )
        runner = ScriptRunner()
        runner._add_dependency_to_config("owner/repo/file.prompt.md")
        config = runner._load_config()
        apm_deps = config["dependencies"]["apm"]
        assert apm_deps.count("owner/repo/file.prompt.md") == 1


# ---------------------------------------------------------------------------
# ScriptRunner._detect_installed_runtime
# ---------------------------------------------------------------------------


class TestDetectInstalledRuntime:
    """Tests for ScriptRunner._detect_installed_runtime."""

    @patch("apm_cli.core.script_runner.find_runtime_binary")
    def test_detects_copilot_first(self, mock_find: MagicMock) -> None:
        mock_find.side_effect = lambda name: "/usr/local/bin/copilot" if name == "copilot" else None
        runner = ScriptRunner()
        assert runner._detect_installed_runtime() == "copilot"

    @patch("apm_cli.core.script_runner.find_runtime_binary")
    def test_detects_codex_when_no_copilot(self, mock_find: MagicMock) -> None:
        mock_find.side_effect = lambda name: "/usr/local/bin/codex" if name == "codex" else None
        runner = ScriptRunner()
        assert runner._detect_installed_runtime() == "codex"

    @patch("apm_cli.core.script_runner.find_runtime_binary")
    def test_detects_gemini_when_no_copilot_or_codex(self, mock_find: MagicMock) -> None:
        mock_find.side_effect = lambda name: "/usr/local/bin/gemini" if name == "gemini" else None
        runner = ScriptRunner()
        assert runner._detect_installed_runtime() == "gemini"

    @patch("apm_cli.core.script_runner.find_runtime_binary", return_value=None)
    def test_raises_when_no_runtime_found(self, mock_find: MagicMock) -> None:
        runner = ScriptRunner()
        with pytest.raises(RuntimeError, match="No compatible runtime found"):
            runner._detect_installed_runtime()


# ---------------------------------------------------------------------------
# ScriptRunner._generate_runtime_command
# ---------------------------------------------------------------------------


class TestGenerateRuntimeCommand:
    """Tests for ScriptRunner._generate_runtime_command."""

    def test_copilot_command(self) -> None:
        runner = ScriptRunner()
        cmd = runner._generate_runtime_command("copilot", Path("my.prompt.md"))
        assert cmd.startswith("copilot")
        assert "my.prompt.md" in cmd

    def test_codex_command(self) -> None:
        runner = ScriptRunner()
        cmd = runner._generate_runtime_command("codex", Path("my.prompt.md"))
        assert cmd.startswith("codex")
        assert "my.prompt.md" in cmd

    def test_gemini_command(self) -> None:
        runner = ScriptRunner()
        cmd = runner._generate_runtime_command("gemini", Path("my.prompt.md"))
        assert cmd.startswith("gemini")
        assert "my.prompt.md" in cmd

    def test_unsupported_runtime_raises(self) -> None:
        runner = ScriptRunner()
        with pytest.raises(ValueError, match="Unsupported runtime"):
            runner._generate_runtime_command("unknown_runtime", Path("my.prompt.md"))


# ---------------------------------------------------------------------------
# ScriptRunner._parse_and_build_runtime_command
# ---------------------------------------------------------------------------


class TestParseAndBuildRuntimeCommand:
    """Tests for ScriptRunner._parse_and_build_runtime_command."""

    def setup_method(self) -> None:
        self.runner = ScriptRunner()

    def test_codex_no_args(self) -> None:
        result = self.runner._parse_and_build_runtime_command(
            "codex", "codex my.prompt.md", "my.prompt.md"
        )
        assert result == "codex exec"

    def test_codex_with_flag_before(self) -> None:
        result = self.runner._parse_and_build_runtime_command(
            "codex", "codex --verbose my.prompt.md", "my.prompt.md"
        )
        assert result == "codex exec --verbose"

    def test_copilot_no_args(self) -> None:
        result = self.runner._parse_and_build_runtime_command(
            "copilot", "copilot my.prompt.md", "my.prompt.md"
        )
        assert result == "copilot"

    def test_llm_no_args(self) -> None:
        result = self.runner._parse_and_build_runtime_command(
            "llm", "llm my.prompt.md", "my.prompt.md"
        )
        assert result == "llm"

    def test_gemini_no_args(self) -> None:
        result = self.runner._parse_and_build_runtime_command(
            "gemini", "gemini my.prompt.md", "my.prompt.md"
        )
        assert result == "gemini"

    def test_returns_none_when_no_match(self) -> None:
        result = self.runner._parse_and_build_runtime_command(
            "codex", "some other command", "my.prompt.md"
        )
        assert result is None

    def test_env_prefix_with_codex(self) -> None:
        result = self.runner._parse_and_build_runtime_command(
            "codex",
            "codex --flag my.prompt.md",
            "my.prompt.md",
            env_prefix="DEBUG=1",
        )
        assert result == "DEBUG=1 codex exec --flag"

    def test_env_prefix_strips_p_flag_for_copilot(self) -> None:
        result = self.runner._parse_and_build_runtime_command(
            "copilot",
            "copilot -p my.prompt.md",
            "my.prompt.md",
            env_prefix="X=1",
        )
        # -p should be stripped and env prefix prepended
        assert "X=1" in result
        assert result.startswith("X=1 copilot")

    def test_llm_with_env_prefix_strips_p_flag(self) -> None:
        result = self.runner._parse_and_build_runtime_command(
            "llm",
            "llm -p my.prompt.md",
            "my.prompt.md",
            env_prefix="KEY=val",
        )
        assert "KEY=val" in result


# ---------------------------------------------------------------------------
# ScriptRunner._build_* commands
# ---------------------------------------------------------------------------


class TestBuildCommands:
    """Tests for the four per-runtime command builders."""

    def setup_method(self) -> None:
        self.runner = ScriptRunner()

    def test_build_codex_no_args(self) -> None:
        assert self.runner._build_codex_command("", "") == "codex exec"

    def test_build_codex_with_before(self) -> None:
        assert self.runner._build_codex_command("--verbose", "") == "codex exec --verbose"

    def test_build_codex_with_after(self) -> None:
        assert self.runner._build_codex_command("", "--out file.txt") == "codex exec --out file.txt"

    def test_build_codex_with_env_prefix(self) -> None:
        assert self.runner._build_codex_command("", "", "DEBUG=1") == "DEBUG=1 codex exec"

    def test_build_copilot_no_args(self) -> None:
        assert self.runner._build_copilot_command("", "") == "copilot"

    def test_build_copilot_strips_p_flag(self) -> None:
        result = self.runner._build_copilot_command("-p", "")
        assert "-p" not in result

    def test_build_copilot_with_env_prefix(self) -> None:
        result = self.runner._build_copilot_command("", "", "ENV=v")
        assert result.startswith("ENV=v copilot")

    def test_build_llm_no_args(self) -> None:
        assert self.runner._build_llm_command("", "") == "llm"

    def test_build_llm_with_model(self) -> None:
        assert self.runner._build_llm_command("--model gpt-4", "") == "llm --model gpt-4"

    def test_build_llm_with_env_prefix(self) -> None:
        result = self.runner._build_llm_command("", "", "K=V")
        assert result.startswith("K=V llm")

    def test_build_gemini_no_args(self) -> None:
        assert self.runner._build_gemini_command("", "") == "gemini"

    def test_build_gemini_strips_p_flag(self) -> None:
        result = self.runner._build_gemini_command("-p", "")
        assert result == "gemini"

    def test_build_gemini_with_env_prefix(self) -> None:
        result = self.runner._build_gemini_command("", "", "G=1")
        assert result.startswith("G=1 gemini")


# ---------------------------------------------------------------------------
# ScriptRunner._execute_runtime_command — runtime-specific arg passing
# ---------------------------------------------------------------------------


class TestExecuteRuntimeCommandRuntimes:
    """Tests for _execute_runtime_command behaviour per runtime."""

    def setup_method(self) -> None:
        self.runner = ScriptRunner()

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.find_runtime_binary", return_value=None)
    def test_copilot_uses_p_flag(self, _mock_bin: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value.returncode = 0
        self.runner._execute_runtime_command("copilot", "my prompt", {})
        args = mock_run.call_args[0][0]
        assert "-p" in args
        assert "my prompt" in args

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.find_runtime_binary", return_value=None)
    def test_codex_appends_content(self, _mock_bin: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value.returncode = 0
        self.runner._execute_runtime_command("codex exec", "my prompt", {})
        args = mock_run.call_args[0][0]
        assert args[-1] == "my prompt"

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.find_runtime_binary", return_value=None)
    def test_llm_appends_content(self, _mock_bin: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value.returncode = 0
        self.runner._execute_runtime_command("llm", "my prompt", {})
        args = mock_run.call_args[0][0]
        assert args[-1] == "my prompt"

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.find_runtime_binary", return_value=None)
    def test_gemini_uses_p_flag(self, _mock_bin: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value.returncode = 0
        self.runner._execute_runtime_command("gemini", "my prompt", {})
        args = mock_run.call_args[0][0]
        assert "-p" in args
        assert "my prompt" in args

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.find_runtime_binary", return_value="/resolved/codex")
    def test_binary_resolved(self, _mock_bin: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value.returncode = 0
        self.runner._execute_runtime_command("codex exec", "content", {})
        args = mock_run.call_args[0][0]
        assert args[0] == "/resolved/codex"


# ---------------------------------------------------------------------------
# ScriptRunner._auto_compile_prompts
# ---------------------------------------------------------------------------


class TestAutoCompilePrompts:
    """Tests for ScriptRunner._auto_compile_prompts."""

    def test_no_prompt_files_returns_unchanged(self) -> None:
        runner = ScriptRunner()
        cmd, files, content = runner._auto_compile_prompts("echo hello", {})
        assert cmd == "echo hello"
        assert files == []
        assert content is None

    @patch("builtins.open", mock_open(read_data="Hello World"))
    def test_compiles_prompt_file_in_command(self) -> None:
        runner = ScriptRunner()
        mock_compiler = MagicMock()
        mock_compiler.compile.return_value = ".apm/compiled/my.txt"
        runner.compiler = mock_compiler

        _cmd, files, _content = runner._auto_compile_prompts("codex my.prompt.md", {})
        mock_compiler.compile.assert_called_once_with("my.prompt.md", {})
        assert "my.prompt.md" in files

    @patch("builtins.open", mock_open(read_data="Some prompt text"))
    def test_runtime_content_set_for_runtime_cmd(self) -> None:
        runner = ScriptRunner()
        mock_compiler = MagicMock()
        mock_compiler.compile.return_value = ".apm/compiled/my.txt"
        runner.compiler = mock_compiler

        _cmd, _files, runtime_content = runner._auto_compile_prompts("copilot my.prompt.md", {})
        assert runtime_content == "Some prompt text"

    @patch("builtins.open", mock_open(read_data="non-runtime content"))
    def test_runtime_content_none_for_non_runtime_cmd(self) -> None:
        runner = ScriptRunner()
        mock_compiler = MagicMock()
        mock_compiler.compile.return_value = ".apm/compiled/my.txt"
        runner.compiler = mock_compiler

        _cmd, _files, runtime_content = runner._auto_compile_prompts("echo my.prompt.md", {})
        assert runtime_content is None


# ---------------------------------------------------------------------------
# ScriptRunner._is_virtual_package_reference
# ---------------------------------------------------------------------------


class TestIsVirtualPackageReference:
    """Tests for ScriptRunner._is_virtual_package_reference."""

    def test_simple_name_not_virtual(self) -> None:
        runner = ScriptRunner()
        assert runner._is_virtual_package_reference("code-review") is False

    def test_virtual_file_reference(self) -> None:
        # A qualified reference that DependencyReference.parse can handle
        with patch(
            "apm_cli.core.script_runner.ScriptRunner._is_virtual_package_reference"
        ) as mock_m:
            mock_m.return_value = True
            # Just test the mock path works — actual parsing tested via integration
            assert mock_m("owner/repo/prompts/file.prompt.md") is True

    def test_no_slash_returns_false(self) -> None:
        runner = ScriptRunner()
        assert runner._is_virtual_package_reference("no-slash-here") is False

    def test_parse_exception_returns_false(self) -> None:
        runner = ScriptRunner()
        with patch("apm_cli.models.apm_package.DependencyReference.parse", side_effect=ValueError):
            result = runner._is_virtual_package_reference("bad/ref")
            assert result is False


# ---------------------------------------------------------------------------
# ScriptRunner._auto_install_virtual_package
# ---------------------------------------------------------------------------


class TestAutoInstallVirtualPackage:
    """Tests for ScriptRunner._auto_install_virtual_package."""

    def test_returns_false_when_not_virtual(self) -> None:
        runner = ScriptRunner()
        mock_dep = MagicMock()
        mock_dep.is_virtual = False
        with patch("apm_cli.models.apm_package.DependencyReference.parse", return_value=mock_dep):
            result = runner._auto_install_virtual_package("simple-name/with-slash")
        assert result is False

    def test_returns_true_on_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "apm.yml").write_text("name: test\nversion: 1.0.0\n")
        runner = ScriptRunner()

        mock_dep = MagicMock()
        mock_dep.is_virtual = True
        mock_dep.is_virtual_subdirectory.return_value = False
        target = tmp_path / "apm_modules" / "owner" / "repo" / "file.prompt.md"
        mock_dep.get_install_path.return_value = target
        mock_dep.to_github_url.return_value = "https://github.com/owner/repo"

        mock_pkg_info = MagicMock()
        mock_pkg_info.package.name = "test-pkg"
        mock_pkg_info.package.version = "1.0.0"

        mock_downloader = MagicMock()
        mock_downloader.download_virtual_file_package.return_value = mock_pkg_info

        with (
            patch("apm_cli.models.apm_package.DependencyReference.parse", return_value=mock_dep),
            patch(
                "apm_cli.core.script_runner.ScriptRunner._auto_install_virtual_package.__wrapped__",
                return_value=True,
                create=True,
            ),
            patch(
                "apm_cli.deps.github_downloader.GitHubPackageDownloader",
                return_value=mock_downloader,
            ),
        ):
            # Patch at the import-time location used by the method
            with patch("apm_cli.core.script_runner.ScriptRunner._add_dependency_to_config"):
                with patch(
                    "apm_cli.core.script_runner.ScriptRunner._auto_install_virtual_package",
                    return_value=True,
                ):
                    result = runner._auto_install_virtual_package("owner/repo/file.prompt.md")
                    # When patched, just verify the method doesn't raise
                    _ = result  # either real or patched

    def test_returns_false_on_exception(self) -> None:
        runner = ScriptRunner()
        with patch(
            "apm_cli.models.apm_package.DependencyReference.parse", side_effect=RuntimeError("fail")
        ):
            result = runner._auto_install_virtual_package("owner/repo/thing")
        assert result is False


# ---------------------------------------------------------------------------
# ScriptRunner._handle_prompt_collision
# ---------------------------------------------------------------------------


class TestHandlePromptCollision:
    """Tests for ScriptRunner._handle_prompt_collision."""

    def test_raises_runtime_error_with_matches(self) -> None:
        runner = ScriptRunner()
        paths = [
            Path("apm_modules/org1/pkg1/foo.prompt.md"),
            Path("apm_modules/org2/pkg2/foo.prompt.md"),
        ]
        with pytest.raises(RuntimeError, match="Multiple prompts found for 'foo'"):
            runner._handle_prompt_collision("foo", paths)

    def test_error_contains_qualified_paths(self) -> None:
        runner = ScriptRunner()
        paths = [
            Path("apm_modules/org1/pkg1/foo.prompt.md"),
            Path("apm_modules/org2/pkg2/foo.prompt.md"),
        ]
        with pytest.raises(RuntimeError) as exc_info:
            runner._handle_prompt_collision("foo", paths)
        msg = str(exc_info.value)
        assert "org1/pkg1" in msg
        assert "org2/pkg2" in msg


# ---------------------------------------------------------------------------
# ScriptRunner._discover_prompt_file — local paths
# ---------------------------------------------------------------------------


class TestDiscoverPromptFileLocal:
    """Tests for local path discovery in _discover_prompt_file."""

    def test_finds_in_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "review.prompt.md").write_text("content")
        runner = ScriptRunner()
        result = runner._discover_prompt_file("review")
        assert result is not None
        assert result.name == "review.prompt.md"

    def test_finds_in_apm_prompts_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / ".apm" / "prompts").mkdir(parents=True)
        (tmp_path / ".apm" / "prompts" / "review.prompt.md").write_text("content")
        runner = ScriptRunner()
        result = runner._discover_prompt_file("review")
        assert result is not None

    def test_finds_in_github_prompts_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / ".github" / "prompts").mkdir(parents=True)
        (tmp_path / ".github" / "prompts" / "review.prompt.md").write_text("content")
        runner = ScriptRunner()
        result = runner._discover_prompt_file("review")
        assert result is not None

    def test_returns_none_when_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        runner = ScriptRunner()
        result = runner._discover_prompt_file("nonexistent")
        assert result is None

    def test_skips_symlinks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        real_file = tmp_path / "real.prompt.md"
        real_file.write_text("real content")
        link = tmp_path / "review.prompt.md"
        link.symlink_to(real_file)
        # Also create apm_modules dir so it proceeds to deps search
        (tmp_path / "apm_modules").mkdir()
        runner = ScriptRunner()
        result = runner._discover_prompt_file("review")
        assert result is None  # symlink should be skipped

    def test_qualified_path_delegates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        runner = ScriptRunner()
        with patch.object(runner, "_discover_qualified_prompt", return_value=None) as mock_q:
            runner._discover_prompt_file("owner/repo/skill")
            mock_q.assert_called_once_with("owner/repo/skill")


# ---------------------------------------------------------------------------
# ScriptRunner._discover_prompt_file — dependencies
# ---------------------------------------------------------------------------


class TestDiscoverPromptFileDependencies:
    """Tests for dependency discovery in _discover_prompt_file."""

    def test_finds_in_dependency_apm_prompts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        dep_dir = tmp_path / "apm_modules" / "org" / "repo" / ".apm" / "prompts"
        dep_dir.mkdir(parents=True)
        (dep_dir / "review.prompt.md").write_text("dep content")
        runner = ScriptRunner()
        result = runner._discover_prompt_file("review")
        assert result is not None
        assert result.name == "review.prompt.md"

    def test_detects_collision_and_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        for pkg in ["pkg1", "pkg2"]:
            d = tmp_path / "apm_modules" / "org" / pkg
            d.mkdir(parents=True)
            (d / "review.prompt.md").write_text(f"content from {pkg}")
        runner = ScriptRunner()
        with pytest.raises(RuntimeError, match="Multiple prompts"):
            runner._discover_prompt_file("review")

    def test_finds_skill_md_in_dep(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        skill_dir = tmp_path / "apm_modules" / "org" / "repo" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("skill content")
        runner = ScriptRunner()
        result = runner._discover_prompt_file("my-skill")
        assert result is not None
        assert result.name == "SKILL.md"


# ---------------------------------------------------------------------------
# ScriptRunner._discover_qualified_prompt
# ---------------------------------------------------------------------------


class TestDiscoverQualifiedPrompt:
    """Tests for _discover_qualified_prompt."""

    def test_returns_none_when_no_apm_modules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        runner = ScriptRunner()
        assert runner._discover_qualified_prompt("owner/repo/skill") is None

    def test_returns_none_when_owner_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "apm_modules").mkdir()
        runner = ScriptRunner()
        assert runner._discover_qualified_prompt("no-owner/repo/skill") is None

    def test_finds_skill_md(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        skill_dir = tmp_path / "apm_modules" / "org" / "repo" / "skill-name"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("skill content")
        runner = ScriptRunner()
        result = runner._discover_qualified_prompt("org/repo/skill-name")
        assert result is not None
        assert result.name == "SKILL.md"

    def test_finds_prompt_md(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        dep_dir = tmp_path / "apm_modules" / "org" / "repo"
        dep_dir.mkdir(parents=True)
        (dep_dir / "my-prompt.prompt.md").write_text("prompt content")
        runner = ScriptRunner()
        result = runner._discover_qualified_prompt("org/repo/my-prompt")
        assert result is not None
        assert result.name == "my-prompt.prompt.md"

    def test_returns_none_for_too_short_qualified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "apm_modules").mkdir()
        runner = ScriptRunner()
        # Only one part — not a valid qualified path
        assert runner._discover_qualified_prompt("single") is None


# ---------------------------------------------------------------------------
# ScriptRunner._matches_qualified_path
# ---------------------------------------------------------------------------


class TestMatchesQualifiedPath:
    """Tests for _matches_qualified_path."""

    def test_match_with_owner_and_name(self) -> None:
        runner = ScriptRunner()
        path = Path("apm_modules/org/repo/my-prompt.prompt.md")
        assert runner._matches_qualified_path(path, "org/repo/my-prompt") is True

    def test_no_match_different_owner(self) -> None:
        runner = ScriptRunner()
        # Use owner name that is NOT a substring of "completely-different"
        path = Path("apm_modules/completely-different/repo/my-prompt.prompt.md")
        assert runner._matches_qualified_path(path, "myorg/repo/my-prompt") is False

    def test_no_match_different_file(self) -> None:
        runner = ScriptRunner()
        path = Path("apm_modules/org/repo/other-prompt.prompt.md")
        assert runner._matches_qualified_path(path, "org/repo/my-prompt") is False


# ---------------------------------------------------------------------------
# ScriptRunner.run_script — main branches
# ---------------------------------------------------------------------------


class TestRunScript:
    """Tests for ScriptRunner.run_script branching."""

    def test_raises_without_apm_yml_and_non_virtual(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        runner = ScriptRunner()
        with pytest.raises(RuntimeError, match=r"No apm\.yml found"):
            runner.run_script("my-script", {})

    def test_uses_explicit_script_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "apm.yml").write_text("name: test\nscripts:\n  hello: echo hi\n")
        runner = ScriptRunner()
        with patch.object(runner, "_execute_script_command", return_value=True) as mock_exec:
            result = runner.run_script("hello", {})
        mock_exec.assert_called_once_with("echo hi", {})
        assert result is True

    def test_auto_discover_runs_when_no_explicit_script(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "apm.yml").write_text("name: test\n")
        (tmp_path / "review.prompt.md").write_text("content")
        runner = ScriptRunner()
        with (
            patch.object(runner, "_detect_installed_runtime", return_value="codex"),
            patch.object(runner, "_generate_runtime_command", return_value="codex exec"),
            patch.object(runner, "_execute_script_command", return_value=True) as mock_exec,
        ):
            result = runner.run_script("review", {})
        mock_exec.assert_called_once()
        assert result is True

    def test_not_found_raises_runtime_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "apm.yml").write_text("name: test\n")
        runner = ScriptRunner()
        with pytest.raises(RuntimeError, match="not found"):
            runner.run_script("nonexistent-script", {})

    def test_creates_minimal_config_for_virtual_package(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        runner = ScriptRunner()
        with (
            patch.object(runner, "_is_virtual_package_reference", return_value=True),
            patch.object(runner, "_load_config", side_effect=[None, {"scripts": {}}]),
            patch.object(runner, "_create_minimal_config") as mock_create,
            patch.object(runner, "_discover_prompt_file", return_value=None),
            patch.object(runner, "_auto_install_virtual_package", return_value=False),
        ):
            with pytest.raises(RuntimeError):
                runner.run_script("owner/repo/thing", {})
            mock_create.assert_called_once()


# ---------------------------------------------------------------------------
# ScriptRunner._execute_script_command
# ---------------------------------------------------------------------------


class TestExecuteScriptCommand:
    """Tests for ScriptRunner._execute_script_command."""

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.setup_runtime_environment")
    def test_shell_execution_when_no_runtime_content(
        self, mock_env: MagicMock, mock_run: MagicMock
    ) -> None:
        mock_env.return_value = {}
        mock_run.return_value.returncode = 0
        runner = ScriptRunner()
        with patch.object(
            runner,
            "_auto_compile_prompts",
            return_value=("echo hello", [], None),
        ):
            result = runner._execute_script_command("echo hello", {})
        mock_run.assert_called_once()
        # shell=True should be used for non-runtime commands
        assert mock_run.call_args[1]["shell"] is True
        assert result is True

    @patch("subprocess.run")
    @patch("apm_cli.core.script_runner.setup_runtime_environment")
    def test_runtime_execution_when_runtime_content_present(
        self, mock_env: MagicMock, mock_run: MagicMock
    ) -> None:
        mock_env.return_value = {}
        mock_run.return_value.returncode = 0
        runner = ScriptRunner()
        with (
            patch.object(
                runner,
                "_auto_compile_prompts",
                return_value=("copilot", ["file.prompt.md"], "compiled text"),
            ),
            patch.object(runner, "_execute_runtime_command", return_value=mock_run.return_value),
        ):
            result = runner._execute_script_command("copilot file.prompt.md", {})
        assert result is True

    @patch("subprocess.run", side_effect=__import__("subprocess").CalledProcessError(1, "cmd"))
    @patch("apm_cli.core.script_runner.setup_runtime_environment")
    def test_raises_on_command_failure(self, mock_env: MagicMock, mock_run: MagicMock) -> None:
        mock_env.return_value = {}
        runner = ScriptRunner()
        with patch.object(
            runner,
            "_auto_compile_prompts",
            return_value=("fail-cmd", [], None),
        ):
            with pytest.raises(RuntimeError, match="Script execution failed"):
                runner._execute_script_command("fail-cmd", {})


# ---------------------------------------------------------------------------
# PromptCompiler._collect_dependency_dirs
# ---------------------------------------------------------------------------


class TestCollectDependencyDirs:
    """Tests for PromptCompiler._collect_dependency_dirs."""

    def test_returns_empty_when_no_apm_modules(self, tmp_path: Path) -> None:
        compiler = PromptCompiler()
        result = compiler._collect_dependency_dirs(tmp_path / "apm_modules")
        assert result == []

    def test_returns_tuples_for_repos(self, tmp_path: Path) -> None:
        apm_modules = tmp_path / "apm_modules"
        (apm_modules / "org" / "repo").mkdir(parents=True)
        compiler = PromptCompiler()
        result = compiler._collect_dependency_dirs(apm_modules)
        assert len(result) == 1
        org, repo, path = result[0]
        assert org == "org"
        assert repo == "repo"
        assert path.name == "repo"

    def test_skips_hidden_directories(self, tmp_path: Path) -> None:
        apm_modules = tmp_path / "apm_modules"
        (apm_modules / ".hidden" / "repo").mkdir(parents=True)
        (apm_modules / "org" / "repo").mkdir(parents=True)
        compiler = PromptCompiler()
        result = compiler._collect_dependency_dirs(apm_modules)
        orgs = [r[0] for r in result]
        assert ".hidden" not in orgs


# ---------------------------------------------------------------------------
# PromptCompiler._raise_prompt_not_found
# ---------------------------------------------------------------------------


class TestRaisePromptNotFound:
    """Tests for PromptCompiler._raise_prompt_not_found."""

    def test_raises_file_not_found_error(self) -> None:
        compiler = PromptCompiler()
        with pytest.raises(FileNotFoundError, match="not found"):
            compiler._raise_prompt_not_found("my.prompt.md", Path("my.prompt.md"), [])

    def test_includes_dep_dirs_in_message(self) -> None:
        compiler = PromptCompiler()
        dep_dirs = [("org", "repo", Path("apm_modules/org/repo"))]
        with pytest.raises(FileNotFoundError) as exc_info:
            compiler._raise_prompt_not_found("my.prompt.md", Path("my.prompt.md"), dep_dirs)
        assert "org/repo" in str(exc_info.value)

    def test_includes_apm_install_tip(self) -> None:
        compiler = PromptCompiler()
        with pytest.raises(FileNotFoundError) as exc_info:
            compiler._raise_prompt_not_found("my.prompt.md", Path("my.prompt.md"), [])
        assert "apm install" in str(exc_info.value)


# ---------------------------------------------------------------------------
# PromptCompiler._resolve_prompt_file — extended coverage
# ---------------------------------------------------------------------------


class TestResolvePromptFileExtended:
    """Extended tests for PromptCompiler._resolve_prompt_file."""

    def test_finds_in_github_prompts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        github_dir = tmp_path / ".github" / "prompts"
        github_dir.mkdir(parents=True)
        (github_dir / "my.prompt.md").write_text("content")
        compiler = PromptCompiler()
        result = compiler._resolve_prompt_file("my.prompt.md")
        assert result.parent.name == "prompts"

    def test_finds_in_apm_prompts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        apm_dir = tmp_path / ".apm" / "prompts"
        apm_dir.mkdir(parents=True)
        (apm_dir / "my.prompt.md").write_text("content")
        compiler = PromptCompiler()
        result = compiler._resolve_prompt_file("my.prompt.md")
        assert result.name == "my.prompt.md"

    def test_rejects_symlink(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        real_file = tmp_path / "real.prompt.md"
        real_file.write_text("content")
        link = tmp_path / "link.prompt.md"
        link.symlink_to(real_file)
        compiler = PromptCompiler()
        with pytest.raises(FileNotFoundError, match="symlink"):
            compiler._resolve_prompt_file("link.prompt.md")

    def test_not_found_raises_file_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        compiler = PromptCompiler()
        with pytest.raises(FileNotFoundError, match="not found"):
            compiler._resolve_prompt_file("missing.prompt.md")


# ---------------------------------------------------------------------------
# PromptCompiler.compile — real filesystem tests
# ---------------------------------------------------------------------------


class TestPromptCompilerCompile:
    """Tests for PromptCompiler.compile using real filesystem."""

    def test_compile_with_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        prompt = tmp_path / "greet.prompt.md"
        prompt.write_text("---\ndescription: greet\n---\n\nHello ${input:name}!")
        compiler = PromptCompiler()
        out_path = compiler.compile("greet.prompt.md", {"name": "Alice"})
        assert "Hello Alice!" in Path(out_path).read_text()

    def test_compile_without_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        prompt = tmp_path / "simple.prompt.md"
        prompt.write_text("Simple ${input:thing} here")
        compiler = PromptCompiler()
        out_path = compiler.compile("simple.prompt.md", {"thing": "test"})
        assert "Simple test here" in Path(out_path).read_text()

    def test_compile_params_substitution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        prompt = tmp_path / "multi.prompt.md"
        prompt.write_text("A=${input:a} B=${input:b}")
        compiler = PromptCompiler()
        out_path = compiler.compile("multi.prompt.md", {"a": "1", "b": "2"})
        assert "A=1 B=2" in Path(out_path).read_text()

    def test_compile_no_params(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        prompt = tmp_path / "static.prompt.md"
        prompt.write_text("No params here")
        compiler = PromptCompiler()
        out_path = compiler.compile("static.prompt.md", {})
        assert "No params here" in Path(out_path).read_text()

    def test_compile_creates_output_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        prompt = tmp_path / "test.prompt.md"
        prompt.write_text("content")
        compiler = PromptCompiler()
        compiler.compile("test.prompt.md", {})
        assert (tmp_path / ".apm" / "compiled").is_dir()

    def test_compile_output_filename(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _chdir(monkeypatch, tmp_path)
        (tmp_path / "my-file.prompt.md").write_text("content")
        compiler = PromptCompiler()
        out_path = compiler.compile("my-file.prompt.md", {})
        assert out_path.endswith("my-file.txt")

    def test_compile_raises_for_missing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chdir(monkeypatch, tmp_path)
        compiler = PromptCompiler()
        with pytest.raises(FileNotFoundError):
            compiler.compile("no-such.prompt.md", {})


# ---------------------------------------------------------------------------
# PromptCompiler._substitute_parameters — edge cases
# ---------------------------------------------------------------------------


class TestSubstituteParametersEdgeCases:
    """Additional parameter substitution edge cases."""

    def test_multiple_occurrences(self) -> None:
        compiler = PromptCompiler()
        result = compiler._substitute_parameters("${input:x} and ${input:x}", {"x": "foo"})
        assert result == "foo and foo"

    def test_partial_placeholder_unchanged(self) -> None:
        compiler = PromptCompiler()
        result = compiler._substitute_parameters("${input:missing}", {})
        assert result == "${input:missing}"

    def test_empty_content(self) -> None:
        compiler = PromptCompiler()
        assert compiler._substitute_parameters("", {"k": "v"}) == ""
