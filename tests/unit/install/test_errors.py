"""Unit tests for install/errors.py exception types."""

from apm_cli.install.errors import AuthenticationError, DirectDependencyError, PolicyViolationError


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
