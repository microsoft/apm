"""Unit tests for install/errors.py exception types."""

from unittest.mock import Mock, call

import pytest

from apm_cli.core.command_logger import InstallLogger
from apm_cli.deps.lockfile import LockfileConflictError, LockfileFormatError
from apm_cli.install.errors import (
    AuthenticationError,
    DirectDependencyError,  # noqa: F401
    FrozenInstallError,
    PolicyViolationError,  # noqa: F401
    install_failure_message,
    log_frozen_install_error,
)


@pytest.mark.parametrize("error_type", [RuntimeError, OSError, LockfileFormatError])
def test_non_conflict_install_failures_keep_existing_prefix(error_type):
    assert install_failure_message(error_type("original cause")) == (
        "Error installing dependencies: original cause"
    )


def test_install_conflict_message_remains_canonical(tmp_path):
    error = LockfileConflictError(tmp_path / "apm.lock.yaml")
    assert install_failure_message(error) == str(error)


@pytest.mark.parametrize(
    ("tip", "expected_tip"),
    [
        (
            None,
            "Tip: rerun 'apm install' without --frozen to create or repair package lock state, "
            "retaining the original options, including --root DIR or --global if specified.",
        ),
        ("", None),
        ("Keep the explicit remedy.", "Keep the explicit remedy."),
    ],
)
def test_frozen_renderer_honors_tip_overrides(tip, expected_tip):
    logger = Mock(spec=InstallLogger)
    error = FrozenInstallError("Original failure", tip=tip)

    log_frozen_install_error(error, logger)

    expected = [call.error("Original failure")]
    if expected_tip:
        expected.append(call.info(expected_tip))
    assert logger.mock_calls == expected


def test_frozen_renderer_retains_reason_order_and_explicit_tip():
    logger = Mock(spec=InstallLogger)
    error = FrozenInstallError("Original failure", reasons=["first", "second"], tip="Remedy")

    log_frozen_install_error(error, logger)

    assert logger.mock_calls == [
        call.error("Original failure"),
        call.error_detail("first"),
        call.error_detail("second"),
        call.info("Remedy"),
    ]


class TestAuthenticationError:
    """AuthenticationError attribute roundtrip and isinstance checks."""

    def test_carries_diagnostic_context(self):
        err = AuthenticationError(
            "Authentication failed for dev.azure.com",
            diagnostic_context="Try az login",
        )
        assert err.diagnostic_context == "Try az login"
        assert str(err) == "Authentication failed for dev.azure.com"

    def test_is_runtime_error(self):
        err = AuthenticationError("msg")
        assert isinstance(err, RuntimeError)

    def test_default_diagnostic_context_is_empty(self):
        err = AuthenticationError("msg")
        assert err.diagnostic_context == ""

    def test_multiline_diagnostic_preserved(self):
        diag = (
            "\n    ADO_APM_PAT is set, but the Azure DevOps request failed.\n"
            "    Generate a new PAT.\n"
        )
        err = AuthenticationError("msg", diagnostic_context=diag)
        assert "\n" in err.diagnostic_context
        assert "ADO_APM_PAT" in err.diagnostic_context
