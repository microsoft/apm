"""Unit tests for APMPackage devDependencies support (pure logic cases)."""

import pytest

from apm_cli.models.apm_package import (
    APMPackage,
)

pytestmark = pytest.mark.unit


class TestTargetField:
    """Tests for target field supporting both str and list[str]."""

    def test_target_direct_construction_string(self):
        """APMPackage can be constructed with target as string."""
        pkg = APMPackage(name="t", version="1.0.0", target="claude")
        assert pkg.target == "claude"

    def test_target_direct_construction_list(self):
        """APMPackage can be constructed with target as list."""
        pkg = APMPackage(name="t", version="1.0.0", target=["claude", "copilot"])
        assert pkg.target == ["claude", "copilot"]
