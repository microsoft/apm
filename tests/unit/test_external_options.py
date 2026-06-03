"""Unit tests for scanner-agnostic options + the passthrough security gate.

Covers :mod:`apm_cli.security.external.options`:

* :func:`resolve_scanner_options` precedence matrix (the core contract that
  policy never injects argv and never forces LLM on -- restrict-only).
* :func:`validate_extra_args` fail-closed allowlist (dangerous scanner-native
  flags, credential-bearing tokens, and out-of-root path values are rejected).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.security.external.base import ExternalScanError
from apm_cli.security.external.options import (
    ScannerOptions,
    resolve_scanner_options,
    validate_extra_args,
)

_ALLOWED = frozenset({"--model", "--severity"})


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


def test_allowed_flag_passes(tmp_path: Path):
    args = ("--model", "gpt-4o", "--severity", "high")
    assert validate_extra_args("skillspector", args, _ALLOWED, base_dir=tmp_path) == args


def test_disallowed_flag_rejected(tmp_path: Path):
    with pytest.raises(ExternalScanError, match=r"not\s+allowed"):
        validate_extra_args("skillspector", ("--output",), _ALLOWED, base_dir=tmp_path)


@pytest.mark.parametrize("flag", ["--config", "--plugin", "--output", "--search-path"])
def test_dangerous_native_flags_rejected(flag: str, tmp_path: Path):
    with pytest.raises(ExternalScanError):
        validate_extra_args("skillspector", (flag, "x"), _ALLOWED, base_dir=tmp_path)


@pytest.mark.parametrize("token", ["--api-key", "--token", "--secret"])
def test_credential_flag_rejected(token: str, tmp_path: Path):
    with pytest.raises(ExternalScanError, match=r"credential"):
        validate_extra_args("skillspector", (token, "v"), _ALLOWED, base_dir=tmp_path)


def test_inline_secret_value_rejected(tmp_path: Path):
    with pytest.raises(ExternalScanError, match=r"credential"):
        validate_extra_args("skillspector", ("--api-key=sk-123",), _ALLOWED, base_dir=tmp_path)


def test_absolute_path_value_rejected(tmp_path: Path):
    with pytest.raises(ExternalScanError, match=r"scan directory"):
        validate_extra_args("skillspector", ("--model=/etc/passwd",), _ALLOWED, base_dir=tmp_path)


def test_parent_traversal_value_rejected(tmp_path: Path):
    with pytest.raises(ExternalScanError, match=r"scan directory"):
        validate_extra_args("skillspector", ("../outside",), _ALLOWED, base_dir=tmp_path)


def test_plain_value_within_root_passes(tmp_path: Path):
    args = ("--model", "gpt-4o")
    assert validate_extra_args("skillspector", args, _ALLOWED, base_dir=tmp_path) == args


def test_no_dollar_expansion_value_is_literal(tmp_path: Path):
    # The validator never expands ``$VAR``; the token is treated literally and,
    # since it is an allowed flag value with no path separators, it passes.
    args = ("--model", "$HOME")
    assert validate_extra_args("skillspector", args, _ALLOWED, base_dir=tmp_path) == args


def test_empty_allowlist_rejects_any_flag(tmp_path: Path):
    with pytest.raises(ExternalScanError):
        validate_extra_args("sarif", ("--model",), frozenset(), base_dir=tmp_path)


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
