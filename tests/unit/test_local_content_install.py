"""Unit tests for the local .apm/ content integration feature in apm install.

Covers:
- _has_local_apm_content()
- _integrate_local_content()
- LockFile.local_deployed_files field
"""

from unittest.mock import MagicMock, patch

import pytest  # noqa: F401

from apm_cli.commands.install import _has_local_apm_content, _integrate_local_content
from apm_cli.deps.lockfile import LockFile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_integrators():
    """Return a dict of MagicMock integrators for _integrate_local_content."""
    return {
        "targets": [MagicMock()],
        "prompt_integrator": MagicMock(),
        "agent_integrator": MagicMock(),
        "skill_integrator": MagicMock(),
        "instruction_integrator": MagicMock(),
        "command_integrator": MagicMock(),
        "hook_integrator": MagicMock(),
        "force": False,
        "managed_files": set(),
        "diagnostics": MagicMock(),
    }


def _zero_counters(**overrides):
    """Return a result dict shaped like _integrate_package_primitives output."""
    base = {
        "prompts": 0,
        "agents": 0,
        "skills": 0,
        "sub_skills": 0,
        "instructions": 0,
        "commands": 0,
        "hooks": 0,
        "links_resolved": 0,
        "deployed_files": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests: _has_local_apm_content()
# ---------------------------------------------------------------------------


class TestHasLocalApmContent:
    """Tests for the _has_local_apm_content() helper."""

    def test_no_apm_dir(self, tmp_path):
        """No .apm/ directory at all -> False."""
        assert _has_local_apm_content(tmp_path) is False

    def test_empty_apm_dir(self, tmp_path):
        """.apm/ exists but contains no recognised primitive sub-dirs -> False."""
        (tmp_path / ".apm").mkdir()
        assert _has_local_apm_content(tmp_path) is False

    def test_apm_dir_with_instructions(self, tmp_path):
        """.apm/instructions/ with at least one file -> True."""
        instr_dir = tmp_path / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        (instr_dir / "coding.instructions.md").write_text("# Coding standards")
        assert _has_local_apm_content(tmp_path) is True

    def test_apm_dir_with_skills(self, tmp_path):
        """.apm/skills/ with a nested file -> True."""
        skill_dir = tmp_path / ".apm" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# My Skill")
        assert _has_local_apm_content(tmp_path) is True

    def test_apm_dir_with_agents(self, tmp_path):
        """.apm/agents/ with an agent file -> True."""
        agents_dir = tmp_path / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "my-agent.agent.md").write_text("# My Agent")
        assert _has_local_apm_content(tmp_path) is True

    def test_apm_dir_with_hooks(self, tmp_path):
        """.apm/hooks/ with a hook config -> True."""
        hooks_dir = tmp_path / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "pre-commit.json").write_text('{"hooks": []}')
        assert _has_local_apm_content(tmp_path) is True

    def test_apm_dir_with_empty_subdirs(self, tmp_path):
        """.apm/instructions/ exists but is empty -> False."""
        instr_dir = tmp_path / ".apm" / "instructions"
        instr_dir.mkdir(parents=True)
        # Leave it empty
        assert _has_local_apm_content(tmp_path) is False

    def test_apm_dir_with_only_nested_dirs_no_files(self, tmp_path):
        """.apm/skills/ contains subdirectories but no actual files -> False."""
        skill_dir = tmp_path / ".apm" / "skills" / "empty-skill"
        skill_dir.mkdir(parents=True)
        assert _has_local_apm_content(tmp_path) is False

    def test_apm_dir_only_unknown_subdirs(self, tmp_path):
        """.apm/ has only unrecognised sub-dirs -> False."""
        unknown_dir = tmp_path / ".apm" / "random_stuff"
        unknown_dir.mkdir(parents=True)
        (unknown_dir / "file.txt").write_text("hello")
        assert _has_local_apm_content(tmp_path) is False


# ---------------------------------------------------------------------------
# Tests: _integrate_local_content()
# ---------------------------------------------------------------------------


class TestIntegrateLocalContent:
    """Tests for the _integrate_local_content() helper."""

    @patch("apm_cli.install.services.integrate_package_primitives")
    def test_integrates_instructions(self, mock_integrate, tmp_path):
        """Instructions file in .apm/ is counted in the result."""
        mock_integrate.return_value = _zero_counters(
            instructions=1,
            deployed_files=[".github/instructions/coding.instructions.md"],
        )

        result = _integrate_local_content(tmp_path, **_make_integrators())

        assert result["instructions"] == 1
        assert ".github/instructions/coding.instructions.md" in result["deployed_files"]

    @patch("apm_cli.install.services.integrate_package_primitives")
    def test_integrates_agents(self, mock_integrate, tmp_path):
        """Agent file in .apm/ is counted in the result."""
        mock_integrate.return_value = _zero_counters(
            agents=1,
            deployed_files=[".github/agents/backend.agent.md"],
        )

        result = _integrate_local_content(tmp_path, **_make_integrators())

        assert result["agents"] == 1
        assert ".github/agents/backend.agent.md" in result["deployed_files"]

    @patch("apm_cli.install.services.integrate_package_primitives")
    def test_skips_root_skill_md(self, mock_integrate, tmp_path):
        """A root SKILL.md must NOT be deployed (package_type=APM_PACKAGE prevents it).

        We verify this by inspecting the PackageInfo passed to the underlying
        helper: its package_type must be APM_PACKAGE.
        """
        from apm_cli.models.apm_package import PackageType

        mock_integrate.return_value = _zero_counters()

        # Create a root-level SKILL.md that must be ignored
        (tmp_path / "SKILL.md").write_text("# Project skill description")

        _integrate_local_content(tmp_path, **_make_integrators())

        assert mock_integrate.called
        package_info = mock_integrate.call_args[0][0]
        assert package_info.package_type == PackageType.APM_PACKAGE

    @patch("apm_cli.install.services.integrate_package_primitives")
    def test_package_info_install_path_is_project_root(self, mock_integrate, tmp_path):
        """The synthetic PackageInfo must point to project_root at project scope."""
        mock_integrate.return_value = _zero_counters()

        _integrate_local_content(tmp_path, **_make_integrators())

        package_info = mock_integrate.call_args[0][0]
        assert package_info.install_path == tmp_path

    @patch("apm_cli.install.services.integrate_package_primitives")
    def test_user_scope_install_path_stays_project_root(self, mock_integrate, tmp_path):
        """At user scope, install_path must remain project_root so that
        integrators can still find <project_root>/.apm/<type>/.
        The recursive-glob fix lives in init_link_resolver, not here.
        Regression check for #830."""
        from apm_cli.core.scope import InstallScope

        mock_integrate.return_value = _zero_counters()
        (tmp_path / ".apm").mkdir(exist_ok=True)

        _integrate_local_content(tmp_path, **_make_integrators(), scope=InstallScope.USER)

        package_info = mock_integrate.call_args[0][0]
        assert package_info.install_path == tmp_path

    @patch("apm_cli.install.services.integrate_package_primitives")
    def test_returns_zero_counters_when_nothing_deployed(self, mock_integrate, tmp_path):
        """When nothing is deployed the result counters are all zero."""
        mock_integrate.return_value = _zero_counters()

        result = _integrate_local_content(tmp_path, **_make_integrators())

        for key in ("prompts", "agents", "skills", "instructions", "commands", "hooks"):
            assert result[key] == 0
        assert result["deployed_files"] == []


# ---------------------------------------------------------------------------
# Tests: LockFile.local_deployed_files
# ---------------------------------------------------------------------------


class TestLockFileLocalDeployedFiles:
    """Tests for the LockFile.local_deployed_files field."""

    def test_lockfile_local_deployed_files_default_empty(self):
        """A freshly created LockFile has an empty local_deployed_files list."""
        lock = LockFile()
        assert lock.local_deployed_files == []

    def test_lockfile_local_deployed_files_round_trip(self, tmp_path):
        """Write a lockfile with local_deployed_files, read it back, verify preserved."""
        files = [
            ".github/instructions/foo.instructions.md",
            ".claude/instructions/foo.instructions.md",
        ]
        lock = LockFile()
        lock.local_deployed_files = files

        lock_path = tmp_path / "apm.lock.yaml"
        lock.write(lock_path)

        loaded = LockFile.read(lock_path)
        assert loaded is not None
        assert sorted(loaded.local_deployed_files) == sorted(files)

    def test_lockfile_local_deployed_files_sorted_on_write(self, tmp_path):
        """local_deployed_files are written in sorted order."""
        import yaml

        lock = LockFile()
        lock.local_deployed_files = ["z_file.md", "a_file.md", "m_file.md"]
        lock_path = tmp_path / "apm.lock.yaml"
        lock.write(lock_path)

        raw = yaml.safe_load(lock_path.read_text())
        assert raw["local_deployed_files"] == sorted(lock.local_deployed_files)

    def test_lockfile_semantic_equivalence_with_local(self, tmp_path):
        """Two lockfiles with same local_deployed_files are equivalent; different lists are not."""
        files = [".github/instructions/foo.instructions.md"]

        lock_a = LockFile()
        lock_a.local_deployed_files = files

        lock_b = LockFile()
        lock_b.local_deployed_files = files

        assert lock_a.is_semantically_equivalent(lock_b)

        lock_c = LockFile()
        lock_c.local_deployed_files = [".github/instructions/bar.instructions.md"]

        assert not lock_a.is_semantically_equivalent(lock_c)

    def test_lockfile_empty_local_deployed_not_written(self, tmp_path):
        """An empty local_deployed_files list must NOT appear in the YAML output."""
        import yaml

        lock = LockFile()
        lock_path = tmp_path / "apm.lock.yaml"
        lock.write(lock_path)

        raw = yaml.safe_load(lock_path.read_text())
        assert "local_deployed_files" not in raw

    def test_lockfile_from_yaml_missing_key_defaults_to_empty(self):
        """from_yaml with no local_deployed_files key defaults to empty list."""
        import yaml

        raw = {
            "lockfile_version": "1",
            "generated_at": "2024-01-01T00:00:00+00:00",
            "dependencies": [],
        }
        lock = LockFile.from_yaml(yaml.dump(raw))
        assert lock.local_deployed_files == []

    def test_lockfile_semantic_equivalence_order_independent(self):
        """Semantic equivalence is order-independent for local_deployed_files."""
        lock_a = LockFile()
        lock_a.local_deployed_files = ["b.md", "a.md"]

        lock_b = LockFile()
        lock_b.local_deployed_files = ["a.md", "b.md"]

        assert lock_a.is_semantically_equivalent(lock_b)
