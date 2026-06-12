"""Acceptance tests for 'apm install -g' copilot user-scope instructions.

Issue #650: apm install -g should write instruction primitives to
~/.copilot/copilot-instructions.md by concatenating all instruction files
from the package, since Copilot CLI reads a single file at user scope
instead of individual *.instructions.md files.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from apm_cli.integration.base_integrator import IntegrationResult
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, GitReferenceType, PackageInfo, ResolvedReference


def _make_package_info(package_dir: Path, name: str = "test-pkg") -> PackageInfo:
    package = APMPackage(
        name=name,
        version="1.0.0",
        package_path=package_dir,
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
        install_path=package_dir,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
    )


# ---------------------------------------------------------------------------
# Target profile: user-scope copilot
# ---------------------------------------------------------------------------


class TestCopilotUserScopeTargetProfile:
    """Copilot target must support instructions at user scope after #650."""

    def test_instructions_supported_at_user_scope(self):
        """instructions must NOT be listed in unsupported_user_primitives."""
        copilot = KNOWN_TARGETS["copilot"]
        assert "instructions" not in copilot.unsupported_user_primitives

    def test_supports_at_user_scope_instructions_true(self):
        """supports_at_user_scope('instructions') must be True for copilot."""
        copilot = KNOWN_TARGETS["copilot"]
        assert copilot.supports_at_user_scope("instructions") is True

    def test_for_scope_user_instructions_uses_concat_format(self):
        """for_scope(user_scope=True) must map instructions to copilot_user_instructions."""
        copilot = KNOWN_TARGETS["copilot"]
        user_profile = copilot.for_scope(user_scope=True)
        assert user_profile is not None
        mapping = user_profile.primitives.get("instructions")
        assert mapping is not None
        assert mapping.format_id == "copilot_user_instructions"

    def test_for_scope_user_root_is_copilot_dir(self):
        """User-scope profile must have root_dir == '.copilot'."""
        copilot = KNOWN_TARGETS["copilot"]
        user_profile = copilot.for_scope(user_scope=True)
        assert user_profile is not None
        assert user_profile.root_dir == ".copilot"


# ---------------------------------------------------------------------------
# Integration: instruction files -> ~/.copilot/copilot-instructions.md
# ---------------------------------------------------------------------------


class TestCopilotUserInstructionsIntegration:
    """InstructionIntegrator must concatenate files into copilot-instructions.md."""

    def setup_method(self):
        import tempfile

        self.temp_dir = tempfile.mkdtemp()
        self.home = Path(self.temp_dir)
        self.integrator = InstructionIntegrator()

    def teardown_method(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_pkg(self, instructions: dict[str, str]) -> PackageInfo:
        """Create a package directory with given instruction files."""
        pkg_dir = self.home / "pkg"
        inst_dir = pkg_dir / ".apm" / "instructions"
        inst_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in instructions.items():
            (inst_dir / fname).write_text(content, encoding="utf-8")
        return _make_package_info(pkg_dir)

    def test_single_instruction_file_written_to_copilot_instructions_md(self):
        """A single instruction file is written to ~/.copilot/copilot-instructions.md."""
        pkg_info = self._make_pkg(
            {
                "coding.instructions.md": (
                    "---\napplyTo: '**/*.py'\n---\n\n# Coding standards\n\nUse type hints.\n"
                ),
            }
        )
        copilot = KNOWN_TARGETS["copilot"]
        user_profile = copilot.for_scope(user_scope=True)

        result = self.integrator.integrate_instructions_for_target(
            user_profile,
            pkg_info,
            self.home,
        )

        out_file = self.home / ".copilot" / "copilot-instructions.md"
        assert out_file.exists(), "copilot-instructions.md must be created"
        content = out_file.read_text(encoding="utf-8")
        assert "# Coding standards" in content
        assert "Use type hints." in content
        assert isinstance(result, IntegrationResult)
        assert result.files_integrated == 1

    def test_multiple_instruction_files_concatenated(self):
        """Multiple instruction files are concatenated into a single output file."""
        pkg_info = self._make_pkg(
            {
                "coding.instructions.md": (
                    "---\napplyTo: '**/*.py'\n---\n\n# Python coding\n\nUse type hints.\n"
                ),
                "security.instructions.md": (
                    "---\ndescription: Security rules\n---\n\n# Security\n\nSanitize inputs.\n"
                ),
            }
        )
        copilot = KNOWN_TARGETS["copilot"]
        user_profile = copilot.for_scope(user_scope=True)

        self.integrator.integrate_instructions_for_target(
            user_profile,
            pkg_info,
            self.home,
        )

        out_file = self.home / ".copilot" / "copilot-instructions.md"
        content = out_file.read_text(encoding="utf-8")
        assert "# Python coding" in content
        assert "Use type hints." in content
        assert "# Security" in content
        assert "Sanitize inputs." in content

    def test_frontmatter_stripped_from_output(self):
        """YAML frontmatter must not appear in the concatenated output."""
        pkg_info = self._make_pkg(
            {
                "coding.instructions.md": (
                    "---\napplyTo: '**/*.py'\ndescription: Python guide\n---\n\n"
                    "# Python coding\n\nUse type hints.\n"
                ),
            }
        )
        copilot = KNOWN_TARGETS["copilot"]
        user_profile = copilot.for_scope(user_scope=True)

        self.integrator.integrate_instructions_for_target(
            user_profile,
            pkg_info,
            self.home,
        )

        out_file = self.home / ".copilot" / "copilot-instructions.md"
        content = out_file.read_text(encoding="utf-8")
        assert "applyTo:" not in content
        assert "description: Python guide" not in content
        assert "---" not in content

    def test_no_instructions_returns_zero_integrated(self):
        """No instruction files in package -> zero files integrated, no output."""
        pkg_dir = self.home / "empty-pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_info = _make_package_info(pkg_dir)

        copilot = KNOWN_TARGETS["copilot"]
        user_profile = copilot.for_scope(user_scope=True)

        result = self.integrator.integrate_instructions_for_target(
            user_profile,
            pkg_info,
            self.home,
        )

        out_file = self.home / ".copilot" / "copilot-instructions.md"
        assert not out_file.exists()
        assert result.files_integrated == 0

    def test_multi_package_instructions_all_concatenated(self):
        """Instructions from two packages must both appear in copilot-instructions.md.

        Regression test for the multi-package collision bug: the second package's
        instructions must not be silently dropped because the file already exists
        after the first package's integration.
        """
        pkg_a_dir = self.home / "pkg-a"
        (pkg_a_dir / ".apm" / "instructions").mkdir(parents=True, exist_ok=True)
        (pkg_a_dir / ".apm" / "instructions" / "a.instructions.md").write_text(
            "# Package A\n\nAlways use type hints.\n", encoding="utf-8"
        )
        pkg_a = _make_package_info(pkg_a_dir, name="pkg-a")

        pkg_b_dir = self.home / "pkg-b"
        (pkg_b_dir / ".apm" / "instructions").mkdir(parents=True, exist_ok=True)
        (pkg_b_dir / ".apm" / "instructions" / "b.instructions.md").write_text(
            "# Package B\n\nAlways sanitize inputs.\n", encoding="utf-8"
        )
        pkg_b = _make_package_info(pkg_b_dir, name="pkg-b")

        copilot = KNOWN_TARGETS["copilot"]
        user_profile = copilot.for_scope(user_scope=True)

        self.integrator.integrate_instructions_for_target(user_profile, pkg_a, self.home)
        self.integrator.integrate_instructions_for_target(user_profile, pkg_b, self.home)

        out_file = self.home / ".copilot" / "copilot-instructions.md"
        content = out_file.read_text(encoding="utf-8")
        assert "# Package A" in content, "Package A instructions must be present"
        assert "Always use type hints." in content
        assert "# Package B" in content, "Package B instructions must be present"
        assert "Always sanitize inputs." in content

    def test_strip_frontmatter_strips_crlf_line_endings(self):
        """_strip_frontmatter must handle Windows CRLF line endings."""
        crlf_content = "---\r\napplyTo: '**'\r\n---\r\n\r\n# Body content\r\n"
        result = InstructionIntegrator._strip_frontmatter(crlf_content)
        assert "# Body content" in result
        assert "applyTo:" not in result
        assert "---" not in result

    def test_project_scope_unaffected(self):
        """Project-scope integration still deploys individual .instructions.md files."""
        pkg_info = self._make_pkg(
            {
                "coding.instructions.md": "---\n---\n\n# Coding\n",
            }
        )
        copilot = KNOWN_TARGETS["copilot"]
        project_root = self.home / "project"
        (project_root / ".github" / "instructions").mkdir(parents=True, exist_ok=True)

        result = self.integrator.integrate_instructions_for_target(
            copilot,
            pkg_info,
            project_root,
        )

        assert result.files_integrated == 1
        deployed = result.target_paths[0]
        assert deployed.name == "coding.instructions.md"
        assert ".copilot" not in str(deployed)
