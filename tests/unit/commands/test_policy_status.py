"""Tests for ``apm policy status`` diagnostic command."""

from __future__ import annotations

import json
import textwrap
import unicodedata
from pathlib import Path  # noqa: F401
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.policy import (
    _count_rules,
    _format_age,
)
from apm_cli.commands.policy import (
    policy as policy_group,
)
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.parser import load_policy
from apm_cli.policy.schema import ApmPolicy

# -- Fixtures -------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


def _make_policy(yaml_str: str) -> ApmPolicy:
    pol, _ = load_policy(yaml_str)
    return pol


def _rich_policy() -> ApmPolicy:
    """A policy with a non-trivial set of rules across sections."""
    return _make_policy(
        textwrap.dedent(
            """\
            name: test-policy
            version: '1.0'
            enforcement: block
            dependencies:
              deny:
                - evil/*
                - bad/actor
              allow:
                - safe/*
              require:
                - org/baseline
            mcp:
              deny:
                - bad-mcp
              transport:
                allow: [stdio]
            compilation:
              target:
                allow: [vscode, claude]
            manifest:
              required_fields: [name, version]
            unmanaged_files:
              directories: [.legacy, .scratch]
            """
        )
    )


def _ascii_only(text: str) -> bool:
    """Return True iff every codepoint is printable ASCII (U+0020-U+007E)."""
    for ch in text:
        if ch in ("\n", "\r", "\t"):
            continue
        cp = ord(ch)
        if cp < 0x20 or cp > 0x7E:
            # Allow Rich box-drawing characters in *rendered* output --
            # they originate from the Rich library, not our source code.
            # The encoding rule applies to source/CLI strings we author.
            if unicodedata.category(ch).startswith("C"):
                return False
            # Box-drawing & similar are tolerated in Rich-rendered output;
            # this guard exists to catch emojis or stray smart-quotes in
            # text we control.
            if 0x2500 <= cp <= 0x257F:
                continue
            if cp in (
                0x2501,
                0x2503,
                0x250F,
                0x2513,
                0x2517,
                0x251B,
                0x2523,
                0x252B,
                0x2533,
                0x253B,
                0x254B,
                0x2578,
                0x2579,
                0x257A,
                0x257B,
            ):
                continue
            return False
    return True


# -- _format_age ----------------------------------------------------


class TestFormatAge:
    def test_none(self):
        assert _format_age(None) == "n/a"

    def test_seconds(self):
        assert _format_age(5) == "5s ago"

    def test_minutes(self):
        assert _format_age(125) == "2m ago"

    def test_hours(self):
        assert _format_age(3600 * 3 + 12) == "3h ago"

    def test_days(self):
        assert _format_age(3600 * 24 * 8) == "8d ago"


# -- _count_rules ---------------------------------------------------


class TestCountRules:
    def test_empty_policy(self):
        counts = _count_rules(ApmPolicy())
        # Allow-list "no opinion" reports -1 to distinguish from explicit empty.
        assert counts["dependencies_allow"] == -1
        assert counts["dependencies_deny"] == 0
        assert counts["mcp_transports_allowed"] == -1
        assert counts["compilation_targets_allowed"] == -1

    def test_rich_policy(self):
        counts = _count_rules(_rich_policy())
        assert counts["dependencies_deny"] == 2
        assert counts["dependencies_allow"] == 1
        assert counts["dependencies_require"] == 1
        assert counts["mcp_deny"] == 1
        assert counts["mcp_transports_allowed"] == 1
        assert counts["compilation_targets_allowed"] == 2
        assert counts["manifest_required_fields"] == 2
        assert counts["unmanaged_files_directories"] == 2

    def test_none(self):
        assert _count_rules(None) == {}


# -- Status command renderings -------------------------------------


class TestStatusFoundOutcome:
    def test_renders_found_outcome(self, runner):
        result_obj = PolicyFetchResult(
            policy=_rich_policy(),
            source="org:contoso/.github",
            outcome="found",
            cached=True,
            cache_age_seconds=120,
        )
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            return_value=result_obj,
        ):
            result = runner.invoke(policy_group, ["status"])
        assert result.exit_code == 0, result.output
        assert "found" in result.output
        assert "block" in result.output  # enforcement
        assert "org:contoso/.github" in result.output
        assert "2m ago" in result.output
        assert "dependency denies" in result.output
        assert _ascii_only(result.output)


class TestStatusAbsentOutcome:
    def test_renders_absent_cleanly(self, runner):
        result_obj = PolicyFetchResult(
            source="org:contoso/.github",
            outcome="absent",
        )
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            return_value=result_obj,
        ):
            result = runner.invoke(policy_group, ["status"])
        assert result.exit_code == 0, result.output
        assert "absent" in result.output
        assert "n/a" in result.output  # enforcement + cache_age
        assert "none" in result.output  # extends + rules
        assert _ascii_only(result.output)


class TestStatusCachedStaleOutcome:
    def test_renders_stale_with_refresh_error(self, runner):
        result_obj = PolicyFetchResult(
            policy=_rich_policy(),
            source="org:contoso/.github",
            outcome="cached_stale",
            cached=True,
            cache_stale=True,
            cache_age_seconds=3600 * 24 * 8,
            fetch_error="HTTP 503 fetching contoso/.github",
        )
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            return_value=result_obj,
        ):
            result = runner.invoke(policy_group, ["status"])
        assert result.exit_code == 0, result.output
        assert "cached_stale" in result.output
        assert "stale" in result.output
        assert "8d ago" in result.output
        assert "refresh failed" in result.output
        assert "HTTP 503" in result.output
        assert _ascii_only(result.output)


class TestStatusJsonOutput:
    def test_json_is_valid_with_expected_schema(self, runner):
        result_obj = PolicyFetchResult(
            policy=_rich_policy(),
            source="org:contoso/.github",
            outcome="found",
            cached=False,
            cache_age_seconds=None,
        )
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            return_value=result_obj,
        ):
            result = runner.invoke(policy_group, ["status", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        for key in (
            "outcome",
            "source",
            "enforcement",
            "cache_age_seconds",
            "cache_age_human",
            "cache_stale",
            "cached",
            "fetch_error",
            "error",
            "extends_chain",
            "rule_counts",
            "rule_summary",
        ):
            assert key in data, f"missing key: {key}"
        assert data["outcome"] == "found"
        assert data["enforcement"] == "block"
        assert data["rule_counts"]["dependencies_deny"] == 2
        assert isinstance(data["extends_chain"], list)
        assert isinstance(data["rule_summary"], list)
        assert _ascii_only(result.output)

    def test_dash_o_json_alias(self, runner):
        result_obj = PolicyFetchResult(
            source="org:contoso/.github",
            outcome="absent",
        )
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            return_value=result_obj,
        ):
            result = runner.invoke(policy_group, ["status", "-o", "json"])
        assert result.exit_code == 0, result.output
        json.loads(result.output)  # must parse


class TestStatusNoCache:
    def test_no_cache_triggers_fresh_fetch(self, runner):
        result_obj = PolicyFetchResult(
            source="org:contoso/.github",
            outcome="absent",
        )
        with (
            patch(
                "apm_cli.commands.policy.discover_policy",
                return_value=result_obj,
            ) as mock_disc,
            patch("apm_cli.commands.policy.discover_policy_with_chain") as mock_chain,
        ):
            result = runner.invoke(policy_group, ["status", "--no-cache"])
        assert result.exit_code == 0, result.output
        # --no-cache must bypass the chain helper and call discover_policy
        # with no_cache=True so the cache layer is skipped.
        mock_chain.assert_not_called()
        mock_disc.assert_called_once()
        _, kwargs = mock_disc.call_args
        assert kwargs.get("no_cache") is True


class TestStatusPolicySourceOverride:
    def test_policy_source_override(self, runner, tmp_path):
        policy_file = tmp_path / "apm-policy.yml"
        policy_file.write_text(
            "name: override-policy\nversion: '1.0'\nenforcement: warn\n",
            encoding="utf-8",
        )
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                policy_group,
                ["status", "--policy-source", str(policy_file), "--json"],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["enforcement"] == "warn"
        assert data["source"].startswith("file:")
        assert str(policy_file) in data["source"]

    def test_policy_source_routes_through_discover_policy(self, runner):
        result_obj = PolicyFetchResult(
            policy=_rich_policy(),
            source="url:https://example.com/p.yml",
            outcome="found",
        )
        with (
            patch(
                "apm_cli.commands.policy.discover_policy",
                return_value=result_obj,
            ) as mock_disc,
            patch("apm_cli.commands.policy.discover_policy_with_chain") as mock_chain,
        ):
            result = runner.invoke(
                policy_group,
                ["status", "--policy-source", "https://example.com/p.yml"],
            )
        assert result.exit_code == 0, result.output
        mock_chain.assert_not_called()
        mock_disc.assert_called_once()
        _, kwargs = mock_disc.call_args
        assert kwargs.get("policy_override") == "https://example.com/p.yml"


class TestStatusExitCodes:
    @pytest.mark.parametrize(
        "outcome",
        [
            "found",
            "absent",
            "cached_stale",
            "cache_miss_fetch_fail",
            "garbage_response",
            "malformed",
            "no_git_remote",
            "disabled",
            "empty",
        ],
    )
    def test_exit_code_is_always_zero(self, runner, outcome):
        result_obj = PolicyFetchResult(outcome=outcome)
        if outcome in ("found", "cached_stale", "empty"):
            result_obj.policy = _rich_policy()
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            return_value=result_obj,
        ):
            result = runner.invoke(policy_group, ["status"])
        assert result.exit_code == 0, (
            f"outcome={outcome} produced exit {result.exit_code}\noutput:\n{result.output}"
        )


class TestStatusDiscoveryException:
    def test_unexpected_error_still_exits_zero(self, runner):
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            side_effect=RuntimeError("boom"),
        ):
            result = runner.invoke(policy_group, ["status"])
        assert result.exit_code == 0
        # The synthetic outcome should land in the rendered table.
        assert "cache_miss_fetch_fail" in result.output


class TestStatusAsciiOnly:
    @pytest.mark.parametrize(
        "outcome,policy_obj,extras",
        [
            ("found", _rich_policy(), {"cached": True, "cache_age_seconds": 60}),
            ("absent", None, {}),
            (
                "cached_stale",
                _rich_policy(),
                {
                    "cached": True,
                    "cache_stale": True,
                    "cache_age_seconds": 99999,
                    "fetch_error": "boom",
                },
            ),
            ("disabled", None, {}),
        ],
    )
    def test_renderings_are_ascii_safe(self, runner, outcome, policy_obj, extras):
        result_obj = PolicyFetchResult(
            outcome=outcome,
            policy=policy_obj,
            source="org:contoso/.github",
            **extras,
        )
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            return_value=result_obj,
        ):
            table_result = runner.invoke(policy_group, ["status"])
            json_result = runner.invoke(policy_group, ["status", "--json"])
        assert _ascii_only(table_result.output), (
            f"non-ASCII output for outcome={outcome}: {table_result.output!r}"
        )
        assert _ascii_only(json_result.output), (
            f"non-ASCII JSON for outcome={outcome}: {json_result.output!r}"
        )


class TestStatusCheckFlag:
    """``--check`` flips exit code to 1 when no usable policy is found."""

    def test_check_exits_zero_when_outcome_is_found(self, runner):
        result_obj = PolicyFetchResult(outcome="found", policy=_rich_policy())
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            return_value=result_obj,
        ):
            result = runner.invoke(policy_group, ["status", "--check"])
        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize(
        "outcome",
        [
            "absent",
            "cache_miss_fetch_fail",
            "garbage_response",
            "malformed",
            "no_git_remote",
            "disabled",
            "empty",
        ],
    )
    def test_check_exits_one_when_policy_unresolvable(self, runner, outcome):
        result_obj = PolicyFetchResult(outcome=outcome)
        if outcome == "empty":
            result_obj.policy = _rich_policy()
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            return_value=result_obj,
        ):
            result = runner.invoke(policy_group, ["status", "--check"])
        assert result.exit_code == 1, (
            f"outcome={outcome} should exit 1 with --check, got "
            f"{result.exit_code}\noutput:\n{result.output}"
        )

    def test_check_exits_one_on_discovery_exception(self, runner):
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            side_effect=RuntimeError("boom"),
        ):
            result = runner.invoke(policy_group, ["status", "--check"])
        assert result.exit_code == 1
        assert "cache_miss_fetch_fail" in result.output

    def test_check_with_json_output(self, runner):
        """--check still emits JSON; only the exit code changes."""
        result_obj = PolicyFetchResult(outcome="absent")
        with patch(
            "apm_cli.commands.policy.discover_policy_with_chain",
            return_value=result_obj,
        ):
            result = runner.invoke(policy_group, ["status", "--check", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["outcome"] == "absent"
