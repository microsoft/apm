"""E2E integration tests for plugin support.

Hero-scenario tests — no mocks for Class 1 (real filesystem), real network
for Class 2. Verifies the full plugin lifecycle from detection through
integrator deployment, orphan detection, and CLI round-trips.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    PackageType,
    ResolvedReference,
    validate_apm_package,
)
from apm_cli.utils.helpers import find_plugin_json

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "mock-marketplace-plugin"


def _make_package_info(
    package: APMPackage, install_path: Path, package_type: PackageType
) -> PackageInfo:
    """Build a PackageInfo with a dummy resolved reference."""
    return PackageInfo(
        package=package,
        install_path=install_path,
        resolved_reference=ResolvedReference(
            original_ref="main",
            ref_type=GitReferenceType.BRANCH,
            resolved_commit="abcdef1234567890abcdef1234567890abcdef12",
            ref_name="main",
        ),
        installed_at=datetime.now().isoformat(),
        package_type=package_type,
    )


def _run_integrators(package_info: PackageInfo, project_root: Path):
    """Run all four integrators against a package."""
    prompt_result = PromptIntegrator().integrate_package_prompts(package_info, project_root)
    agent_result = AgentIntegrator().integrate_package_agents(package_info, project_root)
    skill_result = SkillIntegrator().integrate_package_skill(package_info, project_root)
    command_result = CommandIntegrator().integrate_package_commands(package_info, project_root)
    return prompt_result, agent_result, skill_result, command_result


# ===========================================================================
# Class 1 — LOCAL tests (no network, uses mock fixture)
# ===========================================================================


class TestPluginHeroScenarios:
    """Local hero-scenario tests using the mock-marketplace-plugin fixture."""

    # ---- Test 1: Full lifecycle -----------------------------------------

    def test_full_lifecycle_install_to_deploy(self, tmp_path):
        """Complete hero: detect → validate → normalize → integrate → deploy."""
        if not FIXTURE_DIR.exists():
            pytest.skip("mock-marketplace-plugin fixture not found")

        # 1. Copy fixture & validate
        plugin_dir = tmp_path / "mock-marketplace-plugin"
        shutil.copytree(FIXTURE_DIR, plugin_dir)

        result = validate_apm_package(plugin_dir)
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN
        assert result.is_valid, f"Validation errors: {result.errors}"
        assert result.package is not None

        # 2. Verify synthesized apm.yml
        assert (plugin_dir / "apm.yml").exists(), "apm.yml should be synthesized"

        # 3. Verify .apm/ structure
        apm_dir = plugin_dir / ".apm"
        assert apm_dir.exists()
        assert (apm_dir / "agents" / "test-agent.agent.md").exists()
        assert (apm_dir / "skills" / "test-skill" / "SKILL.md").exists()
        assert (apm_dir / "prompts" / "test-command.prompt.md").exists()

        # 4. Set up a project root and run integrators
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / ".github").mkdir()

        pkg_info = _make_package_info(result.package, plugin_dir, result.package_type)
        prompt_r, agent_r, skill_r, command_r = _run_integrators(pkg_info, project_root)  # noqa: RUF059

        # 5. Assert scattered files
        assert (project_root / ".github" / "prompts" / "test-command.prompt.md").exists()
        assert (project_root / ".github" / "agents" / "test-agent.agent.md").exists()
        assert (project_root / ".github" / "skills" / "test-skill" / "SKILL.md").exists()

    # ---- Test 2: No false orphans after install -------------------------

    def test_no_false_orphans_after_install(self, tmp_path):
        """Scattered sub-skills must NOT appear as independent packages."""
        if not FIXTURE_DIR.exists():
            pytest.skip("mock-marketplace-plugin fixture not found")

        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / ".github").mkdir()

        # Create apm.yml declaring the plugin as a dependency
        apm_yml = project_root / "apm.yml"
        apm_yml.write_text(
            "name: orphan-test\n"
            "version: 1.0.0\n"
            "dependencies:\n"
            "  apm:\n"
            "    - microsoft/apm-test-plugin\n"
        )

        # Install the fixture into apm_modules
        apm_modules = project_root / "apm_modules" / "microsoft" / "apm-test-plugin"
        apm_modules.mkdir(parents=True)
        shutil.copytree(FIXTURE_DIR, apm_modules, dirs_exist_ok=True)

        # Validate and integrate
        result = validate_apm_package(apm_modules)
        assert result.is_valid
        pkg_info = _make_package_info(result.package, apm_modules, result.package_type)
        _run_integrators(pkg_info, project_root)

        # Walk apm_modules the same way deps.py list_packages() does
        modules_root = project_root / "apm_modules"
        false_orphans = []
        for candidate in modules_root.rglob("*"):
            if not candidate.is_dir() or candidate.name.startswith("."):
                continue
            has_apm = (candidate / "apm.yml").exists()
            has_skill = (candidate / "SKILL.md").exists()
            if not has_apm and not has_skill:
                continue
            rel_parts = candidate.relative_to(modules_root).parts
            if len(rel_parts) < 2:
                continue
            # Skip sub-components inside .apm/
            if ".apm" in rel_parts:
                continue
            # Skip sub-components nested inside a parent with apm.yml
            if has_skill and not has_apm:
                is_sub = False
                check = candidate.parent
                while check != modules_root and check != check.parent:  # noqa: PLR1714
                    if (check / "apm.yml").exists():
                        is_sub = True
                        break
                    check = check.parent
                if is_sub:
                    continue
            org_repo = "/".join(rel_parts)
            if org_repo != "microsoft/apm-test-plugin":
                false_orphans.append(org_repo)

        assert false_orphans == [], f"False orphan packages detected: {false_orphans}"

    # ---- Test 3: Empty dir rejected -------------------------------------

    def test_empty_dir_rejected(self, tmp_path):
        """An empty directory must not validate as a valid package."""
        empty_dir = tmp_path / "empty-plugin"
        empty_dir.mkdir()

        result = validate_apm_package(empty_dir)
        assert not result.is_valid, "Empty directory should be invalid"
        assert len(result.errors) > 0

    # ---- Test 4: Symlinks not followed ----------------------------------

    @pytest.mark.skipif(
        sys.platform == "win32", reason="Symlinks require admin privileges on Windows"
    )
    def test_symlinks_not_followed(self, tmp_path):
        """Symlinks inside plugin dirs must NOT be dereferenced during copytree."""
        plugin_dir = tmp_path / "symlink-plugin"
        plugin_dir.mkdir()

        # plugin.json so it's detected as a plugin
        (plugin_dir / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "Symlink Plugin",
                    "version": "1.0.0",
                    "description": "Plugin with a symlink",
                }
            )
        )

        # agents/ with a symlink pointing at an external file
        agents_dir = plugin_dir / "agents"
        agents_dir.mkdir()
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text("TOP SECRET DATA")
        os.symlink(str(secret_file), str(agents_dir / "secret-link.txt"))

        result = validate_apm_package(plugin_dir)
        assert result.package_type == PackageType.MARKETPLACE_PLUGIN

        # After normalization, .apm/agents/ should have the symlink as-is
        # (or not at all) — never the resolved target content
        apm_agents = plugin_dir / ".apm" / "agents"
        if apm_agents.exists():
            for f in apm_agents.iterdir():
                if f.name == "secret-link.txt":
                    # Either it's still a symlink or it was skipped — both OK.
                    # What is NOT OK is that it's a regular file with "TOP SECRET".
                    if f.is_file() and not f.is_symlink():
                        content = f.read_text()
                        assert "TOP SECRET" not in content, (
                            "Symlink was followed and target content leaked into .apm/"
                        )

    # ---- Test 5: find_plugin_json deterministic -------------------------

    def test_find_plugin_json_deterministic(self, tmp_path):
        """Root plugin.json wins; node_modules is never found."""
        pkg = tmp_path / "plugin-prio"
        pkg.mkdir()

        # Root plugin.json
        (pkg / "plugin.json").write_text('{"name":"root"}')

        # .github/plugin/plugin.json
        (pkg / ".github" / "plugin").mkdir(parents=True)
        (pkg / ".github" / "plugin" / "plugin.json").write_text('{"name":"github"}')

        # Deeply nested node_modules — should never be found
        nm = pkg / "node_modules" / "foo"
        nm.mkdir(parents=True)
        (nm / "plugin.json").write_text('{"name":"node_modules"}')

        found = find_plugin_json(pkg)
        assert found is not None
        assert found == pkg / "plugin.json", "Root plugin.json should win"

        # Remove root, .github/plugin/ should win next
        (pkg / "plugin.json").unlink()
        found2 = find_plugin_json(pkg)
        assert found2 == pkg / ".github" / "plugin" / "plugin.json"

        # Remove .github/plugin/ version — node_modules should NOT be found
        (pkg / ".github" / "plugin" / "plugin.json").unlink()
        found3 = find_plugin_json(pkg)
        assert found3 is None, "node_modules plugin.json must never be discovered"

    # ---- Test 6: deps info virtual subpath ------------------------------

    def test_deps_info_virtual_subpath(self, tmp_path):
        """Direct-path lookup in deps info() resolves deep sub-path packages."""
        project_root = tmp_path / "project"
        apm_modules = project_root / "apm_modules"

        # Set up a virtual subpath package (4-level deep)
        pkg_path = apm_modules / "github" / "awesome-copilot" / "plugins" / "context-engineering"
        pkg_path.mkdir(parents=True)
        (pkg_path / "apm.yml").write_text(
            "name: context-engineering\nversion: 2.0.0\ndescription: Context engineering plugin\n"
        )
        (pkg_path / "SKILL.md").write_text("---\nname: context-engineering\n---\n# Skill")

        # Exercise the same direct_match lookup that deps info() uses
        package = "github/awesome-copilot/plugins/context-engineering"
        direct_match = apm_modules / package
        assert direct_match.is_dir()
        assert (direct_match / "apm.yml").exists() or (direct_match / "SKILL.md").exists(), (
            "direct_match lookup must find deep sub-path package"
        )

        # Parse the resolved package
        pkg = APMPackage.from_apm_yml(direct_match / "apm.yml")
        assert pkg.name == "context-engineering"
        assert pkg.version == "2.0.0"

    # ---- Test 7: compile discovers plugin primitives --------------------

    def test_compile_discovers_plugin_primitives(self, tmp_path):
        """Compile primitive discovery should find .apm/ content from plugins."""
        from apm_cli.primitives.discovery import discover_primitives_with_dependencies

        if not FIXTURE_DIR.exists():
            pytest.skip("mock-marketplace-plugin fixture not found")

        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / ".github").mkdir()

        # Set up apm_modules with the plugin
        pkg_dir = project_root / "apm_modules" / "microsoft" / "apm-test-plugin"
        pkg_dir.mkdir(parents=True)
        shutil.copytree(FIXTURE_DIR, pkg_dir, dirs_exist_ok=True)

        # Validate to trigger normalization (creates .apm/ and apm.yml)
        result = validate_apm_package(pkg_dir)
        assert result.is_valid

        # Create apm.yml declaring the dependency
        (project_root / "apm.yml").write_text(
            "name: compile-test\nversion: 1.0.0\n"
            "dependencies:\n  apm:\n    - microsoft/apm-test-plugin\n"
        )

        # Run primitive discovery (what compile uses internally)
        collection = discover_primitives_with_dependencies(str(project_root))

        # Plugin primitives should appear (agents/ and any instructions in .apm/)
        all_sources = [p.source for p in collection.all_primitives()]
        has_dep_source = any("dependency:microsoft/apm-test-plugin" in s for s in all_sources)
        assert has_dep_source, f"Plugin primitives not discovered. Sources found: {all_sources}"

    # ---- Test 8: lockfile package_type round-trip -----------------------

    def test_lockfile_package_type_roundtrip(self, tmp_path):
        """LockedDependency.package_type serializes and deserializes correctly."""
        from apm_cli.deps.lockfile import LockedDependency

        dep = LockedDependency(
            repo_url="microsoft/apm-test-plugin",
            resolved_commit="abc123",
            package_type="marketplace_plugin",
            deployed_files=[".github/agents/test.agent.md"],
        )

        serialized = dep.to_dict()
        assert serialized["package_type"] == "marketplace_plugin"

        restored = LockedDependency.from_dict(serialized)
        assert restored.package_type == "marketplace_plugin"
        assert restored.deployed_files == [".github/agents/test.agent.md"]

    # ---- Test 9: generated apm.yml always has type: hybrid --------------

    def test_generated_apm_yml_type_is_hybrid(self, tmp_path):
        """Synthesized apm.yml should always emit type: hybrid (dead metadata)."""
        import yaml as yaml_lib

        if not FIXTURE_DIR.exists():
            pytest.skip("mock-marketplace-plugin fixture not found")

        plugin_dir = tmp_path / "type-test-plugin"
        shutil.copytree(FIXTURE_DIR, plugin_dir)

        result = validate_apm_package(plugin_dir)
        assert result.is_valid

        parsed = yaml_lib.safe_load((plugin_dir / "apm.yml").read_text())
        assert parsed["type"] == "hybrid", f"Expected type 'hybrid', got '{parsed.get('type')}'"


# ===========================================================================
# Class 2 — NETWORK E2E tests (real CLI, requires GitHub token)
# ===========================================================================

pytestmark_network = pytest.mark.skipif(
    not os.environ.get("GITHUB_APM_PAT") and not os.environ.get("GITHUB_TOKEN"),
    reason="GITHUB_APM_PAT or GITHUB_TOKEN required for GitHub API access",
)


@pytest.fixture
def apm_command():
    """Get the path to the APM CLI executable."""
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        return apm_on_path
    venv_apm = Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm"
    if venv_apm.exists():
        return str(venv_apm)
    return "apm"


@pytest.fixture
def temp_project(tmp_path):
    """Create a temporary APM project."""
    project_dir = tmp_path / "e2e-project"
    project_dir.mkdir()
    apm_yml = project_dir / "apm.yml"
    apm_yml.write_text(
        "name: e2e-test-project\n"
        "version: 1.0.0\n"
        "description: E2E test project for plugin support\n"
        "dependencies:\n"
        "  apm: []\n"
    )
    (project_dir / ".github").mkdir()
    return project_dir


@pytestmark_network
class TestPluginNetworkE2E:
    """Network E2E tests — real CLI installs from GitHub."""

    PLUGIN_REF = "github/awesome-copilot/plugins/context-engineering"

    # ---- Test 1: install real plugin ------------------------------------

    def test_install_real_plugin(self, apm_command, temp_project):
        """Install a real plugin from GitHub, verify artifacts on disk."""
        result = subprocess.run(
            [apm_command, "install", self.PLUGIN_REF, "--verbose"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )
        assert result.returncode == 0, (
            f"apm install failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        # Installed directory exists
        pkg_path = temp_project / "apm_modules" / self.PLUGIN_REF
        assert pkg_path.is_dir(), f"Expected {pkg_path} to exist"

        # apm.yml synthesized
        assert (pkg_path / "apm.yml").exists(), "apm.yml should be synthesized"

        # Lock file created
        assert (temp_project / "apm.lock.yaml").exists(), "apm.lock.yaml should be created"

        # Skills scattered to .github/skills/
        skills_dir = temp_project / ".github" / "skills"
        if skills_dir.exists():
            skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
            assert len(skill_dirs) > 0, "At least one skill should be scattered"

    # ---- Test 2: deps list — no false orphans ---------------------------

    def test_deps_list_no_false_orphans(self, apm_command, temp_project):
        """After install, deps list should show the plugin without orphan warnings."""
        # Install first
        subprocess.run(
            [apm_command, "install", self.PLUGIN_REF, "--verbose"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )

        result = subprocess.run(
            [apm_command, "deps", "list"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=60,
        )
        assert result.returncode == 0, (
            f"deps list failed (rc={result.returncode}):\n{result.stderr}"
        )

        combined = result.stdout + result.stderr
        assert "orphan" not in combined.lower(), (
            f"False orphan detected in deps list output:\n{combined}"
        )

    # ---- Test 3: deps tree shows plugin ---------------------------------

    def test_deps_tree_shows_plugin(self, apm_command, temp_project):
        """deps tree output should contain the plugin reference."""
        subprocess.run(
            [apm_command, "install", self.PLUGIN_REF, "--verbose"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )

        result = subprocess.run(
            [apm_command, "deps", "tree"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=60,
        )
        assert result.returncode == 0, (
            f"deps tree failed (rc={result.returncode}):\n{result.stderr}"
        )

        combined = result.stdout + result.stderr
        assert "context-engineering" in combined, (
            f"Plugin not found in deps tree output:\n{combined}"
        )

    # ---- Test 4: mixed dependencies (plugin + skill) --------------------

    def test_install_mixed_dependencies(self, apm_command, temp_project):
        """Install a plugin AND a regular skill together."""
        apm_yml = temp_project / "apm.yml"
        apm_yml.write_text(
            "name: e2e-test-project\n"
            "version: 1.0.0\n"
            "dependencies:\n"
            "  apm:\n"
            f"    - {self.PLUGIN_REF}\n"
            "    - github/awesome-copilot/skills/review-and-refactor\n"
        )

        result = subprocess.run(
            [apm_command, "install", "--verbose"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )
        assert result.returncode == 0, (
            f"mixed install failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        # Both packages installed
        assert (temp_project / "apm_modules" / self.PLUGIN_REF).is_dir()
        review_path = (
            temp_project
            / "apm_modules"
            / "github"
            / "awesome-copilot"
            / "skills"
            / "review-and-refactor"
        )
        # The skill may be installed as a virtual subdir or flattened — check either
        skill_installed = review_path.is_dir() or any(
            (temp_project / "apm_modules" / "github").rglob("review-and-refactor")
        )
        assert skill_installed, "review-and-refactor skill should be installed"

        # deps list — no orphans
        list_result = subprocess.run(
            [apm_command, "deps", "list"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=60,
        )
        combined = list_result.stdout + list_result.stderr
        assert "orphan" not in combined.lower(), f"False orphan in mixed install:\n{combined}"

    # ---- Test 5: uninstall plugin ---------------------------------------

    def test_uninstall_plugin(self, apm_command, temp_project):
        """Uninstall a plugin — directory and scattered files cleaned up."""
        # Install first
        subprocess.run(
            [apm_command, "install", self.PLUGIN_REF, "--verbose"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )
        pkg_path = temp_project / "apm_modules" / self.PLUGIN_REF
        assert pkg_path.is_dir(), "Plugin must be installed before uninstall test"

        # Uninstall
        result = subprocess.run(
            [apm_command, "uninstall", self.PLUGIN_REF],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=60,
        )
        assert result.returncode == 0, (
            f"uninstall failed (rc={result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

        # Package directory should be gone
        assert not pkg_path.exists(), "Plugin directory should be removed after uninstall"

    # ---- Test 6: lockfile preserved on sequential installs ---------------

    def test_lockfile_preserved_on_sequential_install(self, apm_command, temp_project):
        """Installing packages one at a time must preserve previous lockfile entries."""
        skill_ref = "github/awesome-copilot/skills/review-and-refactor"

        # Install plugin
        r1 = subprocess.run(
            [apm_command, "install", self.PLUGIN_REF],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )
        assert r1.returncode == 0, f"First install failed:\n{r1.stderr}"

        # Install skill separately
        r2 = subprocess.run(
            [apm_command, "install", skill_ref],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )
        assert r2.returncode == 0, f"Second install failed:\n{r2.stderr}"

        # Lockfile should contain BOTH entries
        import yaml

        lockfile = yaml.safe_load((temp_project / "apm.lock.yaml").read_text())
        dep_keys = {
            f"{d['repo_url']}/{d.get('virtual_path', '')}" for d in lockfile["dependencies"]
        }
        assert "github/awesome-copilot/plugins/context-engineering" in dep_keys, (
            f"Plugin missing from lockfile after sequential install. Keys: {dep_keys}"
        )
        assert "github/awesome-copilot/skills/review-and-refactor" in dep_keys, (
            f"Skill missing from lockfile after sequential install. Keys: {dep_keys}"
        )

        # deps tree should show both
        tree = subprocess.run(
            [apm_command, "deps", "tree"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=60,
        )
        combined = tree.stdout + tree.stderr
        assert "context-engineering" in combined, "Plugin missing from deps tree"
        assert "review-and-refactor" in combined, "Skill missing from deps tree"

        # Uninstall plugin should clean up agent files
        r3 = subprocess.run(
            [apm_command, "uninstall", self.PLUGIN_REF],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=60,
        )
        assert r3.returncode == 0, f"Uninstall failed:\n{r3.stderr}"
        combined = r3.stdout + r3.stderr
        assert "agent" in combined.lower(), f"Uninstall should report agent cleanup:\n{combined}"

    # ---- Test 7: compile includes plugin primitives ---------------------

    def test_compile_includes_plugin_primitives(self, apm_command, temp_project):
        """apm compile should include primitives from a normalized plugin."""
        # Install the plugin
        r = subprocess.run(
            [apm_command, "install", self.PLUGIN_REF, "--verbose"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )
        assert r.returncode == 0, f"Install failed:\n{r.stderr}"

        # Compile
        result = subprocess.run(
            [apm_command, "compile"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=60,
        )
        assert result.returncode == 0, f"Compile failed (rc={result.returncode}):\n{result.stderr}"

        # AGENTS.md should exist (even if minimal — plugin primitives are in .apm/)
        agents_md = temp_project / "AGENTS.md"
        if agents_md.exists():
            content = agents_md.read_text()
            # Should reference the plugin as a source
            assert (
                "context-engineering" in content.lower() or "awesome-copilot" in content.lower()
            ), f"AGENTS.md should reference the plugin source:\n{content[:500]}"

    # ---- Test 8: prune removes orphaned plugin --------------------------

    def test_prune_removes_orphaned_plugin(self, apm_command, temp_project):
        """apm prune should remove a plugin no longer in apm.yml."""
        # Install the plugin
        r = subprocess.run(
            [apm_command, "install", self.PLUGIN_REF, "--verbose"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )
        assert r.returncode == 0, f"Install failed:\n{r.stderr}"

        pkg_path = temp_project / "apm_modules" / self.PLUGIN_REF
        assert pkg_path.is_dir(), "Plugin must be installed before prune test"

        # Remove the plugin from apm.yml (simulate user edit)
        apm_yml = temp_project / "apm.yml"
        apm_yml.write_text(
            "name: e2e-test-project\n"
            "version: 1.0.0\n"
            "description: E2E test project\n"
            "dependencies:\n"
            "  apm: []\n"
        )

        # Prune should detect and remove the orphan
        result = subprocess.run(
            [apm_command, "prune"],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=60,
        )
        assert result.returncode == 0, f"Prune failed (rc={result.returncode}):\n{result.stderr}"

        combined = result.stdout + result.stderr
        assert "orphan" in combined.lower() or "removed" in combined.lower(), (
            f"Prune should report orphan removal:\n{combined}"
        )

    # ---- Test 9: install counter reports plugin -------------------------

    def test_install_counter_includes_plugin(self, apm_command, temp_project):
        """apm install output should count plugin as an installed dependency."""
        result = subprocess.run(
            [apm_command, "install", self.PLUGIN_REF],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )
        assert result.returncode == 0, f"Install failed:\n{result.stderr}"

        combined = result.stdout + result.stderr
        # Should NOT say "Installed 0"
        assert "installed 0" not in combined.lower(), (
            f"Install counter should not be 0 after plugin install:\n{combined}"
        )

    # ---- Test 10: lockfile records package_type -------------------------

    def test_lockfile_records_package_type(self, apm_command, temp_project):
        """Lockfile should record package_type for plugin dependencies."""
        result = subprocess.run(
            [apm_command, "install", self.PLUGIN_REF],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )
        assert result.returncode == 0, f"Install failed:\n{result.stderr}"

        import yaml

        lockfile = yaml.safe_load((temp_project / "apm.lock.yaml").read_text())
        assert "dependencies" in lockfile, "Lockfile missing dependencies"

        plugin_entry = None
        for dep in lockfile["dependencies"]:
            key = dep.get("repo_url", "")
            vpath = dep.get("virtual_path", "")
            full = f"{key}/{vpath}" if vpath else key
            if "context-engineering" in full:
                plugin_entry = dep
                break

        assert plugin_entry is not None, (
            f"Plugin not found in lockfile. Deps: {lockfile['dependencies']}"
        )
        assert plugin_entry.get("package_type") == "marketplace_plugin", (
            f"Expected package_type 'marketplace_plugin', got: {plugin_entry.get('package_type')}"
        )

    # ---- Test 11: idempotent reinstall ----------------------------------

    def test_idempotent_reinstall(self, apm_command, temp_project):
        """Running apm install twice should be safe and produce identical results."""
        # First install
        r1 = subprocess.run(
            [apm_command, "install", self.PLUGIN_REF],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )
        assert r1.returncode == 0, f"First install failed:\n{r1.stderr}"

        # Capture lockfile state
        import yaml

        lock1 = yaml.safe_load((temp_project / "apm.lock.yaml").read_text())

        # Second install (should use cache)
        r2 = subprocess.run(
            [apm_command, "install", self.PLUGIN_REF],
            capture_output=True,
            text=True,
            cwd=str(temp_project),
            timeout=180,
        )
        assert r2.returncode == 0, f"Second install failed:\n{r2.stderr}"

        # Lockfile should be identical
        lock2 = yaml.safe_load((temp_project / "apm.lock.yaml").read_text())
        assert len(lock1["dependencies"]) == len(lock2["dependencies"]), (
            "Reinstall changed lockfile dependency count"
        )

        # Package should still be on disk
        pkg_path = temp_project / "apm_modules" / self.PLUGIN_REF
        assert pkg_path.is_dir(), "Plugin should still be present after reinstall"
