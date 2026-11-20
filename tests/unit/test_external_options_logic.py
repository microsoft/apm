"""Unit tests for scanner-agnostic options + the passthrough security gate (pure logic cases)."""

from __future__ import annotations

import pytest

from apm_cli.security.external.options import (
    ScannerOptions,
    resolve_scanner_options,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# resolve_scanner_options -- llm precedence
# ---------------------------------------------------------------------------


def test_llm_defaults_to_none_when_no_layer_opines():
    opts = resolve_scanner_options(
        cli_llm=None,
        cli_args=None,
        config_llm=None,
        config_args=None,
        policy_allow_args=None,
    )
    assert opts.llm is None
    assert opts.extra_args == ()


def test_llm_config_used_when_no_cli():
    opts = resolve_scanner_options(
        cli_llm=None,
        cli_args=None,
        config_llm=True,
        config_args=None,
        policy_allow_args=None,
    )
    assert opts.llm is True


def test_llm_cli_overrides_config_true_to_false():
    opts = resolve_scanner_options(
        cli_llm=False,
        cli_args=None,
        config_llm=True,
        config_args=None,
        policy_allow_args=None,
    )
    assert opts.llm is False


def test_llm_cli_true_overrides_config_false():
    opts = resolve_scanner_options(
        cli_llm=True,
        cli_args=None,
        config_llm=False,
        config_args=None,
        policy_allow_args=None,
    )
    assert opts.llm is True


# ---------------------------------------------------------------------------
# resolve_scanner_options -- args precedence + policy restrict-only
# ---------------------------------------------------------------------------


def test_args_cli_overrides_config():
    opts = resolve_scanner_options(
        cli_llm=None,
        cli_args=("--model", "gpt-4o"),
        config_llm=None,
        config_args=("--severity", "high"),
        policy_allow_args=None,
    )
    assert opts.extra_args == ("--model", "gpt-4o")


def test_args_config_used_when_cli_absent():
    opts = resolve_scanner_options(
        cli_llm=None,
        cli_args=None,
        config_llm=None,
        config_args=("--severity", "high"),
        policy_allow_args=None,
    )
    assert opts.extra_args == ("--severity", "high")


def test_policy_allow_args_false_strips_cli_args():
    opts = resolve_scanner_options(
        cli_llm=None,
        cli_args=("--model", "gpt-4o"),
        config_llm=None,
        config_args=None,
        policy_allow_args=False,
    )
    assert opts.extra_args == ()


def test_policy_allow_args_false_strips_config_args():
    opts = resolve_scanner_options(
        cli_llm=None,
        cli_args=None,
        config_llm=None,
        config_args=("--severity", "high"),
        policy_allow_args=False,
    )
    assert opts.extra_args == ()


def test_policy_allow_args_true_permits_args():
    opts = resolve_scanner_options(
        cli_llm=None,
        cli_args=("--model", "gpt-4o"),
        config_llm=None,
        config_args=None,
        policy_allow_args=True,
    )
    assert opts.extra_args == ("--model", "gpt-4o")


def test_empty_cli_args_tuple_overrides_config():
    # Passed-but-empty (``--external-args ""`` parsed to ``()``) still wins over
    # config -- the user explicitly cleared args this run.
    opts = resolve_scanner_options(
        cli_llm=None,
        cli_args=(),
        config_llm=None,
        config_args=("--severity", "high"),
        policy_allow_args=None,
    )
    assert opts.extra_args == ()


def test_regression_policy_never_contributes_argv():
    # There is no parameter through which policy could add argv tokens: the only
    # policy input is the restrict-only ``allow_args`` boolean.
    opts = resolve_scanner_options(
        cli_llm=None,
        cli_args=None,
        config_llm=None,
        config_args=None,
        policy_allow_args=True,
    )
    assert opts.extra_args == ()


# ---------------------------------------------------------------------------
# validate_extra_args -- allowlist (fail-closed)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# validate_value_arity -- exactly one value for value-bearing flags
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ScannerOptions value object
# ---------------------------------------------------------------------------


def test_scanner_options_defaults_are_inert():
    opts = ScannerOptions()
    assert opts.llm is None
    assert opts.extra_args == ()


def test_scanner_options_is_frozen():
    opts = ScannerOptions(llm=True)
    with pytest.raises(AttributeError):
        opts.llm = False  # type: ignore[misc]
