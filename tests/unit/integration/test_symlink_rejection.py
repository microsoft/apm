"""Regression tests for symlink rejection in prompt/agent integrators.

Verifies that find_prompt_files() and find_agent_files() reject symlinks,
preventing supply-chain file disclosure attacks via malicious APM packages.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator


@pytest.fixture
def package_with_symlinks(tmp_path: Path) -> Path:
    """Create a fixture package with symlinks under .apm/ directories."""
    pkg = tmp_path / "pkg"
    (pkg / ".apm" / "prompts").mkdir(parents=True)
    (pkg / ".apm" / "agents").mkdir(parents=True)
    (pkg / ".apm" / "chatmodes").mkdir(parents=True)

    # Create a sentinel file outside the package
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("REGRESSION-SENTINEL-CONTENT")

    # Create legitimate files
    (pkg / ".apm" / "prompts" / "legit.prompt.md").write_text("legit prompt")
    (pkg / ".apm" / "agents" / "legit.agent.md").write_text("legit agent")
    (pkg / ".apm" / "chatmodes" / "legit.chatmode.md").write_text("legit chatmode")

    # Create symlinks pointing outside
    (pkg / ".apm" / "prompts" / "leak.prompt.md").symlink_to(sentinel)
    (pkg / ".apm" / "agents" / "leak.agent.md").symlink_to(sentinel)
    (pkg / ".apm" / "chatmodes" / "leak.chatmode.md").symlink_to(sentinel)

    # Create a symlink with absolute path target
    (pkg / "abs.agent.md").symlink_to(sentinel)

    return pkg


class TestPromptIntegratorSymlinkRejection:
    """Verify PromptIntegrator rejects symlinked files."""

    def test_find_prompt_files_excludes_symlinks(self, package_with_symlinks: Path) -> None:
        integrator = PromptIntegrator()
        result = integrator.find_prompt_files(package_with_symlinks)

        # Should find the legit file but not the symlink
        assert all(not p.is_symlink() for p in result)
        assert not any(p.name == "leak.prompt.md" for p in result)
        assert any(p.name == "legit.prompt.md" for p in result)

    def test_copy_prompt_rejects_symlink_source(
        self, package_with_symlinks: Path, tmp_path: Path
    ) -> None:
        integrator = PromptIntegrator()
        symlink_source = package_with_symlinks / ".apm" / "prompts" / "leak.prompt.md"
        target = tmp_path / "output.prompt.md"

        with pytest.raises(ValueError, match=r"symlink"):
            integrator.copy_prompt(symlink_source, target)


class TestAgentIntegratorSymlinkRejection:
    """Verify AgentIntegrator rejects symlinked files."""

    def test_find_agent_files_excludes_symlinks(self, package_with_symlinks: Path) -> None:
        integrator = AgentIntegrator()
        result = integrator.find_agent_files(package_with_symlinks)

        # Should find legit files but not symlinks
        assert all(not p.is_symlink() for p in result)
        assert not any(p.name == "leak.agent.md" for p in result)
        assert not any(p.name == "leak.chatmode.md" for p in result)
        assert not any(p.name == "abs.agent.md" for p in result)
        assert any(p.name == "legit.agent.md" for p in result)
        assert any(p.name == "legit.chatmode.md" for p in result)

    def test_copy_agent_rejects_symlink_source(
        self, package_with_symlinks: Path, tmp_path: Path
    ) -> None:
        integrator = AgentIntegrator()
        symlink_source = package_with_symlinks / ".apm" / "agents" / "leak.agent.md"
        target = tmp_path / "output.agent.md"

        with pytest.raises(ValueError, match=r"symlink"):
            integrator.copy_agent(symlink_source, target)


class TestHardlinkRejection:
    """Verify integrators reject hardlinked files."""

    @pytest.mark.skipif(os.name == "nt", reason="Hardlinks may require privileges on Windows")
    def test_find_prompt_files_excludes_hardlinks(self, tmp_path: Path) -> None:
        pkg = tmp_path / "pkg"
        (pkg / ".apm" / "prompts").mkdir(parents=True)

        # Create a file and a hardlink to it
        original = tmp_path / "original.txt"
        original.write_text("hardlink content")
        hardlink = pkg / ".apm" / "prompts" / "linked.prompt.md"
        os.link(original, hardlink)

        integrator = PromptIntegrator()
        result = integrator.find_prompt_files(pkg)

        # Hardlink has st_nlink > 1, should be rejected
        assert not any(p.name == "linked.prompt.md" for p in result)
