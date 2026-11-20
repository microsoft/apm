"""Regression-trap tests for the .collection.yml -> apm.yml migration (#1094) (filesystem, process and CLI boundary cases)."""

import pytest

pytestmark = pytest.mark.component


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
