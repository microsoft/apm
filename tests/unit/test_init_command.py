"""Tests for the apm init command."""

import json  # noqa: F401
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest  # noqa: F401
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli


class TestInitCommand:
    """Test cases for apm init command."""

    def setup_method(self):
        """Set up test fixtures."""
        self.runner = CliRunner()
        # Use a safe fallback directory if current directory is not accessible
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            # If current directory doesn't exist, use the repo root
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)

    def teardown_method(self):
        """Clean up after tests."""
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            # If original directory doesn't exist anymore, go to repo root
            repo_root = Path(__file__).parent.parent.parent
            os.chdir(str(repo_root))

    def test_init_current_directory(self):
        """Test initialization in current directory."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                result = self.runner.invoke(cli, ["init", "--yes"])

                assert result.exit_code == 0
                assert "APM project initialized successfully!" in result.output
                assert Path("apm.yml").exists()
                assert not Path("start.prompt.md").exists()
                # No extra template files created
                assert not Path("hello-world.prompt.md").exists()
                assert not Path("README.md").exists()
                assert not Path(".apm").exists()
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_explicit_current_directory(self):
        """Test initialization with explicit '.' argument."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                result = self.runner.invoke(cli, ["init", ".", "--yes"])

                assert result.exit_code == 0
                assert "APM project initialized successfully!" in result.output
                assert Path("apm.yml").exists()
                assert not Path("start.prompt.md").exists()
                # No extra template files created
                assert not Path("hello-world.prompt.md").exists()
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_new_directory(self):
        """Test initialization in new directory."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                result = self.runner.invoke(cli, ["init", "my-project", "--yes"])

                assert result.exit_code == 0
                assert "Created project directory: my-project" in result.output
                # Use absolute path to check files
                project_path = Path(tmp_dir) / "my-project"
                assert project_path.exists()
                assert project_path.is_dir()
                assert (project_path / "apm.yml").exists()
                assert not (project_path / "start.prompt.md").exists()
                # No extra template files created
                assert not (project_path / "hello-world.prompt.md").exists()
                assert not (project_path / "README.md").exists()
                assert not (project_path / ".apm").exists()
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_existing_project_without_force(self):
        """Test initialization over existing apm.yml without --force (removed flag)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                # Create existing apm.yml
                Path("apm.yml").write_text("name: existing-project\nversion: 0.1.0\n")

                # Try to init without interactive confirmation (should prompt)
                result = self.runner.invoke(cli, ["init", "--yes"])

                assert result.exit_code == 0
                assert "apm.yml already exists" in result.output
                assert "--yes specified, overwriting apm.yml..." in result.output
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_existing_project_with_force(self):
        """Test initialization over existing apm.yml (--force flag removed, behavior same as --yes)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                # Create existing apm.yml
                Path("apm.yml").write_text("name: existing-project\nversion: 0.1.0\n")

                result = self.runner.invoke(cli, ["init", "--yes"])

                assert result.exit_code == 0
                assert "APM project initialized successfully!" in result.output
                # Should overwrite the file with minimal structure
                with open("apm.yml", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                    # Minimal structure
                    assert "dependencies" in config
                    assert config["dependencies"] == {"apm": [], "mcp": []}
                    assert "scripts" in config
                    assert config["scripts"] == {}
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_preserves_existing_config(self):
        """Test that init with --yes overwrites existing apm.yml (no merge in minimal mode)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                # Create existing apm.yml with custom values
                existing_config = {
                    "name": "my-custom-project",
                    "version": "2.0.0",
                    "description": "Custom description",
                    "author": "Custom Author",
                }
                with open("apm.yml", "w", encoding="utf-8") as f:
                    yaml.dump(existing_config, f)

                result = self.runner.invoke(cli, ["init", "--yes"])

                assert result.exit_code == 0
                # Minimal mode: overwrites with auto-detected values
                assert "apm.yml already exists" in result.output
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_interactive_mode(self):
        """Test interactive mode with user input."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                # Simulate user input (includes target prompt: done + confirm empty)
                user_input = "my-test-project\n1.5.0\nTest description\nTest Author\ny\ndone\ny\n"

                result = self.runner.invoke(cli, ["init"], input=user_input)

                assert result.exit_code == 0
                assert "Setting up your APM project" in result.output
                assert "Project name" in result.output
                assert "Version" in result.output
                assert "Description" in result.output
                assert "Author" in result.output

                # Verify the interactive values were applied to apm.yml
                with open("apm.yml", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                    assert config["name"] == "my-test-project"
                    assert config["version"] == "1.5.0"
                    assert config["description"] == "Test description"
                    assert config["author"] == "Test Author"
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_interactive_mode_abort(self):
        """Test aborting interactive mode."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                # Simulate user input with 'no' to confirmation
                user_input = "my-test-project\n1.5.0\nTest description\nTest Author\nn\n"

                result = self.runner.invoke(cli, ["init"], input=user_input)

                assert result.exit_code == 0
                assert "Aborted" in result.output
                assert not Path("apm.yml").exists()
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_existing_project_interactive_cancel(self):
        """Test cancelling when existing apm.yml detected in interactive mode."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                # Create existing apm.yml
                Path("apm.yml").write_text("name: existing-project\nversion: 0.1.0\n")

                # Simulate user saying 'no' to overwrite
                result = self.runner.invoke(cli, ["init"], input="n\n")

                assert result.exit_code == 0
                assert "apm.yml already exists" in result.output
                assert "Initialization cancelled" in result.output
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_existing_project_confirm_prompt_shown_once(self):
        """Test that overwrite confirmation prompt appears exactly once (#602).

        On Windows CP950 terminals, Rich Confirm.ask() could fail on encoding,
        retry internally, then fall back to click.confirm(), showing the prompt
        three times. After the fix, only click.confirm() is used.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                # Create existing apm.yml
                Path("apm.yml").write_text("name: existing-project\nversion: 0.1.0\n")

                # Say yes to overwrite, then provide interactive setup input + target prompt
                user_input = "y\nmy-project\n1.0.0\nA description\nAuthor\ny\ndone\ny\n"
                result = self.runner.invoke(cli, ["init"], input=user_input)

                assert result.exit_code == 0
                # The overwrite prompt must appear exactly once
                assert result.output.count("Continue and overwrite?") == 1
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_existing_project_confirm_uses_click(self):
        """Test that overwrite confirmation uses click.confirm, not Rich (#602)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                # Create existing apm.yml
                Path("apm.yml").write_text("name: existing-project\nversion: 0.1.0\n")

                with patch(
                    "apm_cli.commands.init.click.confirm", return_value=True
                ) as mock_confirm:
                    result = self.runner.invoke(cli, ["init", "--yes"])
                    # --yes skips the prompt entirely, so confirm should NOT be called
                    mock_confirm.assert_not_called()

                with patch(
                    "apm_cli.commands.init.click.confirm", return_value=False
                ) as mock_confirm:
                    result = self.runner.invoke(cli, ["init"])
                    mock_confirm.assert_called_once_with("Continue and overwrite?")
                    assert "Initialization cancelled" in result.output
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_validates_project_structure(self):
        """Test that init creates expected project structure."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                result = self.runner.invoke(cli, ["init", "test-project", "--yes"])

                assert result.exit_code == 0

                # Use absolute path for checking files
                project_path = Path(tmp_dir) / "test-project"

                # Verify apm.yml minimal structure
                with open(project_path / "apm.yml", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                    assert config["name"] == "test-project"
                    assert "version" in config
                    assert "dependencies" in config
                    assert config["dependencies"] == {"apm": [], "mcp": []}
                    assert "scripts" in config
                    assert config["scripts"] == {}

                # start.prompt.md NOT created (apm init creates only apm.yml)
                assert not (project_path / "start.prompt.md").exists()
                # No extra template files created
                assert not (project_path / "hello-world.prompt.md").exists()
                assert not (project_path / "README.md").exists()
                assert not (project_path / ".apm").exists()
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_auto_detection(self):
        """Test auto-detection of project metadata."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                # Initialize git repo and set author
                import subprocess

                git_init = subprocess.run(["git", "init"], capture_output=True)
                assert git_init.returncode == 0, f"git init failed: {git_init.stderr}"

                git_config = subprocess.run(
                    ["git", "config", "user.name", "Test User"], capture_output=True
                )
                assert git_config.returncode == 0, f"git config failed: {git_config.stderr}"

                result = self.runner.invoke(cli, ["init", "--yes"])

                assert result.exit_code == 0

                with open("apm.yml", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                    # Should auto-detect author from git
                    assert config["author"] == "Test User"
                    # Should auto-detect description
                    assert "APM project" in config["description"]
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_does_not_create_skill_md(self):
        """Test that init does not create SKILL.md (only apm.yml)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                result = self.runner.invoke(cli, ["init", "--yes"])

                assert result.exit_code == 0
                assert Path("apm.yml").exists()
                assert not Path("SKILL.md").exists()
            finally:
                os.chdir(self.original_dir)  # restore CWD before TemporaryDirectory cleanup

    def test_init_next_steps_panel_content(self):
        """Test that next steps show install workflows, not apm run start."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                result = self.runner.invoke(cli, ["init", "--yes"])

                assert result.exit_code == 0
                # New v5 panel content
                assert "apm install" in result.output
                assert "apm pack" in result.output
                assert "https://microsoft.github.io/apm" in result.output
                # Old dead-end content must be gone
                assert "apm compile" not in result.output
                assert "apm run start" not in result.output
                assert "start.prompt.md" not in result.output
            finally:
                os.chdir(self.original_dir)

    def test_init_created_files_table_no_start_prompt(self):
        """Test that Created Files table does NOT list start.prompt.md."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            os.chdir(tmp_dir)
            try:
                result = self.runner.invoke(cli, ["init", "--yes"])

                assert result.exit_code == 0
                assert "apm.yml" in result.output
                assert "start.prompt.md" not in result.output
            finally:
                os.chdir(self.original_dir)


class TestPluginNameValidation:
    """Unit tests for _validate_plugin_name helper."""

    def test_valid_names(self):
        from apm_cli.commands._helpers import _validate_plugin_name

        assert _validate_plugin_name("a") is True
        assert _validate_plugin_name("my-plugin") is True
        assert _validate_plugin_name("plugin2") is True
        assert _validate_plugin_name("a" * 64) is True

    def test_invalid_names(self):
        from apm_cli.commands._helpers import _validate_plugin_name

        assert _validate_plugin_name("") is False
        assert _validate_plugin_name("A") is False
        assert _validate_plugin_name("my_plugin") is False
        assert _validate_plugin_name("1plugin") is False
        assert _validate_plugin_name("-plugin") is False
        assert _validate_plugin_name("a" * 65) is False
        assert _validate_plugin_name("My-Plugin") is False


class TestProjectNameValidation:
    """Unit tests for _validate_project_name helper."""

    def test_valid_names(self):
        from apm_cli.commands._helpers import _validate_project_name

        assert _validate_project_name("myproject") is True
        assert _validate_project_name("my-project") is True
        assert _validate_project_name("my_project") is True
        assert _validate_project_name("Project123") is True
        assert _validate_project_name("4") is True
        assert _validate_project_name(".") is True

    def test_invalid_forward_slash(self):
        from apm_cli.commands._helpers import _validate_project_name

        assert _validate_project_name("4/15") is False
        assert _validate_project_name("a/b") is False
        assert _validate_project_name("/leading") is False
        assert _validate_project_name("trailing/") is False

    def test_invalid_backslash(self):
        from apm_cli.commands._helpers import _validate_project_name

        bs = chr(92)  # one backslash character
        assert _validate_project_name("a" + bs + "b") is False
        assert _validate_project_name(bs + "leading") is False
        assert _validate_project_name("trailing" + bs) is False

    def test_invalid_dotdot(self):
        from apm_cli.commands._helpers import _validate_project_name

        assert _validate_project_name("..") is False

    def test_dotdot_in_slash_path_caught_by_slash_check(self):
        """Names like a/../b are caught by the slash check, not the dotdot check."""
        from apm_cli.commands._helpers import _validate_project_name

        assert _validate_project_name("a/../b") is False  # slash catches it


class TestInitProjectNameValidation:
    """Integration tests: apm init rejects project names with path separators or '..'."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_init_rejects_forward_slash_in_name(self):
        """apm init 4/15 must fail with a clear error, not a WinError."""
        with self.runner.isolated_filesystem():
            result = self.runner.invoke(cli, ["init", "4/15", "--yes"])
            assert result.exit_code != 0
            assert "Invalid project name" in result.output
            assert "4/15" in result.output
            assert not Path("4").exists()

    def test_init_rejects_backslash_in_name(self):
        """apm init with a backslash in the name must fail with a clear error."""
        bs = chr(92)
        with self.runner.isolated_filesystem():
            result = self.runner.invoke(cli, ["init", "a" + bs + "b", "--yes"])
            assert result.exit_code != 0
            assert "Invalid project name" in result.output
            assert bs in result.output

    def test_init_rejects_dotdot(self):
        """apm init .. must fail -- '..' would create a project in the parent directory."""
        with self.runner.isolated_filesystem():
            result = self.runner.invoke(cli, ["init", "..", "--yes"])
            assert result.exit_code != 0
            assert "Invalid project name" in result.output
            assert ".." in result.output

    def test_init_accepts_plain_name(self):
        """apm init with a simple name still works normally."""
        with self.runner.isolated_filesystem() as tmp_dir:
            result = self.runner.invoke(cli, ["init", "my-project", "--yes"])
            assert result.exit_code == 0
            assert (Path(tmp_dir) / "my-project" / "apm.yml").exists()

    def test_init_interactive_reprompts_on_invalid_name_click(self):
        """In interactive mode, an invalid name triggers a re-prompt."""
        with self.runner.isolated_filesystem() as tmp_dir:
            # First input is invalid (contains '/'), second is valid.
            # In no-argument interactive mode, the prompted name goes into apm.yml
            # but does not create a subdirectory; apm.yml lands in the CWD.
            result = self.runner.invoke(
                cli,
                ["init"],
                input="bad/name\nmy-project\n1.0.0\n\n\ny\ndone\ny\n",
                catch_exceptions=False,
            )
            assert "Invalid project name" in result.output
            assert (Path(tmp_dir) / "apm.yml").exists()

    def test_init_interactive_reprompts_on_dotdot_click(self):
        """In interactive mode, '..' triggers re-prompt."""
        with self.runner.isolated_filesystem() as tmp_dir:
            result = self.runner.invoke(
                cli,
                ["init"],
                input="..\nmy-project\n1.0.0\n\n\ny\ndone\ny\n",
                catch_exceptions=False,
            )
            assert "Invalid project name" in result.output
            assert (Path(tmp_dir) / "apm.yml").exists()


class TestInitTargetPrompt:
    """Test cases for the target selection prompt in apm init (S1-S7)."""

    def setup_method(self):
        """Set up test fixtures."""
        self.runner = CliRunner()
        try:
            self.original_dir = os.getcwd()
        except FileNotFoundError:
            self.original_dir = str(Path(__file__).parent.parent.parent)
            os.chdir(self.original_dir)
        # Force TTY=True for interactive-prompt scenarios. The CliRunner's
        # piped stdin reports isatty=False which would otherwise short-circuit
        # the new prompt into the non-interactive auto-detect branch.
        self._isatty_patch = patch("apm_cli.commands.init._stdin_is_tty", return_value=True)
        self._isatty_patch.start()

    def teardown_method(self):
        """Clean up after tests."""
        self._isatty_patch.stop()
        try:
            os.chdir(self.original_dir)
        except (FileNotFoundError, OSError):
            repo_root = Path(__file__).parent.parent.parent
            os.chdir(str(repo_root))

    def test_init_target_prompt_no_signals(self):
        """S1: Empty dir, user toggles targets via numbered input, verify targets: in apm.yml."""
        with self.runner.isolated_filesystem():
            # New flow: name, version, desc, author, toggle 1, toggle 2, '' (done), confirm(y)
            result = self.runner.invoke(
                cli,
                ["init"],
                input="my-project\n1.0.0\n\n\n1\n2\n\ny\n",
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            content = Path("apm.yml").read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            assert "targets" in data
            assert isinstance(data["targets"], list)
            assert "copilot" in data["targets"]
            assert "claude" in data["targets"]

    def test_init_target_prompt_precheck(self):
        """S2: Create .claude/, verify pre-check state and target in output."""
        with self.runner.isolated_filesystem():
            Path(".claude").mkdir()
            result = self.runner.invoke(
                cli,
                ["init"],
                input="my-project\n1.0.0\n\n\n\ny\n",
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            content = Path("apm.yml").read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            assert "targets" in data
            assert "claude" in data["targets"]

    def test_init_target_prompt_multi_sig(self):
        """S3: .claude/ + .cursor/ + copilot-instructions, verify all three pre-checked."""
        with self.runner.isolated_filesystem():
            Path(".github").mkdir()
            Path(".github/copilot-instructions.md").touch()
            Path(".claude").mkdir()
            Path(".cursor").mkdir()
            result = self.runner.invoke(
                cli,
                ["init"],
                input="my-project\n1.0.0\n\n\n\ny\n",
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            content = Path("apm.yml").read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            assert "targets" in data
            assert "copilot" in data["targets"]
            assert "claude" in data["targets"]
            assert "cursor" in data["targets"]

    def test_init_yes_autodetect(self):
        """S4: --yes with copilot signal present, verify targets in output."""
        with self.runner.isolated_filesystem():
            Path(".github").mkdir()
            Path(".github/copilot-instructions.md").touch()
            result = self.runner.invoke(
                cli,
                ["init", "--yes"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            content = Path("apm.yml").read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            assert "targets" in data
            assert "copilot" in data["targets"]

    def test_init_yes_no_signals(self):
        """S4b: --yes with no signals, verify NO targets key in apm.yml."""
        with self.runner.isolated_filesystem():
            result = self.runner.invoke(
                cli,
                ["init", "--yes"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            content = Path("apm.yml").read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            assert "targets" not in data
            assert "target" not in data

    def test_init_target_flag(self):
        """S5: --target claude,cursor, verify exact value in apm.yml."""
        with self.runner.isolated_filesystem():
            result = self.runner.invoke(
                cli,
                ["init", "--yes", "--target", "claude,cursor"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            content = Path("apm.yml").read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            assert "targets" in data
            assert "claude" in data["targets"]
            assert "cursor" in data["targets"]

    def test_init_target_flag_invalid(self):
        """S5b: --target invalid, exit code non-zero, error message."""
        with self.runner.isolated_filesystem():
            result = self.runner.invoke(
                cli,
                ["init", "--target", "invalid_target"],
            )
            assert result.exit_code != 0

    def test_init_empty_selection(self):
        """S6: User selects nothing, confirms empty, no targets key."""
        with self.runner.isolated_filesystem():
            # Flow: name, version, desc, author, '' (done with nothing toggled),
            # confirm empty(y), confirm setup(y)
            result = self.runner.invoke(
                cli,
                ["init"],
                input="my-project\n1.0.0\n\n\n\ny\ny\n",
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            content = Path("apm.yml").read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            assert "targets" not in data
            assert "target" not in data

    def test_init_reinit_preserves_targets_plural(self):
        """S7: Re-init with existing apm.yml `targets:` list, verify pre-check + plural roundtrip."""
        with self.runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: test\nversion: 1.0.0\ndescription: test\n"
                "author: test\ntargets:\n  - claude\n"
                "dependencies:\n  apm: []\n  mcp: []\n",
                encoding="utf-8",
            )
            # Flow: confirm overwrite(y), name, version, desc, author,
            # '' (done, accept claude precheck), confirm setup(y)
            result = self.runner.invoke(
                cli,
                ["init"],
                input="y\nmy-project\n1.0.0\n\n\n\ny\n",
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            content = Path("apm.yml").read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            assert "targets" in data
            assert "claude" in data["targets"]

    def test_init_reinit_legacy_singular_target(self):
        """Backwards compat: existing legacy `target:` CSV is read on re-init and
        rewritten as canonical plural `targets:` list."""
        with self.runner.isolated_filesystem():
            Path("apm.yml").write_text(
                "name: test\nversion: 1.0.0\ndescription: test\n"
                "author: test\ntarget: claude, cursor\n"
                "dependencies:\n  apm: []\n  mcp: []\n",
                encoding="utf-8",
            )
            result = self.runner.invoke(
                cli,
                ["init"],
                input="y\nmy-project\n1.0.0\n\n\n\ny\n",
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            content = Path("apm.yml").read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            assert "targets" in data
            assert "target" not in data
            assert "claude" in data["targets"]
            assert "cursor" in data["targets"]

    def test_init_non_tty_skips_prompt(self):
        """Non-TTY: --yes auto-detects targets without showing prompt."""
        with self.runner.isolated_filesystem():
            Path(".claude").mkdir()
            # With --yes, no interactive prompt is shown
            result = self.runner.invoke(
                cli,
                ["init", "--yes"],
                catch_exceptions=False,
            )
            assert result.exit_code == 0
            assert "Select targets" not in result.output
            content = Path("apm.yml").read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            assert "targets" in data
            assert "claude" in data["targets"]

    def test_init_non_tty_without_yes_auto_detects(self):
        """Non-TTY without --yes: skip prompt, auto-detect, emit provenance log.

        Real-world scenario: piped stdin in CI / container without --yes.
        Must NOT block on prompt; must auto-detect and tell the user it did.
        """
        # Override the class-level isatty=True patch with isatty=False for
        # this test to simulate genuine non-interactive stdin (e.g. piped CI).
        self._isatty_patch.stop()
        try:
            with patch("apm_cli.commands.init._stdin_is_tty", return_value=False):
                with self.runner.isolated_filesystem():
                    Path(".claude").mkdir()
                    # Provide --yes so _interactive_project_setup is skipped too;
                    # the target prompt's non-TTY guard is the unit under test.
                    result = self.runner.invoke(
                        cli,
                        ["init", "--yes"],
                        catch_exceptions=False,
                    )
                    assert result.exit_code == 0
                    assert "Select targets" not in result.output
                    content = Path("apm.yml").read_text(encoding="utf-8")
                    data = yaml.safe_load(content)
                    assert "targets" in data
                    assert "claude" in data["targets"]
        finally:
            # Restore the class-level patch for any subsequent test in the same
            # session (setup_method re-starts it next test, but be defensive).
            self._isatty_patch.start()


class TestToggleInputParser:
    """Unit tests for the _parse_toggle_input helper."""

    def test_single_number(self):
        from apm_cli.commands.init import _parse_toggle_input

        idx, err = _parse_toggle_input("3", 7)
        assert err is None
        assert idx == [2]

    def test_csv(self):
        from apm_cli.commands.init import _parse_toggle_input

        idx, err = _parse_toggle_input("1,3,5", 7)
        assert err is None
        assert idx == [0, 2, 4]

    def test_range(self):
        from apm_cli.commands.init import _parse_toggle_input

        idx, err = _parse_toggle_input("1-3", 7)
        assert err is None
        assert idx == [0, 1, 2]

    def test_mixed(self):
        from apm_cli.commands.init import _parse_toggle_input

        idx, err = _parse_toggle_input("1,3-5,7", 7)
        assert err is None
        assert idx == [0, 2, 3, 4, 6]

    def test_all(self):
        from apm_cli.commands.init import _parse_toggle_input

        idx, err = _parse_toggle_input("all", 7)
        assert err is None
        assert idx == [0, 1, 2, 3, 4, 5, 6]

    def test_whitespace_tolerant(self):
        from apm_cli.commands.init import _parse_toggle_input

        idx, err = _parse_toggle_input(" 1 - 3 , 5 ", 7)
        assert err is None
        assert idx == [0, 1, 2, 4]

    def test_out_of_bounds(self):
        from apm_cli.commands.init import _parse_toggle_input

        _, err = _parse_toggle_input("9", 7)
        assert err is not None
        assert "out of bounds" in err

    def test_invalid_range(self):
        from apm_cli.commands.init import _parse_toggle_input

        _, err = _parse_toggle_input("3-1", 7)
        assert err is not None

    def test_garbage_input(self):
        from apm_cli.commands.init import _parse_toggle_input

        _, err = _parse_toggle_input("abc", 7)
        assert err is not None
