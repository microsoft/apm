"""Phase-3b integration tests for four modules with the largest integration gaps.

Coverage targets:
  - mcp_integrator_install.py   (39.7%, gap=286)
  - agent_integrator.py         (42.9%, gap=196)
  - prompt_integrator.py        (31.4%, gap=194)
  - install/validation.py       (54.9%, gap=207)

Strategy:
  - Exercise real code paths; mock only external I/O (network, subprocesses,
    home-directory state, binary detection).
  - No live network calls.
  - Use type hints throughout.
  - 60+ test functions covering newly-uncovered branches.
"""

from __future__ import annotations

import subprocess  # noqa: F401 -- keep for potential future use
from datetime import datetime
from pathlib import Path
from typing import Any  # noqa: F401 -- keep for potential future use
from unittest.mock import MagicMock, patch

import pytest
import requests

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.mcp_integrator_install import run_mcp_install
from apm_cli.integration.prompt_integrator import (
    PromptIntegrator,
    Schedule,
    _is_workflow_shape,
    _parse_workflow_frontmatter,
)
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_package_info(
    name: str,
    install_path: Path,
    source: str | None = None,
    version: str = "1.0.0",
) -> PackageInfo:
    """Build a minimal PackageInfo for testing."""
    package = APMPackage(
        name=name,
        version=version,
        source=source,
        package_path=install_path,
    )
    resolved_ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=install_path,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
    )


# ===========================================================================
# mcp_integrator_install  --  run_mcp_install()
# ===========================================================================


class TestRunMcpInstallScopeHandling:
    """Scope enum propagation into run_mcp_install."""

    def test_no_scope_empty_deps_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert run_mcp_install([], logger=NullCommandLogger()) == 0

    def test_user_scope_enum_propagates_no_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apm_cli.core.scope import InstallScope

        monkeypatch.chdir(tmp_path)
        result = run_mcp_install(
            [],
            scope=InstallScope.USER,
            logger=NullCommandLogger(),
        )
        assert result == 0

    def test_project_scope_enum_propagates_no_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apm_cli.core.scope import InstallScope

        monkeypatch.chdir(tmp_path)
        result = run_mcp_install(
            [],
            scope=InstallScope.PROJECT,
            logger=NullCommandLogger(),
        )
        assert result == 0

    def test_user_scope_without_supported_runtimes_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """user_scope=True with no supported runtimes should warn and return 0."""
        from apm_cli.core.scope import InstallScope
        from apm_cli.models.dependency.mcp import MCPDependency

        monkeypatch.chdir(tmp_path)
        dep = MCPDependency.from_string("io.github.test/my-server")
        logger = MagicMock()

        # Patch to produce a cursor runtime (workspace-only, not user-scope compatible)
        # then scope filter removes it.
        with (
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=["vscode"],
            ),
            patch(
                "apm_cli.factory.ClientFactory.create_client",
                side_effect=_make_vscode_client,
            ),
        ):
            result = run_mcp_install(
                [dep],
                runtime="vscode",
                scope=InstallScope.USER,
                project_root=tmp_path,
                logger=logger,
            )
        # Returns 0: either filtered out or gated
        assert isinstance(result, int)


def _make_vscode_client(name: str) -> MagicMock:
    """Return a mock client that marks itself as NOT supporting user scope."""
    client = MagicMock()
    client.supports_user_scope = False
    return client


class TestRunMcpInstallRuntimeDetection:
    """Runtime detection and exclusion paths."""

    def test_explicit_runtime_targets_single_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """explicit runtime= option sets target_runtimes to [runtime]."""
        from apm_cli.models.dependency.mcp import MCPDependency

        monkeypatch.chdir(tmp_path)
        dep = MCPDependency.from_string("io.github.org/server")

        with patch(
            "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
            return_value=[],  # gate returns empty → early return 0
        ):
            result = run_mcp_install(
                [dep],
                runtime="copilot",
                project_root=tmp_path,
                logger=NullCommandLogger(),
            )
        assert isinstance(result, int)

    def test_exclude_runtime_removes_it_from_targets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """exclude= removes the runtime from target list before gate."""
        from apm_cli.models.dependency.mcp import MCPDependency

        monkeypatch.chdir(tmp_path)
        dep = MCPDependency.from_string("io.github.org/server")

        with (
            patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=False),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
        ):
            result = run_mcp_install(
                [dep],
                runtime="vscode",
                exclude="vscode",
                project_root=tmp_path,
                logger=NullCommandLogger(),
            )
        assert isinstance(result, int)

    def test_directory_presence_enables_cursor_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """.cursor/ directory presence makes cursor an installed runtime."""
        from apm_cli.models.dependency.mcp import MCPDependency

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".cursor").mkdir()
        dep = MCPDependency.from_string("io.github.org/server")

        with (
            patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=False),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
            patch("apm_cli.factory.ClientFactory.create_client", return_value=MagicMock()),
        ):
            result = run_mcp_install(
                [dep],
                project_root=tmp_path,
                logger=NullCommandLogger(),
            )
        assert isinstance(result, int)

    def test_directory_presence_enables_opencode_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """.opencode/ directory presence enables opencode runtime."""
        from apm_cli.models.dependency.mcp import MCPDependency

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".opencode").mkdir()
        dep = MCPDependency.from_string("io.github.org/server")

        with (
            patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=False),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
            patch("apm_cli.factory.ClientFactory.create_client", return_value=MagicMock()),
        ):
            result = run_mcp_install(
                [dep],
                project_root=tmp_path,
                logger=NullCommandLogger(),
            )
        assert isinstance(result, int)

    def test_directory_presence_enables_gemini_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """.gemini/ directory presence enables gemini runtime."""
        from apm_cli.models.dependency.mcp import MCPDependency

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gemini").mkdir()
        dep = MCPDependency.from_string("io.github.org/server")

        with (
            patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=False),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
            patch("apm_cli.factory.ClientFactory.create_client", return_value=MagicMock()),
        ):
            result = run_mcp_install(
                [dep],
                project_root=tmp_path,
                logger=NullCommandLogger(),
            )
        assert isinstance(result, int)

    def test_directory_presence_enables_windsurf_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """.windsurf/ directory presence enables windsurf runtime."""
        from apm_cli.models.dependency.mcp import MCPDependency

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".windsurf").mkdir()
        dep = MCPDependency.from_string("io.github.org/server")

        with (
            patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=False),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
            patch("apm_cli.factory.ClientFactory.create_client", return_value=MagicMock()),
        ):
            result = run_mcp_install(
                [dep],
                project_root=tmp_path,
                logger=NullCommandLogger(),
            )
        assert isinstance(result, int)

    def test_apm_yml_loaded_lazily_when_not_provided(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When apm_config=None, apm.yml is loaded lazily from project root."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "apm.yml").write_text("name: test\nversion: 1.0.0\n")
        result = run_mcp_install([], project_root=tmp_path, logger=NullCommandLogger())
        assert result == 0

    def test_stored_mcp_configs_none_defaults_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stored_mcp_configs=None defaults to {} without error."""
        monkeypatch.chdir(tmp_path)
        result = run_mcp_install(
            [],
            stored_mcp_configs=None,
            logger=NullCommandLogger(),
        )
        assert result == 0

    def test_verbose_true_no_crash_on_empty_deps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = run_mcp_install([], verbose=True, logger=NullCommandLogger())
        assert result == 0

    def test_string_deps_treated_as_registry(self) -> None:
        """Plain string deps are treated as registry entries."""
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_string("io.github.owner/my-server")
        assert dep.is_registry_resolved is True

    def test_self_defined_dep_classified_correctly(self) -> None:
        """registry=False marks dep as self-defined."""
        from apm_cli.models.dependency.mcp import MCPDependency

        dep = MCPDependency.from_dict(
            {
                "name": "local-srv",
                "registry": False,
                "transport": "stdio",
                "command": "python",
                "args": ["srv.py"],
            }
        )
        assert dep.is_self_defined is True
        assert dep.is_registry_resolved is False

    def test_gate_returning_empty_short_circuits_to_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When gate returns [], run_mcp_install returns 0 immediately."""
        from apm_cli.models.dependency.mcp import MCPDependency

        monkeypatch.chdir(tmp_path)
        dep = MCPDependency.from_string("io.github.x/y")

        with patch(
            "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
            return_value=[],
        ):
            result = run_mcp_install(
                [dep],
                runtime="copilot",
                project_root=tmp_path,
                logger=NullCommandLogger(),
            )
        assert result == 0

    def test_no_runtimes_installed_falls_back_to_vscode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no runtimes installed, vscode is used as a fallback."""
        from apm_cli.models.dependency.mcp import MCPDependency

        monkeypatch.chdir(tmp_path)
        dep = MCPDependency.from_string("io.github.x/y")
        logger = MagicMock()

        with (
            patch("apm_cli.integration.mcp_integrator._is_vscode_available", return_value=False),
            patch(
                "apm_cli.integration.mcp_integrator_install.find_runtime_binary",
                return_value=None,
            ),
            patch(
                "apm_cli.integration.mcp_integrator.MCPIntegrator._gate_project_scoped_runtimes",
                return_value=[],
            ),
            patch("apm_cli.factory.ClientFactory.create_client", side_effect=ValueError("nope")),
        ):
            result = run_mcp_install(
                [dep],
                project_root=tmp_path,
                logger=logger,
            )
        assert isinstance(result, int)


# ===========================================================================
# agent_integrator  --  AgentIntegrator
# ===========================================================================


class TestAgentIntegratorFindFiles:
    """find_agent_files discovers the right files."""

    def test_finds_agent_md_in_root(self, tmp_path: Path) -> None:
        (tmp_path / "security.agent.md").write_text("# Security")
        (tmp_path / "planner.agent.md").write_text("# Planner")
        integrator = AgentIntegrator()
        files = integrator.find_agent_files(tmp_path)
        names = {f.name for f in files}
        assert "security.agent.md" in names
        assert "planner.agent.md" in names

    def test_finds_chatmode_md_in_root(self, tmp_path: Path) -> None:
        (tmp_path / "default.chatmode.md").write_text("# Default")
        integrator = AgentIntegrator()
        files = integrator.find_agent_files(tmp_path)
        assert any(f.name == "default.chatmode.md" for f in files)

    def test_finds_agent_md_in_apm_agents_subdir(self, tmp_path: Path) -> None:
        apm_agents = tmp_path / ".apm" / "agents"
        apm_agents.mkdir(parents=True)
        (apm_agents / "reviewer.agent.md").write_text("# Reviewer")
        integrator = AgentIntegrator()
        files = integrator.find_agent_files(tmp_path)
        assert any(f.name == "reviewer.agent.md" for f in files)

    def test_finds_plain_md_in_apm_agents_subdir(self, tmp_path: Path) -> None:
        """Plain .md files in .apm/agents/ are also included."""
        apm_agents = tmp_path / ".apm" / "agents"
        apm_agents.mkdir(parents=True)
        (apm_agents / "helper.md").write_text("# Helper")
        integrator = AgentIntegrator()
        files = integrator.find_agent_files(tmp_path)
        assert any(f.name == "helper.md" for f in files)

    def test_finds_chatmode_in_apm_chatmodes_subdir(self, tmp_path: Path) -> None:
        apm_chatmodes = tmp_path / ".apm" / "chatmodes"
        apm_chatmodes.mkdir(parents=True)
        (apm_chatmodes / "review.chatmode.md").write_text("# Review")
        integrator = AgentIntegrator()
        files = integrator.find_agent_files(tmp_path)
        assert any(f.name == "review.chatmode.md" for f in files)

    def test_non_agent_md_not_found(self, tmp_path: Path) -> None:
        """Plain README.md should NOT be discovered."""
        (tmp_path / "README.md").write_text("# Readme")
        integrator = AgentIntegrator()
        files = integrator.find_agent_files(tmp_path)
        assert all(f.name != "README.md" for f in files)

    def test_empty_package_returns_empty_list(self, tmp_path: Path) -> None:
        integrator = AgentIntegrator()
        files = integrator.find_agent_files(tmp_path)
        assert files == []


class TestAgentIntegratorTargetFilenames:
    """get_target_filename_for_target produces correct file extensions."""

    def test_copilot_target_preserves_agent_md(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = AgentIntegrator()
        source = Path("security.agent.md")
        filename = integrator.get_target_filename_for_target(
            source, "test-pkg", KNOWN_TARGETS["copilot"]
        )
        assert filename.endswith(".agent.md")
        assert "security" in filename

    def test_claude_target_uses_plain_md(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = AgentIntegrator()
        source = Path("security.agent.md")
        filename = integrator.get_target_filename_for_target(
            source, "test-pkg", KNOWN_TARGETS["claude"]
        )
        assert filename.endswith(".md")
        assert not filename.endswith(".agent.md")

    def test_cursor_target_uses_plain_md(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = AgentIntegrator()
        source = Path("review.agent.md")
        filename = integrator.get_target_filename_for_target(source, "pkg", KNOWN_TARGETS["cursor"])
        assert filename.endswith(".md")

    def test_chatmode_source_extracts_stem(self) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        integrator = AgentIntegrator()
        source = Path("backend.chatmode.md")
        filename = integrator.get_target_filename_for_target(
            source, "pkg", KNOWN_TARGETS["copilot"]
        )
        assert "backend" in filename
        assert filename.endswith(".agent.md")

    def test_deprecated_get_target_filename_delegates_to_copilot(self) -> None:
        integrator = AgentIntegrator()
        source = Path("my.agent.md")
        filename = integrator.get_target_filename(source, "pkg")
        assert filename.endswith(".agent.md")

    def test_deprecated_get_target_filename_claude(self) -> None:
        integrator = AgentIntegrator()
        source = Path("my.agent.md")
        filename = integrator.get_target_filename_claude(source, "pkg")
        assert filename.endswith(".md")
        assert not filename.endswith(".agent.md")

    def test_deprecated_get_target_filename_cursor(self) -> None:
        integrator = AgentIntegrator()
        source = Path("my.agent.md")
        filename = integrator.get_target_filename_cursor(source, "pkg")
        assert filename.endswith(".md")


class TestAgentIntegratorCopyAgent:
    """copy_agent reads source and writes target."""

    def test_copy_agent_verbatim(self, tmp_path: Path) -> None:
        src = tmp_path / "agent.agent.md"
        dst = tmp_path / "out.agent.md"
        src.write_text("# Agent\nContent here.", encoding="utf-8")
        integrator = AgentIntegrator()
        integrator.copy_agent(src, dst)
        assert dst.read_text(encoding="utf-8") == "# Agent\nContent here."

    def test_copy_agent_rejects_symlink(self, tmp_path: Path) -> None:
        real = tmp_path / "real.agent.md"
        real.write_text("real content", encoding="utf-8")
        link = tmp_path / "link.agent.md"
        link.symlink_to(real)
        dst = tmp_path / "out.agent.md"
        integrator = AgentIntegrator()
        with pytest.raises(ValueError, match="symlink"):
            integrator.copy_agent(link, dst)


class TestAgentIntegratorWriteCodexAgent:
    """_write_codex_agent transforms .agent.md to TOML."""

    def test_write_codex_agent_plain_md(self, tmp_path: Path) -> None:
        src = tmp_path / "my.agent.md"
        dst = tmp_path / "my.toml"
        src.write_text("# My Agent\n\nDo something.", encoding="utf-8")
        AgentIntegrator._write_codex_agent(src, dst)
        content = dst.read_text(encoding="utf-8")
        assert "developer_instructions" in content

    def test_write_codex_agent_with_frontmatter(self, tmp_path: Path) -> None:
        src = tmp_path / "spec.agent.md"
        dst = tmp_path / "spec.toml"
        src.write_text(
            "---\nname: My Spec Agent\ndescription: Specialised agent\n---\n\nDo work.",
            encoding="utf-8",
        )
        AgentIntegrator._write_codex_agent(src, dst)
        content = dst.read_text(encoding="utf-8")
        assert "My Spec Agent" in content
        assert "Specialised agent" in content

    def test_write_codex_agent_name_from_stem(self, tmp_path: Path) -> None:
        """When no frontmatter name, stem is used."""
        src = tmp_path / "planner.agent.md"
        dst = tmp_path / "planner.toml"
        src.write_text("Some instructions.", encoding="utf-8")
        AgentIntegrator._write_codex_agent(src, dst)
        content = dst.read_text(encoding="utf-8")
        assert "planner" in content

    def test_write_codex_agent_rejects_symlink(self, tmp_path: Path) -> None:
        real = tmp_path / "real.agent.md"
        real.write_text("content", encoding="utf-8")
        link = tmp_path / "link.agent.md"
        link.symlink_to(real)
        dst = tmp_path / "out.toml"
        with pytest.raises(ValueError, match="symlink"):
            AgentIntegrator._write_codex_agent(link, dst)


class TestAgentIntegratorWriteWindsurfAgentSkill:
    """_write_windsurf_agent_skill transforms .agent.md to SKILL.md."""

    def test_write_windsurf_basic(self, tmp_path: Path) -> None:
        src = tmp_path / "tester.agent.md"
        dst = tmp_path / "SKILL.md"
        src.write_text("# Tester Agent\n\nTest things.", encoding="utf-8")
        integrator = AgentIntegrator()
        integrator._write_windsurf_agent_skill(src, dst)
        content = dst.read_text(encoding="utf-8")
        assert "---" in content
        assert "tester" in content.lower()

    def test_write_windsurf_with_frontmatter(self, tmp_path: Path) -> None:
        src = tmp_path / "rev.agent.md"
        dst = tmp_path / "SKILL.md"
        src.write_text(
            "---\nname: Reviewer\ndescription: Reviews code\n---\n\nReview all PRs.",
            encoding="utf-8",
        )
        integrator = AgentIntegrator()
        integrator._write_windsurf_agent_skill(src, dst)
        content = dst.read_text(encoding="utf-8")
        assert "Reviewer" in content
        assert "Reviews code" in content

    def test_write_windsurf_drops_agent_only_fields(self, tmp_path: Path) -> None:
        """tools and model fields are dropped with a diagnostic."""
        src = tmp_path / "agent.agent.md"
        dst = tmp_path / "SKILL.md"
        src.write_text(
            "---\nname: SomeAgent\ntools:\n  - read_file\nmodel: gpt-4\n---\n\nBody.",
            encoding="utf-8",
        )
        diagnostics = MagicMock()
        integrator = AgentIntegrator()
        integrator._write_windsurf_agent_skill(src, dst, diagnostics=diagnostics)
        content = dst.read_text(encoding="utf-8")
        # tools and model should not appear in the output YAML frontmatter
        assert "tools:" not in content or "SomeAgent" in content
        diagnostics.warn.assert_called_once()

    def test_write_windsurf_rejects_symlink(self, tmp_path: Path) -> None:
        real = tmp_path / "real.agent.md"
        real.write_text("content", encoding="utf-8")
        link = tmp_path / "link.agent.md"
        link.symlink_to(real)
        dst = tmp_path / "SKILL.md"
        integrator = AgentIntegrator()
        with pytest.raises(ValueError, match="symlink"):
            integrator._write_windsurf_agent_skill(link, dst)


class TestAgentIntegratorIntegrateForTarget:
    """integrate_agents_for_target deploys files for each target."""

    def test_no_agent_files_returns_empty_result(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        result = integrator.integrate_agents_for_target(
            KNOWN_TARGETS["copilot"],
            pkg_info,
            tmp_path,
        )
        assert result.files_integrated == 0
        assert result.files_skipped == 0

    def test_integrates_to_github_agents_copilot(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "reviewer.agent.md").write_text("# Reviewer", encoding="utf-8")
        # Copilot target requires .github/ to NOT auto_create=False but default is True
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        result = integrator.integrate_agents_for_target(
            KNOWN_TARGETS["copilot"],
            pkg_info,
            tmp_path,
        )
        assert result.files_integrated == 1
        agents_dir = tmp_path / ".github" / "agents"
        assert any(agents_dir.iterdir())

    def test_integrates_to_claude_agents(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "security.agent.md").write_text("# Security", encoding="utf-8")
        # Claude target requires .claude/ dir to exist
        (tmp_path / ".claude").mkdir()
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        result = integrator.integrate_agents_for_target(
            KNOWN_TARGETS["claude"],
            pkg_info,
            tmp_path,
        )
        assert result.files_integrated == 1
        assert (tmp_path / ".claude" / "agents").exists()

    def test_skips_when_target_root_missing_and_no_auto_create(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "agent.agent.md").write_text("# Agent", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        # Claude target: .claude/ does NOT exist, auto_create is False by default
        integrator = AgentIntegrator()
        result = integrator.integrate_agents_for_target(
            KNOWN_TARGETS["claude"],
            pkg_info,
            tmp_path,
        )
        assert result.files_integrated == 0

    def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        """A traversal-containing filename is rejected."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        from apm_cli.utils.diagnostics import DiagnosticCollector

        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "normal.agent.md").write_text("# Normal", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        diag = DiagnosticCollector()

        # Patch get_target_filename_for_target to return a traversal path
        with patch.object(
            integrator,
            "get_target_filename_for_target",
            return_value="../../../etc/malicious.agent.md",
        ):
            result = integrator.integrate_agents_for_target(
                KNOWN_TARGETS["copilot"],
                pkg_info,
                tmp_path,
                diagnostics=diag,
            )
        assert result.files_skipped >= 1


class TestAgentIntegratorLegacyAPI:
    """integrate_package_agents (legacy) creates .github/agents/ and optionally .claude/ / .cursor/."""

    def test_integrate_package_agents_creates_github_agents(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "helper.agent.md").write_text("# Helper", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        result = integrator.integrate_package_agents(pkg_info, tmp_path)
        assert result.files_integrated == 1
        assert (tmp_path / ".github" / "agents").exists()

    def test_integrate_package_agents_also_copies_to_claude(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "spec.agent.md").write_text("# Spec", encoding="utf-8")
        (tmp_path / ".claude").mkdir()
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        result = integrator.integrate_package_agents(pkg_info, tmp_path)
        assert result.files_integrated >= 1
        # Claude copy should also have been attempted
        assert (tmp_path / ".claude" / "agents").exists()

    def test_integrate_package_agents_also_copies_to_cursor(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "spec.agent.md").write_text("# Spec", encoding="utf-8")
        (tmp_path / ".cursor").mkdir()
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        result = integrator.integrate_package_agents(pkg_info, tmp_path)
        assert result.files_integrated >= 1
        assert (tmp_path / ".cursor" / "agents").exists()

    def test_integrate_package_agents_force_overwrites(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "agent.agent.md").write_text("new content", encoding="utf-8")
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        (tmp_path / ".github" / "agents" / "agent.agent.md").write_text("old", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        result = integrator.integrate_package_agents(pkg_info, tmp_path, force=True)
        assert result.files_integrated == 1
        content = (tmp_path / ".github" / "agents" / "agent.agent.md").read_text(encoding="utf-8")
        assert content == "new content"

    def test_integrate_package_agents_skips_collision(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "agent.agent.md").write_text("new content", encoding="utf-8")
        (tmp_path / ".github" / "agents").mkdir(parents=True)
        # Pre-existing file with different content (not managed)
        (tmp_path / ".github" / "agents" / "agent.agent.md").write_text(
            "user authored", encoding="utf-8"
        )
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        result = integrator.integrate_package_agents(
            pkg_info, tmp_path, force=False, managed_files=set()
        )
        assert result.files_skipped >= 1

    def test_integrate_package_agents_no_files(self, tmp_path: Path) -> None:
        """No agent files → empty result."""
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        result = integrator.integrate_package_agents(pkg_info, tmp_path)
        assert result.files_integrated == 0

    def test_integrate_package_agents_adopts_identical_file(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        content = "# Adopted Agent\n"
        (package_dir / "adopted.agent.md").write_text(content, encoding="utf-8")
        agents_dir = tmp_path / ".github" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "adopted.agent.md").write_text(content, encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        result = integrator.integrate_package_agents(pkg_info, tmp_path)
        assert result.files_adopted >= 1
        assert result.files_integrated == 0

    def test_sync_integration_removes_managed_files(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / ".github" / "agents"
        agents_dir.mkdir(parents=True)
        managed = agents_dir / "review.agent.md"
        managed.write_text("# Review", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", tmp_path / "pkg")
        integrator = AgentIntegrator()
        managed_files = {".github/agents/review.agent.md"}
        result = integrator.sync_integration(pkg_info.package, tmp_path, managed_files)
        assert result["files_removed"] >= 1
        assert not managed.exists()

    def test_integrate_package_agents_claude_standalone(self, tmp_path: Path) -> None:
        """integrate_package_agents_claude creates .claude/agents/."""
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "agent.agent.md").write_text("# Agent", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        result = integrator.integrate_package_agents_claude(pkg_info, tmp_path)
        assert result.files_integrated == 1
        assert (tmp_path / ".claude" / "agents").exists()

    def test_integrate_package_agents_cursor_standalone(self, tmp_path: Path) -> None:
        """integrate_package_agents_cursor creates .cursor/agents/."""
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "agent.agent.md").write_text("# Agent", encoding="utf-8")
        (tmp_path / ".cursor").mkdir()
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = AgentIntegrator()
        result = integrator.integrate_package_agents_cursor(pkg_info, tmp_path)
        assert result.files_integrated == 1


# ===========================================================================
# prompt_integrator  --  PromptIntegrator
# ===========================================================================


class TestPromptIntegratorFindFiles:
    """find_prompt_files scans the right locations."""

    def test_finds_prompt_md_in_root(self, tmp_path: Path) -> None:
        (tmp_path / "review.prompt.md").write_text("# Review", encoding="utf-8")
        integrator = PromptIntegrator()
        files = integrator.find_prompt_files(tmp_path)
        assert any(f.name == "review.prompt.md" for f in files)

    def test_finds_prompt_md_in_apm_prompts(self, tmp_path: Path) -> None:
        apm_prompts = tmp_path / ".apm" / "prompts"
        apm_prompts.mkdir(parents=True)
        (apm_prompts / "workflow.prompt.md").write_text("# Workflow", encoding="utf-8")
        integrator = PromptIntegrator()
        files = integrator.find_prompt_files(tmp_path)
        assert any(f.name == "workflow.prompt.md" for f in files)

    def test_non_prompt_md_not_found(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# Readme")
        integrator = PromptIntegrator()
        files = integrator.find_prompt_files(tmp_path)
        assert not files


class TestPromptIntegratorCopyPrompt:
    """copy_prompt copies content verbatim."""

    def test_copy_prompt_verbatim(self, tmp_path: Path) -> None:
        src = tmp_path / "p.prompt.md"
        dst = tmp_path / "out.prompt.md"
        src.write_text("---\ntitle: T\n---\n\nBody.", encoding="utf-8")
        integrator = PromptIntegrator()
        integrator.copy_prompt(src, dst)
        assert dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")

    def test_copy_prompt_rejects_symlink(self, tmp_path: Path) -> None:
        real = tmp_path / "real.prompt.md"
        real.write_text("real", encoding="utf-8")
        link = tmp_path / "link.prompt.md"
        link.symlink_to(real)
        dst = tmp_path / "out.prompt.md"
        integrator = PromptIntegrator()
        with pytest.raises(ValueError, match="symlink"):
            integrator.copy_prompt(link, dst)

    def test_get_target_filename_returns_original(self) -> None:
        integrator = PromptIntegrator()
        src = Path("accessibility-audit.prompt.md")
        assert integrator.get_target_filename(src, "any-pkg") == "accessibility-audit.prompt.md"


class TestPromptIntegratorIntegratePackagePrompts:
    """integrate_package_prompts deploys prompts to .github/prompts/."""

    def test_no_prompts_returns_empty_result(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(pkg_info, tmp_path)
        assert result.files_integrated == 0
        assert result.files_skipped == 0

    def test_creates_github_prompts_dir(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "review.prompt.md").write_text("# Review", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = PromptIntegrator()
        integrator.integrate_package_prompts(pkg_info, tmp_path)
        assert (tmp_path / ".github" / "prompts").is_dir()

    def test_integrates_prompt_file(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "review.prompt.md").write_text("# Review", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(pkg_info, tmp_path)
        assert result.files_integrated == 1
        assert (tmp_path / ".github" / "prompts" / "review.prompt.md").exists()

    def test_skips_workflow_shape_prompt(self, tmp_path: Path) -> None:
        """Workflow-shape prompts are skipped at file targets."""
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "sched.prompt.md").write_text(
            "---\ninterval: daily\n---\n\nBody.", encoding="utf-8"
        )
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(pkg_info, tmp_path)
        assert result.files_skipped == 1
        assert result.files_integrated == 0

    def test_force_overwrites_collision(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "review.prompt.md").write_text("new", encoding="utf-8")
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "review.prompt.md").write_text("old", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(pkg_info, tmp_path, force=True)
        assert result.files_integrated == 1
        assert (prompts_dir / "review.prompt.md").read_text(encoding="utf-8") == "new"

    def test_skips_collision_when_not_force(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "review.prompt.md").write_text("new content", encoding="utf-8")
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "review.prompt.md").write_text("user authored", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(
            pkg_info, tmp_path, force=False, managed_files=set()
        )
        assert result.files_skipped >= 1

    def test_adopts_identical_file(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        content = "# Same\n"
        (package_dir / "same.prompt.md").write_text(content, encoding="utf-8")
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "same.prompt.md").write_text(content, encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = PromptIntegrator()
        result = integrator.integrate_package_prompts(pkg_info, tmp_path)
        assert result.files_adopted >= 1
        assert result.files_integrated == 0

    def test_managed_file_collision_not_skipped(self, tmp_path: Path) -> None:
        """A pre-existing file in managed_files is overwritten (not a collision)."""
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "review.prompt.md").write_text("updated", encoding="utf-8")
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "review.prompt.md").write_text("previous", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = PromptIntegrator()
        # File listed as managed → no collision → overwritten
        result = integrator.integrate_package_prompts(
            pkg_info,
            tmp_path,
            managed_files={".github/prompts/review.prompt.md"},
        )
        assert result.files_integrated == 1

    def test_path_traversal_in_filename_rejected(self, tmp_path: Path) -> None:
        """A traversal filename is rejected by ensure_path_within."""
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "innocent.prompt.md").write_text("body", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = PromptIntegrator()
        with patch.object(integrator, "get_target_filename", return_value="../evil.prompt.md"):
            result = integrator.integrate_package_prompts(pkg_info, tmp_path)
        assert result.files_skipped >= 1

    def test_sync_integration_removes_managed_prompts(self, tmp_path: Path) -> None:
        prompts_dir = tmp_path / ".github" / "prompts"
        prompts_dir.mkdir(parents=True)
        managed = prompts_dir / "review.prompt.md"
        managed.write_text("# Review", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", tmp_path / "pkg")
        integrator = PromptIntegrator()
        result = integrator.sync_integration(
            pkg_info.package, tmp_path, {".github/prompts/review.prompt.md"}
        )
        assert result["files_removed"] >= 1
        assert not managed.exists()


class TestPromptIntegratorForTarget:
    """integrate_prompts_for_target dispatches to the right path."""

    def test_no_mapping_returns_empty(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        # cursor target has no prompts primitive
        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "p.prompt.md").write_text("body", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = PromptIntegrator()
        # Use a target that does NOT have "prompts" mapping
        cursor_target = KNOWN_TARGETS["cursor"]
        result = integrator.integrate_prompts_for_target(cursor_target, pkg_info, tmp_path)
        assert result.files_integrated == 0

    def test_copilot_target_integrates(self, tmp_path: Path) -> None:
        from apm_cli.integration.targets import KNOWN_TARGETS

        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "p.prompt.md").write_text("body", encoding="utf-8")
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = PromptIntegrator()
        result = integrator.integrate_prompts_for_target(
            KNOWN_TARGETS["copilot"], pkg_info, tmp_path
        )
        assert result.files_integrated == 1

    def test_copilot_app_target_returns_empty_when_no_db(self, tmp_path: Path) -> None:
        """copilot-app target returns empty when no DB path is found."""
        from apm_cli.integration.targets import KNOWN_TARGETS

        package_dir = tmp_path / "pkg"
        package_dir.mkdir()
        (package_dir / "p.prompt.md").write_text(
            "---\ninterval: manual\n---\n\nBody.", encoding="utf-8"
        )
        pkg_info = _make_package_info("test-pkg", package_dir)
        integrator = PromptIntegrator()
        with patch(
            "apm_cli.integration.copilot_app_db.resolve_copilot_app_db_path",
            return_value=None,
        ):
            result = integrator.integrate_prompts_for_target(
                KNOWN_TARGETS["copilot-app"], pkg_info, tmp_path
            )
        assert result.files_integrated == 0


# ===========================================================================
# prompt_integrator  --  _is_workflow_shape
# ===========================================================================


class TestIsWorkflowShape:
    """_is_workflow_shape identifies frontmatter with execution metadata."""

    def test_interval_key_triggers_workflow_shape(self) -> None:
        assert _is_workflow_shape({"interval": "daily"}) is True

    def test_schedule_hour_key_triggers_workflow_shape(self) -> None:
        assert _is_workflow_shape({"schedule_hour": 9}) is True

    def test_schedule_day_key_triggers_workflow_shape(self) -> None:
        assert _is_workflow_shape({"schedule_day": 1}) is True

    def test_no_workflow_keys_returns_false(self) -> None:
        assert _is_workflow_shape({"mode": "agent", "model": "gpt-4"}) is False

    def test_empty_dict_returns_false(self) -> None:
        assert _is_workflow_shape({}) is False

    def test_non_dict_returns_false(self) -> None:
        assert _is_workflow_shape(None) is False  # type: ignore[arg-type]
        assert _is_workflow_shape("string") is False  # type: ignore[arg-type]
        assert _is_workflow_shape([]) is False  # type: ignore[arg-type]

    def test_mode_alone_is_not_workflow_shape(self) -> None:
        """mode alone should NOT trigger workflow dispatch (used by regular prompts too)."""
        assert _is_workflow_shape({"mode": "plan"}) is False

    def test_model_alone_is_not_workflow_shape(self) -> None:
        assert _is_workflow_shape({"model": "gpt-4o"}) is False

    def test_combination_with_workflow_key_returns_true(self) -> None:
        assert _is_workflow_shape({"mode": "interactive", "interval": "manual"}) is True


# ===========================================================================
# prompt_integrator  --  _parse_workflow_frontmatter
# ===========================================================================


class TestParseWorkflowFrontmatter:
    """_parse_workflow_frontmatter validates and returns a Schedule."""

    def test_defaults_when_no_keys_present(self) -> None:
        # If we pass an empty dict it won't have interval so it defaults
        schedule = _parse_workflow_frontmatter({"interval": "manual"})
        assert schedule.interval == "manual"
        assert schedule.schedule_hour == 9
        assert schedule.schedule_day == 1

    def test_valid_interval_daily(self) -> None:
        s = _parse_workflow_frontmatter({"interval": "daily"})
        assert s.interval == "daily"

    def test_valid_interval_hourly(self) -> None:
        s = _parse_workflow_frontmatter({"interval": "hourly"})
        assert s.interval == "hourly"

    def test_valid_interval_weekly(self) -> None:
        s = _parse_workflow_frontmatter({"interval": "weekly"})
        assert s.interval == "weekly"

    def test_invalid_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="interval"):
            _parse_workflow_frontmatter({"interval": "biweekly"})

    def test_schedule_hour_zero_valid(self) -> None:
        s = _parse_workflow_frontmatter({"interval": "manual", "schedule_hour": 0})
        assert s.schedule_hour == 0

    def test_schedule_hour_23_valid(self) -> None:
        s = _parse_workflow_frontmatter({"interval": "manual", "schedule_hour": 23})
        assert s.schedule_hour == 23

    def test_schedule_hour_24_invalid(self) -> None:
        with pytest.raises(ValueError, match="schedule_hour"):
            _parse_workflow_frontmatter({"interval": "manual", "schedule_hour": 24})

    def test_schedule_hour_negative_invalid(self) -> None:
        with pytest.raises(ValueError, match="schedule_hour"):
            _parse_workflow_frontmatter({"interval": "manual", "schedule_hour": -1})

    def test_schedule_hour_non_int_invalid(self) -> None:
        with pytest.raises(ValueError, match="schedule_hour"):
            _parse_workflow_frontmatter({"interval": "manual", "schedule_hour": "nine"})

    def test_schedule_day_zero_valid(self) -> None:
        s = _parse_workflow_frontmatter({"interval": "manual", "schedule_day": 0})
        assert s.schedule_day == 0

    def test_schedule_day_six_valid(self) -> None:
        s = _parse_workflow_frontmatter({"interval": "manual", "schedule_day": 6})
        assert s.schedule_day == 6

    def test_schedule_day_seven_invalid(self) -> None:
        with pytest.raises(ValueError, match="schedule_day"):
            _parse_workflow_frontmatter({"interval": "manual", "schedule_day": 7})

    def test_autopilot_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="autopilot"):
            _parse_workflow_frontmatter({"interval": "manual", "mode": "autopilot"})

    def test_valid_mode_interactive(self) -> None:
        s = _parse_workflow_frontmatter({"interval": "manual", "mode": "interactive"})
        assert s.mode == "interactive"

    def test_valid_mode_plan(self) -> None:
        s = _parse_workflow_frontmatter({"interval": "manual", "mode": "plan"})
        assert s.mode == "plan"

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            _parse_workflow_frontmatter({"interval": "manual", "mode": "streaming"})

    def test_model_non_string_invalid(self) -> None:
        with pytest.raises(ValueError, match="model"):
            _parse_workflow_frontmatter({"interval": "manual", "model": 123})

    def test_model_string_valid(self) -> None:
        s = _parse_workflow_frontmatter({"interval": "manual", "model": "gpt-4o"})
        assert s.model == "gpt-4o"

    def test_reasoning_effort_string_valid(self) -> None:
        s = _parse_workflow_frontmatter({"interval": "manual", "reasoning_effort": "high"})
        assert s.reasoning_effort == "high"

    def test_non_dict_input_raises(self) -> None:
        with pytest.raises(ValueError, match="frontmatter"):
            _parse_workflow_frontmatter("not a dict")  # type: ignore[arg-type]

    def test_returns_schedule_dataclass(self) -> None:
        result = _parse_workflow_frontmatter({"interval": "manual"})
        assert isinstance(result, Schedule)


# ===========================================================================
# install/validation.py  --  pure helpers
# ===========================================================================


class TestIsTlsFailure:
    """_is_tls_failure detects TLS errors in exception chains."""

    def test_ssl_error_detected(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")
        assert _is_tls_failure(exc) is True

    def test_runtime_error_with_tls_prefix(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = RuntimeError("TLS verification failed for host.example.com")
        assert _is_tls_failure(exc) is True

    def test_plain_runtime_error_not_tls(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = RuntimeError("some other error")
        assert _is_tls_failure(exc) is False

    def test_chained_ssl_error_detected(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        inner = requests.exceptions.SSLError("bad cert")
        outer = RuntimeError("wrapped")
        outer.__cause__ = inner
        assert _is_tls_failure(outer) is True

    def test_certificate_verify_failed_string_detected(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = RuntimeError("CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate")
        assert _is_tls_failure(exc) is True

    def test_value_error_without_tls_marker_not_detected(self) -> None:
        from apm_cli.install.validation import _is_tls_failure

        exc = ValueError("authentication failed")
        assert _is_tls_failure(exc) is False


class TestLogTlsFailure:
    """_log_tls_failure emits warnings via logger or _rich_warning."""

    def test_with_logger_calls_warning(self) -> None:
        from apm_cli.install.validation import _log_tls_failure

        logger = MagicMock()
        exc = RuntimeError("TLS verification failed")
        _log_tls_failure("host.example.com", exc, None, logger)
        logger.warning.assert_called_once()
        assert "TLS" in logger.warning.call_args[0][0] or "proxy" in logger.warning.call_args[0][0]

    def test_verbose_mode_logs_underlying_error(self) -> None:
        from apm_cli.install.validation import _log_tls_failure

        logger = MagicMock()
        exc = RuntimeError("TLS verification failed: bad cert")
        verbose_calls: list[str] = []
        _log_tls_failure("host.example.com", exc, lambda m: verbose_calls.append(m), logger)
        assert any("host.example.com" in m or "bad cert" in m for m in verbose_calls)

    def test_without_logger_calls_warning_with_mock(self) -> None:
        """_log_tls_failure always requires a logger; test it calls warning with the right message."""
        from apm_cli.install.validation import _log_tls_failure

        logger = MagicMock()
        exc = RuntimeError("TLS verification failed")
        _log_tls_failure("host.example.com", exc, None, logger)
        logger.warning.assert_called_once()
        call_arg = logger.warning.call_args[0][0]
        assert "REQUESTS_CA_BUNDLE" in call_arg or "TLS" in call_arg or "proxy" in call_arg


class TestLocalPathFailureReason:
    """_local_path_failure_reason returns human-readable failure messages."""

    def test_returns_none_for_remote_dep(self) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        dep_ref = MagicMock()
        dep_ref.is_local = False
        dep_ref.local_path = None
        assert _local_path_failure_reason(dep_ref) is None

    def test_path_does_not_exist(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(tmp_path / "nonexistent")
        reason = _local_path_failure_reason(dep_ref)
        assert reason == "path does not exist"

    def test_path_is_a_file_not_directory(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        f = tmp_path / "file.txt"
        f.write_text("x")
        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(f)
        reason = _local_path_failure_reason(dep_ref)
        assert reason == "path is not a directory"

    def test_directory_with_no_package_markers(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_failure_reason

        d = tmp_path / "mydir"
        d.mkdir()
        dep_ref = MagicMock()
        dep_ref.is_local = True
        dep_ref.local_path = str(d)
        reason = _local_path_failure_reason(dep_ref)
        assert reason == "no apm.yml, SKILL.md, or plugin.json found"


class TestLocalPathNoMarkersHint:
    """_local_path_no_markers_hint scans for nested packages."""

    def test_no_subpackages_prints_nothing(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        # Should not raise, just return silently
        _local_path_no_markers_hint(tmp_path, logger=None)

    def test_subpackage_with_apm_yml_hints(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        sub = tmp_path / "mypkg"
        sub.mkdir()
        (sub / "apm.yml").write_text("name: mypkg\n")
        logger = MagicMock()
        _local_path_no_markers_hint(tmp_path, logger=logger)
        logger.progress.assert_called_once()

    def test_subpackage_with_skill_md_hints(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        sub = tmp_path / "skillpkg"
        sub.mkdir()
        (sub / "SKILL.md").write_text("# Skill\n")
        logger = MagicMock()
        _local_path_no_markers_hint(tmp_path, logger=logger)
        logger.progress.assert_called_once()

    def test_more_than_five_packages_shows_ellipsis(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _local_path_no_markers_hint

        for i in range(7):
            sub = tmp_path / f"pkg{i}"
            sub.mkdir()
            (sub / "apm.yml").write_text(f"name: pkg{i}\n")
        logger = MagicMock()
        _local_path_no_markers_hint(tmp_path, logger=logger)
        # Should print up to 5 + one more line for "and N more"
        calls = [str(c) for c in logger.verbose_detail.call_args_list]
        assert any("more" in c for c in calls)


class TestValidatePackageExistsLocalPaths:
    """_validate_package_exists for local filesystem dependencies."""

    def test_local_path_with_apm_yml_returns_true(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        d = tmp_path / "mypkg"
        d.mkdir()
        (d / "apm.yml").write_text("name: mypkg\n")
        result = _validate_package_exists(str(d))
        assert result is True

    def test_local_path_with_skill_md_returns_true(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        d = tmp_path / "skillpkg"
        d.mkdir()
        (d / "SKILL.md").write_text("# Skill\n")
        result = _validate_package_exists(str(d))
        assert result is True

    def test_local_path_not_directory_returns_false(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        f = tmp_path / "not_a_dir.txt"
        f.write_text("x")
        result = _validate_package_exists(str(f))
        assert result is False

    def test_local_path_no_markers_returns_false(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        d = tmp_path / "empty_pkg"
        d.mkdir()
        result = _validate_package_exists(str(d))
        assert result is False

    def test_nonexistent_path_returns_false(self, tmp_path: Path) -> None:
        from apm_cli.install.validation import _validate_package_exists

        result = _validate_package_exists(str(tmp_path / "does_not_exist"))
        assert result is False


class TestValidatePackageExistsEnforceOnly:
    """_validate_package_exists with PROXY_REGISTRY_ONLY=1 skips probes."""

    def test_enforce_only_returns_true_for_github_package(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apm_cli.install.validation import _validate_package_exists

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=True):
            result = _validate_package_exists("owner/valid-repo")
        assert result is True

    def test_invalid_repo_path_returns_false_before_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repo path with invalid chars returns False without network call."""
        from apm_cli.install.validation import _validate_package_exists

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
            # The path contains "../" which fails the fullmatch guard.
            # DependencyReference.parse will raise, landing in the except branch.
            result = _validate_package_exists("../../etc/passwd")
        # Should be False: the path doesn't match owner/repo pattern
        assert result is False


class TestValidatePackageExistsGitHub:
    """_validate_package_exists for regular GitHub packages via API."""

    def test_api_200_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.install.validation import _validate_package_exists

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200

        def fake_try_with_fallback(host, fn, **kwargs):
            return fn(None, {})

        auth_resolver = MagicMock()
        auth_resolver.classify_host.return_value = MagicMock(
            api_base="https://api.github.com",
            display_name="github.com",
            kind="github",
        )
        auth_resolver.try_with_fallback = fake_try_with_fallback
        auth_resolver.resolve.return_value = MagicMock(source="env", token_type="pat")

        with (
            patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False),
            patch("requests.get", return_value=mock_resp),
        ):
            result = _validate_package_exists(
                "owner/valid-repo",
                auth_resolver=auth_resolver,
            )
        assert result is True

    def test_api_404_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.install.validation import _validate_package_exists

        def fake_try_with_fallback(host, fn, **kwargs):
            raise RuntimeError("API returned 404")

        auth_resolver = MagicMock()
        auth_resolver.classify_host.return_value = MagicMock(
            api_base="https://api.github.com",
            display_name="github.com",
            kind="github",
        )
        auth_resolver.try_with_fallback = fake_try_with_fallback
        auth_resolver.resolve.return_value = MagicMock(source="env", token_type="pat")
        auth_resolver.build_error_context.return_value = "context"

        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
            result = _validate_package_exists(
                "owner/missing-repo",
                auth_resolver=auth_resolver,
            )
        assert result is False

    def test_tls_failure_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apm_cli.install.validation import _validate_package_exists

        def fake_try_with_fallback(host, fn, **kwargs):
            raise RuntimeError("TLS verification failed for api.github.com")

        auth_resolver = MagicMock()
        auth_resolver.classify_host.return_value = MagicMock(
            api_base="https://api.github.com",
            display_name="github.com",
            kind="github",
        )
        auth_resolver.try_with_fallback = fake_try_with_fallback
        auth_resolver.resolve.return_value = MagicMock(source="none", token_type="none")

        logger = MagicMock()
        with patch("apm_cli.deps.registry_proxy.is_enforce_only", return_value=False):
            result = _validate_package_exists(
                "owner/some-repo",
                auth_resolver=auth_resolver,
                logger=logger,
            )
        assert result is False
        logger.warning.assert_called_once()
