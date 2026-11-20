"""Tests for ``load_marketplace_from_apm_yml`` (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.marketplace.output_profiles import MARKETPLACE_OUTPUTS, known_output_names

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-effective-marketplace-output-path")]


def test_marketplace_output_profiles_define_supported_outputs() -> None:
    assert known_output_names() == {"claude", "codex", "copilot"}
    assert MARKETPLACE_OUTPUTS["claude"].mapper == "claude"
    assert MARKETPLACE_OUTPUTS["claude"].supports_cli_output_override is True
    assert MARKETPLACE_OUTPUTS["codex"].mapper == "codex"
    assert MARKETPLACE_OUTPUTS["codex"].supports_cli_output_override is False
    assert MARKETPLACE_OUTPUTS["codex"].required_package_fields == ("category",)
    assert MARKETPLACE_OUTPUTS["copilot"].mapper == "copilot"
    assert MARKETPLACE_OUTPUTS["copilot"].supports_cli_output_override is False
    assert MARKETPLACE_OUTPUTS["copilot"].required_package_fields == ()
