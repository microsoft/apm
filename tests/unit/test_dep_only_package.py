"""Unit tests for dep-only APM_PACKAGE detection and validation (#1094).

A dep-only package is a curated dependency aggregator: ``apm.yml`` declares
``dependencies.apm`` and/or ``dependencies.mcp`` and contributes no own
primitives (no ``.apm/``, no ``SKILL.md``, no nested skills). This shape
existed in practice as a workaround that required an empty ``.apm/.gitkeep``;
issue #1094 collapsed it into APM_PACKAGE so users no longer need to
commit a placeholder directory just to satisfy the structural check.
"""

from pathlib import Path

from src.apm_cli.models.apm_package import (
    PackageType,
    validate_apm_package,
)
from src.apm_cli.models.validation import detect_package_type


class TestDepOnlyPackageDetection:
    """Detection cascade: dep-only apm.yml -> APM_PACKAGE."""

    def _write_apm_yml(self, tmp_path: Path, body: str) -> None:
        (tmp_path / "apm.yml").write_text(body)

    def test_apm_yml_with_apm_deps_detected_as_apm_package(self, tmp_path):
        """apm.yml + non-empty dependencies.apm + no .apm/ -> APM_PACKAGE."""
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
        assert pkg_type == PackageType.APM_PACKAGE

    def test_apm_yml_with_dev_deps_only_detected_as_apm_package(self, tmp_path):
        """A dev-only dep aggregator is still an APM_PACKAGE."""
        self._write_apm_yml(
            tmp_path,
            "name: dev-bundle\nversion: 1.0.0\ndevDependencies:\n  apm:\n    - some/dev-tool\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.APM_PACKAGE

    def test_apm_yml_with_mcp_deps_only_detected_as_apm_package(self, tmp_path):
        """apm.yml with only mcp deps and no .apm/ still APM_PACKAGE."""
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
        assert pkg_type == PackageType.APM_PACKAGE

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

    def test_apm_yml_with_skill_bundle_still_skill_bundle(self, tmp_path):
        """Nested skills/<x>/SKILL.md takes priority over a dep-only apm.yml."""
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
        """Tolerant of unparseable apm.yml: INVALID (no .apm/, no nested skills)."""
        self._write_apm_yml(tmp_path, "name: bad\nversion: 1.0.0\ndependencies: not-a-dict\n")
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID

    def test_apm_string_value_is_invalid(self, tmp_path):
        """Schema requires apm to be a list; a string value is malformed -> INVALID."""
        self._write_apm_yml(
            tmp_path,
            "name: malformed\nversion: 1.0.0\ndependencies:\n  apm: foo\n  mcp: []\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID

    def test_apm_dict_value_is_invalid(self, tmp_path):
        """Schema requires apm to be a list; a dict value is malformed -> INVALID."""
        self._write_apm_yml(
            tmp_path,
            "name: malformed\nversion: 1.0.0\ndependencies:\n  apm:\n    key: value\n  mcp: []\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID

    def test_apm_list_with_only_non_parseable_entries_is_invalid(self, tmp_path):
        """A list of non-str/non-dict entries (e.g., bare integers) is malformed -> INVALID.

        Regression trap (#1097 panel review): the original guard accepted any
        truthy list; that meant `apm: [123]` would parse as an empty package
        with no real deps, defeating the safety check the cascade is supposed
        to enforce.
        """
        self._write_apm_yml(
            tmp_path,
            "name: malformed\nversion: 1.0.0\ndependencies:\n  apm:\n    - 123\n  mcp: []\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID

    def test_mcp_list_with_only_non_parseable_entries_is_invalid(self, tmp_path):
        """Same regression trap on the `mcp` list."""
        self._write_apm_yml(
            tmp_path,
            "name: malformed\nversion: 1.0.0\ndependencies:\n  apm: []\n  mcp:\n    - 42\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID

    def test_dev_deps_list_with_only_non_parseable_entries_is_invalid(self, tmp_path):
        """Same regression trap on `devDependencies`."""
        self._write_apm_yml(
            tmp_path,
            "name: malformed\nversion: 1.0.0\ndevDependencies:\n  apm:\n    - true\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.INVALID

    def test_apm_list_with_mix_of_parseable_and_garbage_is_apm_package(self, tmp_path):
        """A list containing at least one parseable entry is still APM_PACKAGE.

        This mirrors the parser's tolerant behavior (it silently drops
        non-str/non-dict entries) -- partial garbage doesn't void the
        package, but pure garbage does.
        """
        self._write_apm_yml(
            tmp_path,
            "name: ok\nversion: 1.0.0\ndependencies:\n  apm:\n    - 123\n    - owner/repo\n",
        )
        pkg_type, _ = detect_package_type(tmp_path)
        assert pkg_type == PackageType.APM_PACKAGE


class TestDepOnlyPackageValidation:
    """Full validate_apm_package: dep-only APM_PACKAGE passes cleanly."""

    def test_dep_only_package_passes_validation(self, tmp_path):
        """Dep-only APM_PACKAGE validation succeeds without `.apm/`."""
        (tmp_path / "apm.yml").write_text(
            "name: writing\n"
            "version: 1.0.0\n"
            "description: Curated writing-skills bundle\n"
            "dependencies:\n  apm:\n    - owner/repo/skills/foo\n",
        )
        result = validate_apm_package(tmp_path)
        assert result.is_valid is True
        assert result.package_type == PackageType.APM_PACKAGE
        assert result.package is not None
        assert result.package.name == "writing"
        assert result.package.version == "1.0.0"
        # Validation does not require `.apm/` when dependencies are declared.
        assert not (tmp_path / ".apm").exists()

    def test_dep_only_package_no_missing_apm_dir_error(self, tmp_path):
        """The legacy `missing .apm/` error must NOT fire for dep-only APM_PACKAGE."""
        (tmp_path / "apm.yml").write_text(
            "name: writing\nversion: 1.0.0\ndependencies:\n  apm:\n    - owner/repo/skills/foo\n",
        )
        result = validate_apm_package(tmp_path)
        for err in result.errors:
            assert "missing the required .apm/ directory" not in err
            assert "Missing required directory: .apm/" not in err

    def test_invalid_apm_yml_still_errors(self, tmp_path):
        """A dep-only APM_PACKAGE with malformed apm.yml surfaces the parse error."""
        # Pass detect_package_type by declaring deps, but break the from_apm_yml
        # parser by omitting the required `version` field.
        (tmp_path / "apm.yml").write_text(
            "name: writing\ndependencies:\n  apm:\n    - owner/repo/skills/foo\n",
        )
        result = validate_apm_package(tmp_path)
        assert result.is_valid is False
        assert any("Invalid apm.yml" in err for err in result.errors)
