"""Strict transport policy threaded from the positional install CLI."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps.transport_selection import ProtocolPreference
from apm_cli.utils.git_env import GitUrlRewriteProbeError

pytestmark = pytest.mark.component


def _write_cli_manifest(root: Path) -> None:
    (root / "apm.yml").write_text(
        "name: transport-test\n"
        "version: 1.0.0\n"
        "targets: [copilot]\n"
        "dependencies:\n"
        "  apm: []\n"
        "  mcp: []\n",
        encoding="ascii",
    )


@pytest.mark.parametrize(
    ("args", "environment", "saved_pref", "saved_fallback", "expected_pref", "expected_fallback"),
    (
        (
            ("--ssh", "--allow-protocol-fallback"),
            {},
            None,
            False,
            ProtocolPreference.SSH,
            True,
        ),
        (
            (),
            {"APM_GIT_PROTOCOL": "https", "APM_ALLOW_PROTOCOL_FALLBACK": "1"},
            None,
            False,
            ProtocolPreference.HTTPS,
            True,
        ),
        (
            (),
            {},
            "ssh",
            True,
            ProtocolPreference.SSH,
            True,
        ),
    ),
    ids=("cli-flags", "environment", "saved-config"),
)
def test_positional_cli_threads_resolved_transport_policy_to_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
    environment: dict[str, str],
    saved_pref: str | None,
    saved_fallback: bool,
    expected_pref: ProtocolPreference,
    expected_fallback: bool,
) -> None:
    """Positional validation receives the same resolved policy as clone."""
    _write_cli_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    with (
        patch(
            "apm_cli.config.get_apm_protocol_pref",
            return_value=saved_pref or environment.get("APM_GIT_PROTOCOL"),
        ),
        patch(
            "apm_cli.config.get_apm_allow_protocol_fallback",
            return_value=saved_fallback or bool(environment),
        ),
        patch(
            "apm_cli.commands.install._validate_package_exists",
            return_value=False,
        ) as validate,
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
    ):
        result = CliRunner().invoke(
            cli,
            ["install", *args, "git.example.test/acme/repo"],
        )

    assert result.exit_code != 0
    assert validate.call_args.kwargs["protocol_pref"] == expected_pref
    assert validate.call_args.kwargs["allow_protocol_fallback"] is expected_fallback


@pytest.mark.parametrize(
    ("probe_error", "expected_text"),
    (
        (False, "different network host"),
        (True, "Unable to verify Git URL rewrite safety"),
    ),
    ids=("unsafe-rewrite", "probe-failure"),
)
def test_positional_cli_preserves_git_rewrite_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_error: bool,
    expected_text: str,
) -> None:
    """Rewrite failures stay actionable instead of becoming parse fallback."""
    _write_cli_manifest(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv(
        "GIT_CONFIG_KEY_0",
        "url.https://mirror.example/.insteadOf",
    )
    monkeypatch.setenv(
        "GIT_CONFIG_VALUE_0",
        "https://git.example.test/",
    )
    load_patch = (
        patch(
            "apm_cli.deps.transport_selection.GitConfigInsteadOfResolver._load_rewrites",
            side_effect=GitUrlRewriteProbeError("fixture probe failure"),
        )
        if probe_error
        else patch("apm_cli.commands._helpers.check_for_updates", return_value=None)
    )

    with (
        load_patch,
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
    ):
        result = CliRunner().invoke(
            cli,
            ["install", "git.example.test/acme/repo"],
        )

    assert result.exit_code != 0
    rendered = " ".join(result.output.split())
    assert expected_text in rendered
    assert "git config --show-origin --get-regexp" in rendered
