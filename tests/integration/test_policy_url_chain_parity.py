"""Hermetic command-level parity for cold and warm URL policy chains."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.policy.discovery import _read_cache_entry

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "policy_url_chain"


def _response(content: str) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.text = content
    response.headers = {}
    return response


def test_policy_status_preserves_url_chain_denials_on_warm_cache(
    tmp_path: Path, monkeypatch
) -> None:
    """The status command sees the same strict merged policy cold and warm."""
    leaf_url = "https://policy.example.com/leaf.yml"
    parent_url = "https://policy.example.com/parent.yml"
    payloads = {
        leaf_url: _response((FIXTURES_DIR / "leaf.yml").read_text(encoding="utf-8")),
        parent_url: _response((FIXTURES_DIR / "parent.yml").read_text(encoding="utf-8")),
    }
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    with (
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
        patch(
            "apm_cli.policy.discovery.requests.get",
            side_effect=lambda url, **_kwargs: payloads[url],
        ) as transport,
    ):
        cold_result = runner.invoke(
            cli,
            ["policy", "status", "--json", "--policy-source", leaf_url],
            catch_exceptions=False,
        )
        cold = json.loads(cold_result.output)
        cold_entry = _read_cache_entry(leaf_url, tmp_path)
        assert cold_entry is not None

        transport.side_effect = AssertionError("warm command reached the network")
        warm_result = runner.invoke(
            cli,
            ["policy", "status", "--json", "--policy-source", leaf_url],
            catch_exceptions=False,
        )
        warm = json.loads(warm_result.output)

    assert cold_result.exit_code == warm_result.exit_code == 0
    assert transport.call_count == 2
    assert cold["enforcement"] == warm["enforcement"] == "block"
    assert cold["rule_counts"] == warm["rule_counts"]
    assert cold["rule_counts"]["dependencies_deny"] == 3
    assert cold["rule_counts"]["dependencies_require"] == 1
    assert cold_entry.policy.dependencies.deny == (
        "parent/blocked-one",
        "parent/blocked-two",
        "leaf/blocked",
    )
