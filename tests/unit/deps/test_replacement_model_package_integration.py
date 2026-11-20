"""Replace declarations loaded from an apm.yml on disk."""

import pytest

pytestmark = pytest.mark.component

# ---------------------------------------------------------------------------
# Happy path: valid replace blocks
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Error cases: structural and type violations
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Selector identity and normalization
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# APMPackage.from_mapping integration
# ---------------------------------------------------------------------------

_SOURCE_REPLACEMENTS_V1_SCHEMA = (
    "https://microsoft.github.io/apm/specs/schemas/manifest-source-replacements-v1.schema.json"
)


class TestAPMPackageReplaceIntegration:
    """``replace:`` parsed through ``APMPackage.from_mapping``."""

    def _base_manifest(self, **extra):
        data = {"name": "test-pkg", "version": "1.0.0", **extra}
        return data

    def _replace_manifest(self, **extra):
        """Base manifest with source-replacements-v1 contract."""
        return self._base_manifest(**{"$schema": _SOURCE_REPLACEMENTS_V1_SCHEMA, **extra})

    def test_no_replace_key(self, tmp_path):
        from apm_cli.models.apm_package import APMPackage

        pkg = APMPackage.from_mapping(
            self._base_manifest(),
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=None,
        )
        assert pkg.replace_rules is None

    def test_empty_replace_list(self, tmp_path):
        from apm_cli.models.apm_package import APMPackage

        pkg = APMPackage.from_mapping(
            self._replace_manifest(replace=[]),
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=None,
        )
        assert pkg.replace_rules == []

    def test_valid_replace_parsed(self, tmp_path):
        from apm_cli.models.apm_package import APMPackage

        manifest = self._replace_manifest(
            replace=[
                {
                    "for": {"git": "https://github.com/org/lib.git"},
                    "with": {"path": "../local-lib"},
                }
            ]
        )
        pkg = APMPackage.from_mapping(
            manifest,
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=None,
        )
        assert pkg.replace_rules is not None
        assert len(pkg.replace_rules) == 1
        assert pkg.replace_rules[0].selector.repo_url == "org/lib"

    def test_malformed_replace_raises_valueerror(self, tmp_path):
        from apm_cli.models.apm_package import APMPackage

        manifest = self._replace_manifest(replace="not-a-list")
        with pytest.raises(ValueError, match="must be a list"):
            APMPackage.from_mapping(
                manifest,
                package_path=tmp_path,
                source_path=tmp_path,
                warning_handler=None,
            )

    def test_replace_not_in_unknown_keys(self, tmp_path):
        """``replace`` does not trigger an unknown-key warning."""
        from apm_cli.models.apm_package import APMPackage

        warnings: list = []

        def capture_warning(keys, path):
            warnings.extend(keys)

        APMPackage.from_mapping(
            self._replace_manifest(
                replace=[
                    {
                        "for": {"git": "https://github.com/a/b"},
                        "with": {"path": "./local"},
                    }
                ]
            ),
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=capture_warning,
        )
        assert "replace" not in warnings

    # -- Contract gate tests (D3) ------------------------------------------

    def test_replace_with_working_draft_rejects(self, tmp_path):
        """Working-draft (no ``$schema``) with ``replace`` is rejected."""
        from apm_cli.models.apm_package import APMPackage

        manifest = self._base_manifest(replace=[])
        with pytest.raises(ValueError, match="source-replacements-v1"):
            APMPackage.from_mapping(
                manifest,
                package_path=tmp_path,
                source_path=tmp_path,
                warning_handler=None,
            )

    def test_replace_with_openapm_v01_rejects(self, tmp_path):
        """OpenAPM v0.1 ``$schema`` with ``replace`` is rejected."""
        from apm_cli.models.apm_package import APMPackage
        from apm_cli.models.manifest_contract import OPENAPM_V01_SCHEMA_URI

        manifest = self._base_manifest(
            **{"$schema": OPENAPM_V01_SCHEMA_URI},
            replace=[],
        )
        with pytest.raises(ValueError, match="source-replacements-v1"):
            APMPackage.from_mapping(
                manifest,
                package_path=tmp_path,
                source_path=tmp_path,
                warning_handler=None,
            )

    def test_replace_with_correct_schema_accepts(self, tmp_path):
        """Source-replacements-v1 ``$schema`` with ``replace`` is accepted."""
        from apm_cli.models.apm_package import APMPackage

        manifest = self._replace_manifest(replace=[])
        pkg = APMPackage.from_mapping(
            manifest,
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=None,
        )
        assert pkg.replace_rules == []
        assert pkg.manifest_contract == "source-replacements-v1"

    def test_no_replace_with_correct_schema_accepted(self, tmp_path):
        """Source-replacements-v1 ``$schema`` is valid even without ``replace``."""
        from apm_cli.models.apm_package import APMPackage

        manifest = self._base_manifest(**{"$schema": _SOURCE_REPLACEMENTS_V1_SCHEMA})
        pkg = APMPackage.from_mapping(
            manifest,
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=None,
        )
        assert pkg.replace_rules is None
        assert pkg.manifest_contract == "source-replacements-v1"

    def test_no_replace_without_schema_is_working_draft(self, tmp_path):
        """Absent ``replace`` and absent ``$schema`` remains working-draft."""
        from apm_cli.models.apm_package import APMPackage

        pkg = APMPackage.from_mapping(
            self._base_manifest(),
            package_path=tmp_path,
            source_path=tmp_path,
            warning_handler=None,
        )
        assert pkg.replace_rules is None
        assert pkg.manifest_contract == "working-draft"


# ---------------------------------------------------------------------------
# ReplacementIndex
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------
