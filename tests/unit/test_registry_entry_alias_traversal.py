"""Registry aliases delegate character and reserved-name checks to the shared parser."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apm_cli.models.dependency.registry_entry import parse_registry_object_entry
from apm_cli.utils.path_security import PathTraversalError


@pytest.fixture(autouse=True)
def _enable_registry_feature():
    with patch(
        "apm_cli.deps.registry.feature_gate.require_package_registry_enabled",
        return_value=None,
    ):
        yield


def _make_reference_cls() -> type:
    """A DependencyReference stand-in that records what it was constructed with."""
    instance = MagicMock()
    the_cls = MagicMock(return_value=instance)
    return the_cls


class TestRegistryObjectEntryTraversalAlias:
    def test_dotdot_alias_rejected(self):
        """A bare '..' alias passes the regex but must be caught by
        validate_path_segments."""
        cls = _make_reference_cls()
        with pytest.raises((ValueError, PathTraversalError)):
            parse_registry_object_entry(
                cls,
                {"id": "org/pkg", "version": "v1", "alias": ".."},
            )

    def test_dot_alias_rejected(self):
        """A bare dot passes the character regex but names the modules root."""
        cls = _make_reference_cls()
        with pytest.raises(ValueError, match="reserved directory names"):
            parse_registry_object_entry(
                cls,
                {"id": "org/pkg", "version": "v1", "alias": "."},
            )

    def test_encoded_dotdot_alias_rejected(self):
        """Percent-encoded '..' contains '%' which fails _ALIAS_PATTERN."""
        cls = _make_reference_cls()
        with pytest.raises((ValueError, PathTraversalError)):
            parse_registry_object_entry(
                cls,
                {"id": "org/pkg", "version": "v1", "alias": "%2e%2e"},
            )


class TestRegistryObjectEntrySafeAlias:
    def test_safe_alias_accepted(self):
        """A normal dotted / hyphenated alias must flow through unaffected."""
        cls = _make_reference_cls()
        instance = parse_registry_object_entry(
            cls,
            {"id": "org/pkg", "version": "v1", "alias": "my-skill.v2"},
        )

        assert instance is not None
        _, kwargs = cls.call_args
        assert kwargs["alias"] == "my-skill.v2"
