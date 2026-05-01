"""Unit tests for META_PACKAGE detection and validation (#1094).

A meta-package is a curated dependency aggregator: ``apm.yml`` declares
``dependencies.apm`` and/or ``dependencies.mcp`` and contributes no own
primitives (no ``.apm/``, no ``SKILL.md``, no nested skills). This shape
existed in practice as a workaround that required an empty ``.apm/.gitkeep``;
issue #1094 promoted it to a first-class type so users no longer need to
commit a placeholder directory just to satisfy the structural check.
"""

from pathlib import Path

from src.apm_cli.models.apm_package import (
    PackageType,
    validate_apm_package,
)
from src.apm_cli.models.validation import detect_package_type


class TestMetaPackageDetection:
    """Detection cascade: META_PACKAGE classification."""

    def _write_apm_yml(self, tmp_path: Path, body: str) -> None:
        (tmp_path / "apm.yml").write_text(body)

    def test_apm_yml_with_apm_deps_detected_as_meta(self, tmp_path):
        """apm.yml + non-empty dependencies.apm + no .apm/ -> META_PACKAGE."""
        self._write_apm_yml(
            tmp_path,
            "name: writing\n"
            "version: 1.0.0\n"
            "dependencies:\n"
            "  apm:\n"
            "    - owner/repo/skills/foo\n"
            "  mcp: []\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.META_PACKAGE

    def test_apm_yml_with_dev_deps_only_detected_as_meta(self, tmp_path):
        """A dev-only dep aggregator is still a meta-package."""
        self._write_apm_yml(
            tmp_path,
            "name: dev-bundle\nversion: 1.0.0\ndevDependencies:\n  apm:\n    - some/dev-tool\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.META_PACKAGE

    def test_apm_yml_with_mcp_deps_only_detected_as_meta(self, tmp_path):
        """apm.yml with only mcp deps and no .apm/ still meta."""
        self._write_apm_yml(
            tmp_path,
            "name: mcp-bundle\n"
            "version: 1.0.0\n"
            "dependencies:\n"
            "  apm: []\n"
            "  mcp:\n"
            "    - some/mcp-server\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.META_PACKAGE

    def test_apm_yml_no_deps_no_apm_dir_still_invalid(self, tmp_path):
        """apm.yml with no deps and no .apm/ stays INVALID (the warning case)."""
        self._write_apm_yml(tmp_path, "name: empty\nversion: 1.0.0\n")
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID

    def test_apm_yml_empty_deps_dict_still_invalid(self, tmp_path):
        """apm.yml with `dependencies: {apm: [], mcp: []}` -> INVALID."""
        self._write_apm_yml(
            tmp_path,
            "name: empty-deps\nversion: 1.0.0\ndependencies:\n  apm: []\n  mcp: []\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID

    def test_apm_yml_with_apm_dir_still_apm_package(self, tmp_path):
        """apm.yml + .apm/ + deps -> APM_PACKAGE (.apm/ wins over deps signal)."""
        self._write_apm_yml(
            tmp_path,
            "name: real\nversion: 1.0.0\ndependencies:\n  apm:\n    - owner/repo/skills/foo\n",
        )
        (tmp_path / ".apm").mkdir()
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.APM_PACKAGE

    def test_apm_yml_meta_with_skill_bundle_still_skill_bundle(self, tmp_path):
        """Nested skills/<x>/SKILL.md takes priority over META_PACKAGE."""
        self._write_apm_yml(
            tmp_path,
            "name: bundle\nversion: 1.0.0\ndependencies:\n  apm:\n    - owner/repo/skills/foo\n",
        )
        skills_dir = tmp_path / "skills" / "my-skill"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: A test skill\n---\n# Skill\n"
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.SKILL_BUNDLE

    def test_apm_yml_with_skill_md_root_still_hybrid(self, tmp_path):
        """Root SKILL.md + apm.yml + deps -> HYBRID (root SKILL.md wins)."""
        self._write_apm_yml(
            tmp_path,
            "name: hybrid\nversion: 1.0.0\ndependencies:\n  apm:\n    - owner/repo/skills/foo\n",
        )
        (tmp_path / "SKILL.md").write_text(
            "---\nname: root\ndescription: root skill\n---\n# Root\n"
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.HYBRID

    def test_malformed_apm_yml_treated_as_invalid(self, tmp_path):
        """Tolerant of unparseable apm.yml: INVALID, not META_PACKAGE."""
        self._write_apm_yml(tmp_path, "name: bad\nversion: 1.0.0\ndependencies: not-a-dict\n")
        pkg_type, _ = detect_package_type(tmp_path)
        # No .apm/, no nested skills, deps unparseable -> falls through to INVALID
        # so the user sees the standard "missing .apm/" diagnostic.
        assert pkg_type == PackageType.INVALID

    def test_apm_string_value_is_invalid_not_meta(self, tmp_path):
        """Schema requires apm to be a list; a string value is malformed -> INVALID."""
        self._write_apm_yml(
            tmp_path,
            "name: malformed\nversion: 1.0.0\ndependencies:\n  apm: foo\n  mcp: []\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID

    def test_apm_dict_value_is_invalid_not_meta(self, tmp_path):
        """Schema requires apm to be a list; a dict value is malformed -> INVALID."""
        self._write_apm_yml(
            tmp_path,
            "name: malformed\nversion: 1.0.0\ndependencies:\n  apm:\n    key: value\n  mcp: []\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID


class TestMetaPackageValidation:
    """Full validate_apm_package: META_PACKAGE passes cleanly."""

    def test_meta_package_passes_validation(self, tmp_path):
        """META_PACKAGE validation succeeds without `.apm/`."""
        (tmp_path / "apm.yml").write_text(
            "name: writing\n"
            "version: 1.0.0\n"
            "description: Curated writing-skills bundle\n"
            "dependencies:\n  apm:\n    - owner/repo/skills/foo\n",
        )
        result = validate_apm_package(tmp_path)
        assert result.is_valid is True
        assert result.package_type == PackageType.META_PACKAGE
        assert result.package is not None
        assert result.package.name == "writing"
        assert result.package.version == "1.0.0"
        # Validation does not require `.apm/` for META_PACKAGE.
        assert not (tmp_path / ".apm").exists()

    def test_meta_package_no_missing_apm_dir_error(self, tmp_path):
        """The legacy `missing .apm/` error must NOT fire for META_PACKAGE."""
        (tmp_path / "apm.yml").write_text(
            "name: writing\nversion: 1.0.0\ndependencies:\n  apm:\n    - owner/repo/skills/foo\n",
        )
        result = validate_apm_package(tmp_path)
        for err in result.errors:
            assert "missing the required .apm/ directory" not in err
            assert "Missing required directory: .apm/" not in err

    def test_invalid_apm_yml_still_errors_for_meta(self, tmp_path):
        """A META_PACKAGE with malformed apm.yml structure surfaces the parse error."""
        # Pass detect_package_type by declaring deps, but break the from_apm_yml
        # parser by omitting the required `version` field.
        (tmp_path / "apm.yml").write_text(
            "name: writing\ndependencies:\n  apm:\n    - owner/repo/skills/foo\n",
        )
        result = validate_apm_package(tmp_path)
        assert result.is_valid is False
        assert any("Invalid apm.yml" in err for err in result.errors)
