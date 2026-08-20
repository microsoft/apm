"""Regression tests for machine-output detection at the root CLI."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.policy.discovery import PolicyFetchResult


@pytest.mark.parametrize(
    "args",
    [
        ["policy", "status", "--output=json"],
        ["policy", "status", "-ojson"],
        ["audit", "--ci", "--no-drift", "--no-policy", "--format=json"],
        ["audit", "--ci", "--no-drift", "--no-policy", "-fjson"],
        ["--verbose", "policy", "status", "--output", "json"],
    ],
)
def test_machine_output_keeps_update_notice_off_stdout(args: list[str]) -> None:
    """Every Click spelling must leave stdout as one parseable JSON document."""
    from apm_cli.cli import cli

    with (
        patch("apm_cli.commands._helpers.is_self_update_enabled", return_value=True),
        patch("apm_cli.commands._helpers.get_version", return_value="1.0.0"),
        patch("apm_cli.commands._helpers.check_for_updates", return_value="2.0.0"),
        patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            return_value=PolicyFetchResult(outcome="absent"),
        ),
        CliRunner().isolated_filesystem(),
    ):
        result = CliRunner().invoke(cli, args)

    assert result.exception is None, result.output
    json.loads(result.stdout)
    assert "A new version of APM is available" in result.stderr
    assert "A new version of APM is available" not in result.stdout


def test_human_output_is_not_machine_readable() -> None:
    from apm_cli.core.output_mode import detect_output_mode

    assert not detect_output_mode(("policy", "status", "--output", "table")).machine_readable
