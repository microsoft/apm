"""Regression-trap tests for the .collection.yml -> apm.yml migration (#1094).

The `.collection.yml` curated-aggregator format was removed in favor of
dep-only ``apm.yml``. Any URL still ending in `.collection.yml` or
`.collection.yaml` MUST raise a clear migration ``ValueError`` at parse
time so users know exactly what to change.
"""

import pytest

from src.apm_cli.models.dependency.reference import DependencyReference


class TestCollectionMigrationError:
    """Parsing `.collection.yml` URLs raises a migration ValueError."""

    def test_collection_yml_url_raises(self):
        with pytest.raises(ValueError, match=r"\.collection\.yml is no longer supported"):
            DependencyReference.parse("owner/repo/collections/writing.collection.yml")

    def test_collection_yaml_url_raises(self):
        with pytest.raises(ValueError, match=r"\.collection\.yml is no longer supported"):
            DependencyReference.parse("owner/repo/collections/writing.collection.yaml")

    def test_collection_yml_with_ref_raises(self):
        with pytest.raises(ValueError, match=r"\.collection\.yml is no longer supported"):
            DependencyReference.parse("owner/repo/collections/writing.collection.yml#v1.0.0")

    def test_ado_collection_yml_raises(self):
        """ADO-style URLs are also rejected."""
        with pytest.raises(ValueError, match=r"\.collection\.yml is no longer supported"):
            DependencyReference.parse(
                "dev.azure.com/org/project/_git/repo/collections/writing.collection.yml"
            )

    def test_error_message_points_to_apm_yml_migration(self):
        """The error message tells users exactly how to migrate."""
        with pytest.raises(ValueError) as exc_info:
            DependencyReference.parse("owner/repo/collections/writing.collection.yml")
        msg = str(exc_info.value)
        assert "apm.yml" in msg
        assert "dependencies" in msg
        assert "microsoft.github.io/apm" in msg

    def test_collections_path_without_extension_still_parses(self):
        """A `collections/<name>` URL with NO `.collection.yml` extension is
        a valid SUBDIRECTORY reference (no migration error).
        """
        ref = DependencyReference.parse("owner/repo/collections/writing")
        assert ref.is_virtual_subdirectory()
        assert ref.virtual_path == "collections/writing"


class TestCollectionMigrationErrorPropagation:
    """Migration error survives the two-hop wrap from APMPackage.from_apm_yml.

    The unit tests above prove ``DependencyReference.parse()`` raises
    correctly. The install pipeline calls ``APMPackage.from_apm_yml()``
    which calls ``_parse_dependency_dict()`` which catches the
    ``ValueError`` and re-raises with a prefix. This regression-trap
    proves the actionable migration text survives that re-wrap.
    """

    def test_collection_yml_in_apm_yml_surfaces_migration_message(self, tmp_path):
        from src.apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache

        clear_apm_yml_cache()
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            "name: test-project\n"
            "version: 1.0.0\n"
            "dependencies:\n"
            "  apm:\n"
            "    - owner/repo/collections/writing.collection.yml\n"
        )
        with pytest.raises(ValueError) as exc_info:
            APMPackage.from_apm_yml(apm_yml)
        msg = str(exc_info.value)
        assert ".collection.yml is no longer supported" in msg
        assert "apm.yml" in msg
        assert "dependencies" in msg
        assert "microsoft.github.io/apm" in msg
        assert "Invalid APM dependency" in msg

    def test_collection_yml_in_dev_dependencies_surfaces_migration_message(self, tmp_path):
        from src.apm_cli.models.apm_package import APMPackage, clear_apm_yml_cache

        clear_apm_yml_cache()
        apm_yml = tmp_path / "apm.yml"
        apm_yml.write_text(
            "name: test-project\n"
            "version: 1.0.0\n"
            "devDependencies:\n"
            "  apm:\n"
            "    - owner/repo/collections/writing.collection.yaml\n"
        )
        with pytest.raises(ValueError) as exc_info:
            APMPackage.from_apm_yml(apm_yml)
        msg = str(exc_info.value)
        assert ".collection.yml is no longer supported" in msg
        assert "Invalid dev APM dependency" in msg
