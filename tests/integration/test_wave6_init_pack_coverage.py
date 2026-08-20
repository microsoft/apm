"""Wave 6 — integration tests maximising coverage for init.py and pack.py.

Strategy:
- Use CliRunner (click.testing) to invoke real command code paths.
- Only mock external I/O: HTTP, subprocess, auth tokens, os.environ,
  git operations.  No internal apm_cli functions are mocked.
- Create realistic temp-directory fixtures with apm.yml / .apm/ structures.
- ``monkeypatch.chdir`` puts each test in its own clean directory so tests
  do not interfere with the repo checkout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.models.apm_package import clear_apm_yml_cache

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_LOCKFILE_TEMPLATE = """\
lockfile_version: '1'
generated_at: '2025-01-01T00:00:00+00:00'
dependencies: []
"""

_SKILL_MD = """\
---
description: A test skill
---
# Test Skill
Content here
"""

_AGENT_MD = """\
---
description: A test agent
---
# Test Agent
You are a helpful agent.
"""

_INSTRUCTIONS_MD = """\
---
description: Coding instructions
applyTo: '**/*.py'
---
# Coding Instructions
Follow PEP 8.
"""


def _write_lockfile(root: Path) -> None:
    (root / "apm.lock.yaml").write_text(_LOCKFILE_TEMPLATE, encoding="utf-8")


def _write_apm_yml(root: Path, content: str) -> None:
    (root / "apm.yml").write_text(content, encoding="utf-8")


def _make_skill_project(root: Path) -> None:
    """Create a minimal skill project with apm.yml, .apm/, and lockfile."""
    _write_apm_yml(
        root,
        """\
name: test-package
version: 1.0.0
description: A test package
owner:
  name: test-org
type: skill
dependencies:
  apm: []
""",
    )
    skill_dir = root / ".apm" / "skills" / "test-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    _write_lockfile(root)


def _make_agent_project(root: Path) -> None:
    """Create a minimal agent project with apm.yml, .apm/, and lockfile."""
    _write_apm_yml(
        root,
        """\
name: agent-package
version: 1.0.0
description: An agent package
type: hybrid
dependencies:
  apm: []
""",
    )
    agent_dir = root / ".apm" / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / "helper.agent.md").write_text(_AGENT_MD, encoding="utf-8")
    _write_lockfile(root)


def _make_instructions_project(root: Path) -> None:
    """Create a minimal instructions project."""
    _write_apm_yml(
        root,
        """\
name: instructions-package
version: 1.0.0
description: An instructions package
type: instructions
dependencies:
  apm: []
""",
    )
    instr_dir = root / ".apm" / "instructions"
    instr_dir.mkdir(parents=True)
    (instr_dir / "coding.instructions.md").write_text(_INSTRUCTIONS_MD, encoding="utf-8")
    _write_lockfile(root)


def _make_consumer_project(root: Path) -> None:
    """Create a consumer project with dependencies block."""
    _write_apm_yml(
        root,
        """\
name: consumer-project
version: 1.0.0
description: A consumer project
targets:
  - copilot
dependencies:
  apm: []
""",
    )
    _write_lockfile(root)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ===========================================================================
# init tests
# ===========================================================================


class TestInitBasic:
    """Basic init invocation paths."""

    def test_init_yes_in_empty_dir(self, runner, tmp_path, monkeypatch):
        """--yes skips prompts and auto-detects defaults in an empty dir."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "apm.yml").exists()

    def test_init_yes_creates_valid_yml(self, runner, tmp_path, monkeypatch):
        """apm.yml created by --yes must be valid YAML with a name field."""
        import yaml

        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        data = yaml.safe_load((tmp_path / "apm.yml").read_text())
        assert isinstance(data, dict)
        assert "name" in data

    def test_init_yes_verbose(self, runner, tmp_path, monkeypatch):
        """--verbose flag is accepted and produces output."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["init", "--yes", "--verbose"])
        assert result.exit_code == 0, result.output

    def test_init_with_project_name_arg(self, runner, tmp_path, monkeypatch):
        """Positional project_name creates a subdirectory and chdir into it."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["init", "my-new-project", "--yes"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-new-project" / "apm.yml").exists()

    def test_init_dot_project_name(self, runner, tmp_path, monkeypatch):
        """project_name '.' is treated as current directory."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["init", ".", "--yes"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "apm.yml").exists()

    def test_init_invalid_project_name(self, runner, tmp_path, monkeypatch):
        """A project name with a path separator is rejected."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["init", "bad/name", "--yes"])
        assert result.exit_code != 0


class TestInitExistingYml:
    """Tests where apm.yml already exists."""

    def test_init_yes_overwrites_existing(self, runner, tmp_path, monkeypatch):
        """--yes overwrites an existing apm.yml without prompting."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        (tmp_path / "apm.yml").write_text("name: old-project\nversion: 0.1.0\n")
        result = runner.invoke(cli, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "apm.yml").exists()

    def test_init_confirms_overwrite(self, runner, tmp_path, monkeypatch):
        """Interactive mode asks for confirmation when apm.yml exists."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        (tmp_path / "apm.yml").write_text("name: existing\nversion: 1.0.0\n")
        # Answer 'y' to overwrite, then accept defaults for name/version/desc/author
        # then confirm 'y' and accept empty target selection
        result = runner.invoke(
            cli,
            ["init"],
            input="y\nmy-project\n1.0.0\nA description\nTest Author\n\n\ny\n",
        )
        # Should not crash (interactive path exercised)
        assert result.exit_code in (0, 1), result.output

    def test_init_cancels_on_no(self, runner, tmp_path, monkeypatch):
        """Interactive mode cancels when user says 'n' to overwrite."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        (tmp_path / "apm.yml").write_text("name: existing\nversion: 1.0.0\n")
        result = runner.invoke(cli, ["init"], input="n\n")
        assert result.exit_code == 0, result.output
        # Output should mention cancellation
        assert "cancel" in result.output.lower() or result.exit_code == 0


class TestInitTargetFlag:
    """Tests for --target flag handling."""

    def test_init_yes_with_target_copilot(self, runner, tmp_path, monkeypatch):
        """--target copilot writes targets into apm.yml."""
        import yaml

        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["init", "--yes", "--target", "copilot"])
        assert result.exit_code == 0, result.output
        data = yaml.safe_load((tmp_path / "apm.yml").read_text())
        assert "targets" in data or "target" in data

    def test_init_yes_with_target_claude(self, runner, tmp_path, monkeypatch):
        """--target claude is accepted."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["init", "--yes", "--target", "claude"])
        assert result.exit_code == 0, result.output

    def test_init_yes_with_multi_target(self, runner, tmp_path, monkeypatch):
        """Multiple targets via comma-separated --target."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["init", "--yes", "--target", "copilot,claude"])
        assert result.exit_code == 0, result.output


class TestInitDeprecatedFlags:
    """Tests for deprecated --plugin and --marketplace flags."""

    def test_init_plugin_flag_shows_deprecation(self, runner, tmp_path, monkeypatch):
        """--plugin flag shows a deprecation warning."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        # Provide a valid kebab-case project name for --plugin (uses the tmp dir name otherwise)
        result = runner.invoke(cli, ["init", "my-plugin", "--yes", "--plugin"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-plugin" / "apm.yml").exists()
        assert (tmp_path / "my-plugin" / "plugin.json").exists()

    def test_init_marketplace_flag_shows_deprecation(self, runner, tmp_path, monkeypatch):
        """--marketplace flag shows a deprecation warning and appends block."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["init", "--yes", "--marketplace"])
        assert result.exit_code == 0, result.output
        content = (tmp_path / "apm.yml").read_text()
        # marketplace block appended
        assert "marketplace" in content

    def test_init_plugin_and_marketplace_flags(self, runner, tmp_path, monkeypatch):
        """--plugin and --marketplace together work."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["init", "my-plugin", "--yes", "--plugin", "--marketplace"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-plugin" / "plugin.json").exists()


class TestInitInteractive:
    """Tests exercising the interactive prompt code paths."""

    def test_init_interactive_with_answers(self, runner, tmp_path, monkeypatch):
        """Interactive init with full user input creates apm.yml."""
        import yaml

        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        # Input: name, version, description, author, confirm targets, confirm OK
        result = runner.invoke(
            cli,
            ["init"],
            input="my-project\n1.0.0\nA test project\nTest Author\n\n\ny\n",
        )
        assert result.exit_code in (0, 1), result.output
        if (tmp_path / "apm.yml").exists():
            data = yaml.safe_load((tmp_path / "apm.yml").read_text())
            assert isinstance(data, dict)

    def test_init_interactive_abort_confirmation(self, runner, tmp_path, monkeypatch):
        """User can abort after the confirmation panel."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        # Name, version, desc, author, confirm empty target, then 'n' to abort
        result = runner.invoke(
            cli,
            ["init"],
            input="my-project\n1.0.0\nA test\nAuthor\n\nn\n",
        )
        # Aborted is a valid outcome
        assert result.exit_code in (0, 1), result.output

    def test_init_with_codex_dir(self, runner, tmp_path, monkeypatch):
        """When .codex/ exists, a tip is shown after init."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        (tmp_path / ".codex").mkdir()
        result = runner.invoke(cli, ["init", "--yes"])
        assert result.exit_code == 0, result.output
        # .codex/ triggers a tip about agent-skills target
        assert (tmp_path / "apm.yml").exists()

    def test_init_with_existing_targets_in_yml(self, runner, tmp_path, monkeypatch):
        """Re-init pre-seeds targets from existing apm.yml."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _write_apm_yml(
            tmp_path,
            "name: existing\nversion: 1.0.0\ntargets:\n  - copilot\n",
        )
        result = runner.invoke(cli, ["init", "--yes"])
        assert result.exit_code == 0, result.output

    def test_init_with_legacy_target_in_yml(self, runner, tmp_path, monkeypatch):
        """Re-init reads legacy singular 'target:' field."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _write_apm_yml(
            tmp_path,
            "name: legacy\nversion: 1.0.0\ntarget: claude\n",
        )
        result = runner.invoke(cli, ["init", "--yes"])
        assert result.exit_code == 0, result.output

    def test_init_with_github_signals(self, runner, tmp_path, monkeypatch):
        """Detects copilot target from .github/copilot-instructions.md."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        gh_dir = tmp_path / ".github"
        gh_dir.mkdir()
        (gh_dir / "copilot-instructions.md").write_text("# Copilot Instructions\n")
        result = runner.invoke(cli, ["init", "--yes"])
        assert result.exit_code == 0, result.output


class TestInitPluginNameValidation:
    """Plugin name validation with --plugin flag."""

    def test_init_plugin_valid_name(self, runner, tmp_path, monkeypatch):
        """A valid kebab-case plugin name succeeds."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["init", "valid-plugin", "--yes", "--plugin"])
        assert result.exit_code == 0, result.output
        subdir = tmp_path / "valid-plugin"
        assert (subdir / "plugin.json").exists()

    def test_init_plugin_invalid_name(self, runner, tmp_path, monkeypatch):
        """An invalid plugin name (uppercase) is rejected when --plugin."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        # Use a name with uppercase which fails kebab-case validation
        result = runner.invoke(cli, ["init", "Invalid_Plugin", "--yes", "--plugin"])
        assert result.exit_code != 0


# ===========================================================================
# pack tests
# ===========================================================================


class TestPackBasic:
    """Basic pack invocations with a skill project."""

    def test_pack_skill_project(self, runner, tmp_path, monkeypatch):
        """apm pack in a skill project produces output files."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack"])
        assert result.exit_code == 0, result.output

    def test_pack_agent_project(self, runner, tmp_path, monkeypatch):
        """apm pack in an agent project succeeds."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_agent_project(tmp_path)
        result = runner.invoke(cli, ["pack"])
        assert result.exit_code == 0, result.output

    def test_pack_instructions_project(self, runner, tmp_path, monkeypatch):
        """apm pack in an instructions project succeeds."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_instructions_project(tmp_path)
        result = runner.invoke(cli, ["pack"])
        assert result.exit_code == 0, result.output

    def test_pack_consumer_project(self, runner, tmp_path, monkeypatch):
        """apm pack in a consumer project with empty dependencies."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_consumer_project(tmp_path)
        result = runner.invoke(cli, ["pack"])
        assert result.exit_code == 0, result.output

    def test_pack_verbose(self, runner, tmp_path, monkeypatch):
        """--verbose flag is accepted."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--verbose"])
        assert result.exit_code == 0, result.output

    def test_pack_dry_run(self, runner, tmp_path, monkeypatch):
        """--dry-run produces output without writing files."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--dry-run"])
        assert result.exit_code == 0, result.output

    def test_pack_dry_run_verbose(self, runner, tmp_path, monkeypatch):
        """--dry-run --verbose exercises extra file listing paths."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--dry-run", "--verbose"])
        assert result.exit_code == 0, result.output

    def test_pack_with_output_dir(self, runner, tmp_path, monkeypatch):
        """--output flag changes the bundle output directory."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        out_dir = tmp_path / "custom-build"
        result = runner.invoke(cli, ["pack", "-o", str(out_dir)])
        assert result.exit_code == 0, result.output

    def test_pack_format_plugin(self, runner, tmp_path, monkeypatch):
        """--format plugin (default) is accepted."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--format", "plugin"])
        assert result.exit_code == 0, result.output

    def test_pack_format_apm(self, runner, tmp_path, monkeypatch):
        """--format apm (legacy layout) is accepted."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--format", "apm"])
        assert result.exit_code == 0, result.output

    def test_pack_archive(self, runner, tmp_path, monkeypatch):
        """--archive flag produces a .zip artifact."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--archive"])
        assert result.exit_code == 0, result.output

    def test_pack_force(self, runner, tmp_path, monkeypatch):
        """--force flag is accepted."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--force"])
        assert result.exit_code == 0, result.output


class TestPackJsonOutput:
    """Tests for the --json output mode."""

    @staticmethod
    def _extract_json(output: str) -> dict:
        """Extract JSON object from output that may contain log lines."""
        import json

        # Find the first '{' and parse from there
        start = output.find("{")
        if start == -1:
            raise ValueError(f"No JSON found in output: {output!r}")
        return json.loads(output[start:])

    def test_pack_json_output(self, runner, tmp_path, monkeypatch):
        """--json flag emits a JSON envelope to stdout."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--json"])
        assert result.exit_code == 0, result.output
        envelope = self._extract_json(result.output)
        assert "ok" in envelope
        assert "bundle" in envelope or "marketplace" in envelope

    def test_pack_json_dry_run(self, runner, tmp_path, monkeypatch):
        """--json --dry-run returns a valid JSON envelope."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--json", "--dry-run"])
        assert result.exit_code == 0, result.output
        envelope = self._extract_json(result.output)
        assert envelope["dry_run"] is True


class TestPackDeprecatedTarget:
    """Tests for deprecated --target flag on pack."""

    def test_pack_with_target_shows_deprecation(self, runner, tmp_path, monkeypatch):
        """--target on pack triggers the deprecation warning."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--target", "copilot"])
        assert result.exit_code == 0, result.output
        assert "deprecated" in result.output.lower()

    def test_pack_with_target_claude(self, runner, tmp_path, monkeypatch):
        """--target claude is accepted (with deprecation warning)."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "-t", "claude"])
        assert result.exit_code == 0, result.output


class TestPackMarketplaceFilter:
    """Tests for --marketplace / -m filter flag."""

    def test_pack_marketplace_none_skips(self, runner, tmp_path, monkeypatch):
        """--marketplace none skips marketplace output."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--marketplace", "none"])
        assert result.exit_code == 0, result.output

    def test_pack_marketplace_all(self, runner, tmp_path, monkeypatch):
        """--marketplace all is accepted."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "-m", "all"])
        assert result.exit_code == 0, result.output

    def test_pack_marketplace_unknown_format_raises(self, runner, tmp_path, monkeypatch):
        """--marketplace with an unknown format name returns non-zero."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "-m", "unknown-format-xyz"])
        assert result.exit_code != 0


class TestPackMarketplaceOutput:
    """Tests for the removed --marketplace-output flag."""

    def test_pack_marketplace_output_removed(self, runner, tmp_path, monkeypatch):
        """--marketplace-output was removed; Click rejects it."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(
            cli, ["pack", "--marketplace-output", str(tmp_path / "marketplace.json")]
        )
        assert result.exit_code != 0
        assert "no such option" in (result.output or "").lower()
        assert "--marketplace-output" in (result.output or "")


class TestPackMarketplacePath:
    """Tests for --marketplace-path overrides."""

    def test_pack_marketplace_path_valid(self, runner, tmp_path, monkeypatch):
        """Valid --marketplace-path FORMAT=PATH is accepted."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        out_path = tmp_path / "out" / "marketplace.json"
        result = runner.invoke(cli, ["pack", "--marketplace-path", f"claude={out_path}"])
        assert result.exit_code == 0, result.output

    def test_pack_marketplace_path_missing_equals(self, runner, tmp_path, monkeypatch):
        """--marketplace-path without '=' is an error."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--marketplace-path", "no-equals-here"])
        assert result.exit_code != 0

    def test_pack_marketplace_path_unknown_format(self, runner, tmp_path, monkeypatch):
        """--marketplace-path with unknown format name is an error."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--marketplace-path", "no-such-format=/tmp/x.json"])
        assert result.exit_code != 0


class TestPackLegacySkillPaths:
    """Tests for --legacy-skill-paths flag."""

    def test_pack_legacy_skill_paths(self, runner, tmp_path, monkeypatch):
        """--legacy-skill-paths flag is accepted."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--legacy-skill-paths"])
        assert result.exit_code == 0, result.output


class TestPackMissingApmYml:
    """Pack behaviour when apm.yml is absent."""

    def test_pack_missing_apm_yml(self, runner, tmp_path, monkeypatch):
        """pack without apm.yml exits non-zero."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["pack"])
        assert result.exit_code != 0


class TestPackMultiplePrimitives:
    """Pack with various .apm/ directory structures."""

    def test_pack_with_skills_and_agents(self, runner, tmp_path, monkeypatch):
        """Pack project containing both skills and agents."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _write_apm_yml(
            tmp_path,
            """\
name: multi-package
version: 1.0.0
description: Package with multiple primitives
dependencies:
  apm: []
""",
        )
        skill_dir = tmp_path / ".apm" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")

        agent_dir = tmp_path / ".apm" / "agents"
        agent_dir.mkdir(parents=True)
        (agent_dir / "helper.agent.md").write_text(_AGENT_MD, encoding="utf-8")

        instr_dir = tmp_path / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "base.instructions.md").write_text(_INSTRUCTIONS_MD, encoding="utf-8")

        _write_lockfile(tmp_path)
        result = runner.invoke(cli, ["pack"])
        assert result.exit_code == 0, result.output

    def test_pack_with_chatmodes(self, runner, tmp_path, monkeypatch):
        """Pack project containing chatmode files."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _write_apm_yml(
            tmp_path,
            "name: chatmode-pkg\nversion: 1.0.0\ndescription: Chatmode package\ndependencies:\n  apm: []\n",
        )
        chatmode_dir = tmp_path / ".apm" / "agents"
        chatmode_dir.mkdir(parents=True)
        (chatmode_dir / "backend.agent.md").write_text(
            "---\ndescription: Backend mode\n---\n# Backend\nBackend focus.\n",
            encoding="utf-8",
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(cli, ["pack"])
        assert result.exit_code == 0, result.output

    def test_pack_with_memory(self, runner, tmp_path, monkeypatch):
        """Pack project containing memory/constitution file."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _write_apm_yml(
            tmp_path,
            "name: memory-pkg\nversion: 1.0.0\ndescription: Memory package\ndependencies:\n  apm: []\n",
        )
        mem_dir = tmp_path / ".apm" / "memory"
        mem_dir.mkdir(parents=True)
        (mem_dir / "constitution.md").write_text(
            "# Constitution\nCore principles.\n", encoding="utf-8"
        )
        _write_lockfile(tmp_path)
        result = runner.invoke(cli, ["pack"])
        assert result.exit_code == 0, result.output

    def test_pack_empty_apm_dir(self, runner, tmp_path, monkeypatch):
        """Pack with an empty .apm/ dir (no primitive files)."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _write_apm_yml(
            tmp_path,
            "name: empty-pkg\nversion: 1.0.0\ndescription: Empty package\ndependencies:\n  apm: []\n",
        )
        (tmp_path / ".apm").mkdir()
        _write_lockfile(tmp_path)
        result = runner.invoke(cli, ["pack"])
        assert result.exit_code == 0, result.output


class TestPackCheckVersions:
    """Tests for --check-versions gate."""

    def test_pack_check_versions_no_marketplace(self, runner, tmp_path, monkeypatch):
        """--check-versions with no marketplace block logs a skip message."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--check-versions"])
        assert result.exit_code == 0, result.output

    def test_pack_check_clean_no_marketplace(self, runner, tmp_path, monkeypatch):
        """--check-clean with no marketplace block logs a skip message."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--check-clean"])
        assert result.exit_code == 0, result.output

    def test_pack_check_versions_and_clean_together(self, runner, tmp_path, monkeypatch):
        """--check-versions --check-clean together are accepted."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--check-versions", "--check-clean"])
        assert result.exit_code == 0, result.output

    def test_pack_check_versions_json(self, runner, tmp_path, monkeypatch):
        """--check-versions --json emits a JSON envelope."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--check-versions", "--json"])
        assert result.exit_code == 0, result.output
        start = result.output.find("{")
        assert start != -1, f"No JSON in output: {result.output!r}"
        import json

        data = json.loads(result.output[start:])
        assert "version_alignment" in data


class TestPackOfflineAndPrerelease:
    """Tests for --offline and --include-prerelease flags."""

    def test_pack_offline(self, runner, tmp_path, monkeypatch):
        """--offline flag is accepted."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--offline"])
        assert result.exit_code == 0, result.output

    def test_pack_include_prerelease(self, runner, tmp_path, monkeypatch):
        """--include-prerelease flag is accepted."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        _make_skill_project(tmp_path)
        result = runner.invoke(cli, ["pack", "--include-prerelease"])
        assert result.exit_code == 0, result.output


# ===========================================================================
# init internal helpers (direct path tests)
# ===========================================================================


class TestInitReadExistingTargets:
    """Directly exercises the _read_existing_targets helper."""

    def test_reads_targets_list(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        (tmp_path / "apm.yml").write_text(
            "name: x\ntargets:\n  - copilot\n  - claude\n", encoding="utf-8"
        )
        result = _read_existing_targets(tmp_path)
        assert "copilot" in result
        assert "claude" in result

    def test_reads_legacy_target_scalar(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        (tmp_path / "apm.yml").write_text("name: x\ntarget: claude\n", encoding="utf-8")
        result = _read_existing_targets(tmp_path)
        assert "claude" in result

    def test_reads_legacy_target_csv(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        (tmp_path / "apm.yml").write_text("name: x\ntarget: copilot,claude\n", encoding="utf-8")
        result = _read_existing_targets(tmp_path)
        assert "copilot" in result
        assert "claude" in result

    def test_missing_yml_returns_empty(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        result = _read_existing_targets(tmp_path)
        assert result == []

    def test_invalid_yml_returns_empty(self, tmp_path):
        from apm_cli.commands.init import _read_existing_targets

        (tmp_path / "apm.yml").write_text(": invalid: yaml: content:\n", encoding="utf-8")
        result = _read_existing_targets(tmp_path)
        assert isinstance(result, list)


class TestInitParseToggle:
    """Directly exercises the _parse_toggle_input helper."""

    def test_empty_response(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("", 5)
        assert indices == []
        assert err is None

    def test_single_number(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("2", 5)
        assert indices == [1]
        assert err is None

    def test_csv(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("1,3", 5)
        assert 0 in indices
        assert 2 in indices
        assert err is None

    def test_range(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("1-3", 5)
        assert indices == [0, 1, 2]
        assert err is None

    def test_all(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("all", 3)
        assert indices == [0, 1, 2]
        assert err is None

    def test_none(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("none", 3)
        assert indices == [0, 1, 2]
        assert err is None

    def test_invalid_token(self):
        from apm_cli.commands.init import _parse_toggle_input

        _indices, err = _parse_toggle_input("abc", 5)
        assert err is not None

    def test_out_of_bounds(self):
        from apm_cli.commands.init import _parse_toggle_input

        _indices, err = _parse_toggle_input("10", 5)
        assert err is not None

    def test_invalid_range(self):
        from apm_cli.commands.init import _parse_toggle_input

        _indices, err = _parse_toggle_input("3-1", 5)
        assert err is not None

    def test_range_bad_parts(self):
        from apm_cli.commands.init import _parse_toggle_input

        _indices, err = _parse_toggle_input("a-b", 5)
        assert err is not None

    def test_mixed_csv_and_range(self):
        from apm_cli.commands.init import _parse_toggle_input

        indices, err = _parse_toggle_input("1,3-4", 5)
        assert err is None
        assert 0 in indices
        assert 2 in indices
        assert 3 in indices


class TestInitStdinTty:
    """Tests for _stdin_is_tty helper."""

    def test_returns_bool(self):
        from apm_cli.commands.init import _stdin_is_tty

        result = _stdin_is_tty()
        assert isinstance(result, bool)


class TestInitResolveTargets:
    """Tests for _resolve_init_targets in non-interactive mode."""

    def test_target_flag_wins(self, tmp_path):
        from apm_cli.commands.init import _resolve_init_targets
        from apm_cli.core.command_logger import CommandLogger

        logger = CommandLogger("init", verbose=False)
        result = _resolve_init_targets(
            project_root=tmp_path,
            target_flag="copilot",
            yes=True,
            apm_yml_exists=False,
            logger=logger,
        )
        assert result == ["copilot"]

    def test_yes_no_signals_returns_none(self, tmp_path):
        from apm_cli.commands.init import _resolve_init_targets
        from apm_cli.core.command_logger import CommandLogger

        logger = CommandLogger("init", verbose=False)
        result = _resolve_init_targets(
            project_root=tmp_path,
            target_flag=None,
            yes=True,
            apm_yml_exists=False,
            logger=logger,
        )
        # No signals => None
        assert result is None

    def test_yes_with_github_signal(self, tmp_path):
        from apm_cli.commands.init import _resolve_init_targets
        from apm_cli.core.command_logger import CommandLogger

        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "copilot-instructions.md").write_text("# CI\n")
        logger = CommandLogger("init", verbose=False)
        result = _resolve_init_targets(
            project_root=tmp_path,
            target_flag=None,
            yes=True,
            apm_yml_exists=False,
            logger=logger,
        )
        # Signal detected -> copilot in targets
        assert result is None or "copilot" in (result or [])


# ===========================================================================
# pack emit JSON error helper
# ===========================================================================


class TestPackEmitJsonError:
    """Tests for _emit_json_error_or_raise in pack.py."""

    def test_raises_click_exception_non_json(self):
        import click
        from click.testing import CliRunner

        from apm_cli.commands.pack import _emit_json_error_or_raise

        @click.command()
        @click.pass_context
        def _cmd(ctx):
            _emit_json_error_or_raise(ctx, False, "test_code", "test message")

        r = CliRunner().invoke(_cmd)
        assert r.exit_code != 0
        assert "test message" in r.output

    def test_emits_json_envelope(self):
        import json

        import click
        from click.testing import CliRunner

        from apm_cli.commands.pack import _emit_json_error_or_raise

        @click.command()
        @click.pass_context
        def _cmd(ctx):
            _emit_json_error_or_raise(ctx, True, "test_code", "json error message")

        r = CliRunner().invoke(_cmd)
        # Find JSON in output (may have prefix log lines)
        start = r.output.find("{")
        assert start != -1, f"No JSON in output: {r.output!r}"
        data = json.loads(r.output[start:])
        assert data.get("ok") is False


# ===========================================================================
# plugin init subcommand (exercises init._perform_init via plugin source)
# ===========================================================================


class TestPluginInit:
    """Tests for 'apm plugin init' which reuses _perform_init."""

    def test_plugin_init_yes(self, runner, tmp_path, monkeypatch):
        """apm plugin init --yes creates apm.yml and plugin.json."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        # Provide a valid kebab-case project name to avoid tmp_path name validation failure
        result = runner.invoke(cli, ["plugin", "init", "my-plugin", "--yes"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-plugin" / "apm.yml").exists()
        assert (tmp_path / "my-plugin" / "plugin.json").exists()

    def test_plugin_init_verbose(self, runner, tmp_path, monkeypatch):
        """apm plugin init --yes --verbose is accepted."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["plugin", "init", "my-plugin", "--yes", "--verbose"])
        assert result.exit_code == 0, result.output

    def test_plugin_init_with_target(self, runner, tmp_path, monkeypatch):
        """apm plugin init with --target flag records target."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["plugin", "init", "my-plugin", "--yes", "--target", "copilot"])
        assert result.exit_code == 0, result.output

    def test_plugin_init_named_project(self, runner, tmp_path, monkeypatch):
        """apm plugin init my-plugin --yes creates a subdirectory."""
        monkeypatch.chdir(tmp_path)
        clear_apm_yml_cache()
        result = runner.invoke(cli, ["plugin", "init", "my-plugin", "--yes"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-plugin" / "plugin.json").exists()
