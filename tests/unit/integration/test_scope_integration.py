"""Integration tests for scope-resolved targeting.

Verifies that resolve_targets() + integrators deploy files to the
correct paths at both project and user scope, across all targets.
Uses real integrators against temp directories -- no mocks.
"""

import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS, resolve_targets
from apm_cli.models.apm_package import APMPackage, PackageInfo
from apm_cli.models.dependency.types import GitReferenceType, ResolvedReference
from apm_cli.models.validation import PackageType


def _make_package_info(install_path, name="test-pkg"):
    """Create a minimal PackageInfo for testing."""
    package = APMPackage(
        name=name,
        version="1.0.0",
        package_path=install_path,
        source=f"github.com/test/{name}",
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
        package_type=PackageType.APM_PACKAGE,
    )


# -- Copilot scope resolution ------------------------------------------------


class TestCopilotScopeResolution:
    """Verify Copilot deploys to .github at project scope, .copilot at user scope."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_instruction_package(self, name="test-pkg"):
        pkg = self.project_root / "apm_modules" / name
        inst_dir = pkg / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "python.instructions.md").write_text(
            "---\napplyTo: '**/*.py'\n---\n\n# Python rules"
        )
        return _make_package_info(pkg, name)

    def test_project_scope_deploys_to_github(self):
        """At project scope, instructions deploy to .github/instructions/."""
        copilot = KNOWN_TARGETS["copilot"]
        resolved = copilot.for_scope(user_scope=False)
        assert resolved.root_dir == ".github"

        pkg_info = self._create_instruction_package()
        integrator = InstructionIntegrator()
        result = integrator.integrate_instructions_for_target(
            resolved,
            pkg_info,
            self.project_root,
        )

        assert result.files_integrated == 1
        deployed = (
            self.project_root
            / ".github"
            / "instructions"
            / "python.instructions.md"
        )
        assert deployed.exists()
        assert not (self.project_root / ".copilot").exists()

    def test_user_scope_deploys_to_copilot(self):
        """At user scope, instructions are filtered out (unsupported)."""
        copilot = KNOWN_TARGETS["copilot"]
        resolved = copilot.for_scope(user_scope=True)
        assert resolved.root_dir == ".copilot"
        assert "instructions" not in resolved.primitives

    def test_user_scope_agents_deploy_to_copilot(self):
        """At user scope, agents deploy to .copilot/agents/."""
        copilot = KNOWN_TARGETS["copilot"]
        resolved = copilot.for_scope(user_scope=True)
        (self.project_root / ".copilot").mkdir()

        pkg = self.project_root / "apm_modules" / "test-pkg"
        agents_dir = pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "reviewer.agent.md").write_text("# Reviewer agent")
        pkg_info = _make_package_info(pkg)

        integrator = AgentIntegrator()
        result = integrator.integrate_agents_for_target(
            resolved,
            pkg_info,
            self.project_root,
        )

        assert result.files_integrated == 1
        assert (
            self.project_root / ".copilot" / "agents" / "reviewer.agent.md"
        ).exists()
        assert not (self.project_root / ".github" / "agents").exists()


# -- OpenCode scope resolution -----------------------------------------------


class TestOpenCodeScopeResolution:
    """Verify OpenCode handles .config/opencode multi-level root at user scope."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_user_scope_resolves_to_config_opencode(self):
        opencode = KNOWN_TARGETS["opencode"]
        resolved = opencode.for_scope(user_scope=True)
        assert resolved.root_dir == ".config/opencode"

    def test_user_scope_agents_deploy_to_config_opencode(self):
        opencode = KNOWN_TARGETS["opencode"]
        resolved = opencode.for_scope(user_scope=True)
        (self.project_root / ".config" / "opencode").mkdir(parents=True)

        pkg = self.project_root / "apm_modules" / "test-pkg"
        agents_dir = pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "helper.agent.md").write_text("# Helper")
        pkg_info = _make_package_info(pkg)

        integrator = AgentIntegrator()
        result = integrator.integrate_agents_for_target(
            resolved,
            pkg_info,
            self.project_root,
        )

        assert result.files_integrated == 1
        # opencode agents mapping uses .md extension, not .agent.md
        expected = (
            self.project_root
            / ".config"
            / "opencode"
            / "agents"
            / "helper.md"
        )
        assert expected.exists()
        assert not (self.project_root / ".opencode" / "agents").exists()

    def test_project_scope_agents_deploy_to_opencode(self):
        opencode = KNOWN_TARGETS["opencode"]
        resolved = opencode.for_scope(user_scope=False)
        (self.project_root / ".opencode").mkdir()

        pkg = self.project_root / "apm_modules" / "test-pkg"
        agents_dir = pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "helper.agent.md").write_text("# Helper")
        pkg_info = _make_package_info(pkg)

        integrator = AgentIntegrator()
        result = integrator.integrate_agents_for_target(
            resolved,
            pkg_info,
            self.project_root,
        )

        assert result.files_integrated == 1
        # opencode agents mapping uses .md extension
        assert (
            self.project_root / ".opencode" / "agents" / "helper.md"
        ).exists()


# -- Codex user-scope behavior ----------------------------------------------


class TestCodexUserScope:
    """Verify Codex participates in user-scope target resolution."""

    def test_for_scope_returns_profile(self):
        codex = KNOWN_TARGETS["codex"]
        assert codex.user_supported == "partial"
        resolved = codex.for_scope(user_scope=True)
        assert resolved is not None
        assert resolved.root_dir == ".codex"
        assert "agents" in resolved.primitives
        assert "skills" in resolved.primitives
        assert "hooks" in resolved.primitives

    def test_resolve_targets_includes_codex_at_user_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            targets = resolve_targets(root, user_scope=True, explicit_target="all")
            names = {t.name for t in targets}
            assert "codex" in names


# -- Claude same-root behavior -----------------------------------------------


class TestClaudeScopeResolution:
    """Verify Claude uses .claude at both scopes (user_root_dir is None)."""

    def test_project_and_user_scope_same_root(self):
        claude = KNOWN_TARGETS["claude"]
        project = claude.for_scope(user_scope=False)
        user = claude.for_scope(user_scope=True)
        assert project.root_dir == ".claude"
        assert user.root_dir == ".claude"

    def test_all_primitives_available_at_user_scope(self):
        claude = KNOWN_TARGETS["claude"]
        resolved = claude.for_scope(user_scope=True)
        # Claude supports all primitives at user scope
        assert "instructions" in resolved.primitives
        assert "agents" in resolved.primitives


# -- resolve_targets consistency ----------------------------------------------


class TestResolveTargetsConsistency:
    """Verify resolve_targets produces correct profiles for all targets."""

    def test_all_targets_at_user_scope_have_correct_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            targets = resolve_targets(
                Path(tmp), user_scope=True, explicit_target="all"
            )
            root_map = {t.name: t.root_dir for t in targets}
            # Codex keeps .codex at user scope
            assert root_map["codex"] == ".codex"
            # Copilot should use .copilot
            if "copilot" in root_map:
                assert root_map["copilot"] == ".copilot"
            # Claude should use .claude
            if "claude" in root_map:
                assert root_map["claude"] == ".claude"
            # OpenCode should use .config/opencode
            if "opencode" in root_map:
                assert root_map["opencode"] == ".config/opencode"

    def test_unsupported_primitives_filtered_at_user_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            targets = resolve_targets(
                Path(tmp), user_scope=True, explicit_target="all"
            )
            for t in targets:
                if t.name == "copilot":
                    assert "prompts" not in t.primitives
                    assert "instructions" not in t.primitives
                if t.name == "cursor":
                    assert "instructions" not in t.primitives
                if t.name == "opencode":
                    assert "hooks" not in t.primitives

    def test_project_scope_preserves_all_primitives(self):
        with tempfile.TemporaryDirectory() as tmp:
            targets = resolve_targets(
                Path(tmp), user_scope=False, explicit_target="all"
            )
            copilot = next(t for t in targets if t.name == "copilot")
            assert "prompts" in copilot.primitives
            assert "instructions" in copilot.primitives


# -- Skill deploy at user scope ----------------------------------------------


class TestSkillScopeDeployment:
    """Verify skills deploy to scope-resolved paths."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_skill_deploys_to_copilot_at_user_scope(self):
        copilot = KNOWN_TARGETS["copilot"]
        resolved = copilot.for_scope(user_scope=True)
        (self.project_root / ".copilot").mkdir()

        pkg = self.project_root / "apm_modules" / "my-skill"
        pkg.mkdir(parents=True)
        (pkg / "SKILL.md").write_text("---\nname: my-skill\n---\n# Skill")
        pkg_info = _make_package_info(pkg, "my-skill")
        pkg_info = PackageInfo(
            package=pkg_info.package,
            install_path=pkg_info.install_path,
            resolved_reference=pkg_info.resolved_reference,
            installed_at=pkg_info.installed_at,
            package_type=PackageType.CLAUDE_SKILL,
        )

        integrator = SkillIntegrator()
        result = integrator.integrate_package_skill(
            pkg_info,
            self.project_root,
            targets=[resolved],
        )

        assert result.skill_created
        assert (
            self.project_root / ".copilot" / "skills" / "my-skill" / "SKILL.md"
        ).exists()
        assert not (self.project_root / ".github" / "skills").exists()


# -- auto_create guard -------------------------------------------------------


class TestAutoCreateGuard:
    """Verify auto_create=False targets don't create directories."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.project_root = Path(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_auto_create_false_skips_when_dir_missing(self):
        """Targets with auto_create=False skip when root doesn't exist."""
        opencode = KNOWN_TARGETS["opencode"]
        assert opencode.auto_create is False
        # Do NOT create .opencode/

        pkg = self.project_root / "apm_modules" / "test-pkg"
        agents_dir = pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "helper.agent.md").write_text("# Helper")
        pkg_info = _make_package_info(pkg)

        integrator = AgentIntegrator()
        result = integrator.integrate_agents_for_target(
            opencode,
            pkg_info,
            self.project_root,
        )

        assert result.files_integrated == 0
        assert not (self.project_root / ".opencode").exists()

    def test_auto_create_true_creates_dir(self):
        """Copilot with auto_create=True creates .github/ if absent."""
        copilot = KNOWN_TARGETS["copilot"]
        assert copilot.auto_create is True

        pkg = self.project_root / "apm_modules" / "test-pkg"
        agents_dir = pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "helper.agent.md").write_text("# Helper")
        pkg_info = _make_package_info(pkg)

        integrator = AgentIntegrator()
        result = integrator.integrate_agents_for_target(
            copilot,
            pkg_info,
            self.project_root,
        )

        assert result.files_integrated == 1
        assert (
            self.project_root / ".github" / "agents" / "helper.agent.md"
        ).exists()
